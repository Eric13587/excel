"""Versioned schema migrations.

Replaces the "try: ALTER TABLE … except OperationalError: pass" pattern in
``DatabaseManager.create_tables`` for everything added from Phase 1 onward.
That pattern is a migration system with no version number, no ordering
guarantee and no failure reporting: a statement that fails for an unexpected
reason is indistinguishable from one that was already applied.

Design notes
------------
* **Each migration runs in one transaction.** ``sqlite3.Connection.executescript``
  cannot be used for this — it commits any pending transaction and then runs
  every statement in autocommit, so a later failure is not rolled back.
  Statements are therefore split with ``sqlite3.complete_statement`` (which
  correctly keeps ``CREATE TRIGGER … BEGIN … END;`` bodies intact) and executed
  individually inside an explicit ``BEGIN IMMEDIATE``.

* **Failure is never fatal.** A journal whose data cannot satisfy a migration
  must still open, so the user can export or repair it. The migration is rolled
  back, the version is not recorded, and the failure is reported through the
  returned :class:`MigrationReport` for the UI to surface.

* **Migrations are ordered by their numeric prefix** and applied at most once,
  tracked in the ``schema_migrations`` table.

* **Two kinds are supported.** ``NNN_name.sql`` is executed statement by
  statement. ``NNN_name.py`` must define ``apply(conn)`` and is called with the
  open connection inside the same transaction — needed whenever a migration has
  to do something SQL cannot express, such as parsing a value out of free text.

* **A migration may ask for foreign keys off** with a header directive::

      -- pragma: foreign_keys=off

  SQLite's table-rebuild procedure requires it, and ``PRAGMA foreign_keys`` is
  a silent no-op inside a transaction, so the runner must set it before
  ``BEGIN``. Such a migration is verified with ``PRAGMA foreign_key_check``
  *before* it commits, so a rebuild that would leave dangling references is
  rolled back rather than persisted.
"""
import logging
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

MIGRATION_TABLE = "schema_migrations"

# 001_some_name.sql — the numeric prefix is the version and defines the order.
_FILENAME_RE = re.compile(r"^(\d{3,})_(.+)\.(sql|py)$")


@dataclass
class MigrationReport:
    """Outcome of one ``apply_pending`` run."""
    applied: list = field(default_factory=list)      # [(version, name)]
    already_applied: list = field(default_factory=list)
    failed: tuple = None                             # (version, name, error message)

    @property
    def ok(self):
        return self.failed is None

    def summary(self):
        if self.failed:
            v, name, err = self.failed
            return f"migration {v:03d}_{name} failed: {err}"
        if self.applied:
            return "applied " + ", ".join(f"{v:03d}_{n}" for v, n in self.applied)
        return "schema up to date"


def migrations_dir():
    """Directory holding the .sql files, working frozen or from source.

    Mirrors the resolution used by :mod:`src.branding` for bundled resources:
    a frozen build lays data out next to the executable.
    """
    if getattr(sys, "frozen", False):
        bundled = os.path.join(os.path.dirname(sys.executable), "src", "migrations")
        if os.path.isdir(bundled):
            return bundled
    return os.path.dirname(os.path.abspath(__file__))


def discover():
    """Every migration on disk as (version, name, path), lowest version first."""
    directory = migrations_dir()
    found = []
    try:
        entries = os.listdir(directory)
    except OSError:
        logger.error("Migrations directory not found: %s", directory)
        return found
    for filename in entries:
        if filename == "__init__.py":
            continue
        m = _FILENAME_RE.match(filename)
        if m:
            found.append((int(m.group(1)), m.group(2), os.path.join(directory, filename)))
    found.sort(key=lambda row: row[0])
    return found


def ensure_table(conn):
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
                version    INTEGER PRIMARY KEY,
                name       TEXT    NOT NULL,
                applied_at TEXT    NOT NULL,
                statements INTEGER NOT NULL
            )"""
    )


def applied_versions(conn):
    """Versions already recorded against this journal."""
    try:
        return {r[0] for r in conn.execute(f"SELECT version FROM {MIGRATION_TABLE}")}
    except sqlite3.OperationalError:
        return set()


def schema_version(conn):
    """Highest applied migration version, or 0 for an unmigrated journal."""
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def split_statements(script):
    """Split a migration file into individually-executable statements.

    ``sqlite3.complete_statement`` tracks unterminated string literals and
    ``BEGIN … END`` blocks, so a trigger body with internal semicolons is kept
    whole. Comment-only fragments are dropped.
    """
    statements = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if _has_sql(statement):
                statements.append(statement)
            buffer = ""
    tail = buffer.strip()
    if _has_sql(tail):
        statements.append(tail)
    return statements


def _wants_foreign_keys_off(script):
    """True if the migration's header asks for ``-- pragma: foreign_keys=off``."""
    for raw in script.splitlines():
        line = raw.strip()
        if line and not line.startswith("--"):
            break                       # directives live in the header only
        if line.replace(" ", "").lower().startswith("--pragma:foreign_keys=off"):
            return True
    return False


def _has_sql(text):
    """True if the fragment contains anything other than comments/whitespace."""
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("--"):
            return True
    return False


def apply_pending(conn):
    """Apply every migration this journal has not seen yet, in order.

    Returns a :class:`MigrationReport`. Never raises: the caller must be able
    to open a journal that cannot be migrated.
    """
    report = MigrationReport()
    try:
        ensure_table(conn)
        conn.commit()
    except sqlite3.DatabaseError as e:
        report.failed = (0, "schema_migrations", str(e))
        logger.error("Could not create %s: %s", MIGRATION_TABLE, e)
        return report

    done = applied_versions(conn)
    for version, name, path in discover():
        if version in done:
            report.already_applied.append((version, name))
            continue
        is_python = path.endswith(".py")
        statements = []
        needs_fk_off = False
        if not is_python:
            try:
                with open(path, encoding="utf-8") as fh:
                    script = fh.read()
                needs_fk_off = _wants_foreign_keys_off(script)
                statements = split_statements(script)
            except OSError as e:
                report.failed = (version, name, f"unreadable: {e}")
                logger.error("Migration %03d_%s unreadable: %s", version, name, e)
                return report

        restore_fk = False
        try:
            if conn.in_transaction:
                conn.commit()
            if needs_fk_off:
                # Must be set outside a transaction; inside one it is ignored.
                restore_fk = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
                conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            if is_python:
                statements = [None] * _run_python_migration(path, conn)
            else:
                for statement in statements:
                    conn.execute(statement)
            if needs_fk_off:
                # Checked before the commit, so a rebuild that would leave
                # dangling references is rolled back rather than persisted.
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    tables = sorted({v[0] for v in violations})
                    raise RuntimeError(
                        f"{len(violations)} foreign-key violation(s) after rebuild "
                        f"in {', '.join(tables)}")
            conn.execute(
                f"INSERT INTO {MIGRATION_TABLE} (version, name, applied_at, statements) "
                f"VALUES (?, ?, ?, ?)",
                (version, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(statements)),
            )
            conn.commit()
        except Exception as e:      # noqa: BLE001 — a migration must never be fatal
            # Broad on purpose: a .py migration can raise anything, and the
            # guarantee is that a journal which cannot be migrated still opens.
            conn.rollback()
            report.failed = (version, name, str(e))
            logger.error("Migration %03d_%s rolled back: %s", version, name, e)
            return report          # stop at the first failure; later ones may depend on it
        finally:
            if restore_fk:
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                except sqlite3.DatabaseError:
                    logger.warning("Could not restore foreign-key enforcement", exc_info=True)

        report.applied.append((version, name))
        logger.info("Applied migration %03d_%s (%d statements)", version, name, len(statements))

    return report


def _run_python_migration(path, conn):
    """Import a ``NNN_name.py`` migration and run its ``apply(conn)``.

    Returns the number of rows it reports having changed, purely so
    ``schema_migrations`` records something meaningful. The module is loaded
    from its path rather than imported by name so a frozen build does not need
    it on the import path.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_migration_{os.path.basename(path)[:-3]}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "apply"):
        raise RuntimeError(f"{os.path.basename(path)} defines no apply(conn)")
    return module.apply(conn) or 0


def mark_all_applied(conn):
    """Record every migration on disk as applied, without running it.

    For a journal created directly from ``schema_bootstrap.sql``: that file is
    generated from the legacy path plus every migration, so by the time it has
    been applied the migrations *have* run — just not one at a time. Recording
    them keeps ``schema_version`` meaningful and stops them re-running on the
    next open.

    .. warning::
       This marks every migration on disk as applied, whether or not the
       bootstrap file actually contains its effects. **Adding a migration
       therefore requires regenerating the bootstrap**
       (``python -m tools.dump_schema``); without that, new journals stamp the
       migration as done and silently never run it. This has already happened
       once, to migration 012. ``tests/test_schema_current.py`` catches it by
       regenerating and comparing, which is why that test exists.

    The equivalence this relies on is asserted by
    ``tests/test_schema_current.py``, which builds a journal both ways and
    compares the schemas.
    """
    report = MigrationReport()
    try:
        ensure_table(conn)
        stamped = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        done = applied_versions(conn)
        for version, name, _path in discover():
            if version in done:
                report.already_applied.append((version, name))
                continue
            conn.execute(
                f"INSERT INTO {MIGRATION_TABLE} (version, name, applied_at, statements) "
                f"VALUES (?, ?, ?, 0)", (version, name, stamped))
            report.applied.append((version, name))
        conn.commit()
    except sqlite3.DatabaseError as e:
        conn.rollback()
        report.failed = (0, "schema_migrations", str(e))
        logger.error("Could not record the bootstrap migrations: %s", e)
    return report
