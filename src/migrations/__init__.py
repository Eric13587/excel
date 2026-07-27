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
_FILENAME_RE = re.compile(r"^(\d{3,})_(.+)\.sql$")


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
        try:
            with open(path, encoding="utf-8") as fh:
                statements = split_statements(fh.read())
        except OSError as e:
            report.failed = (version, name, f"unreadable: {e}")
            logger.error("Migration %03d_%s unreadable: %s", version, name, e)
            return report

        try:
            if conn.in_transaction:
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                f"INSERT INTO {MIGRATION_TABLE} (version, name, applied_at, statements) "
                f"VALUES (?, ?, ?, ?)",
                (version, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(statements)),
            )
            conn.commit()
        except sqlite3.DatabaseError as e:
            conn.rollback()
            report.failed = (version, name, str(e))
            logger.error("Migration %03d_%s rolled back: %s", version, name, e)
            return report          # stop at the first failure; later ones may depend on it

        report.applied.append((version, name))
        logger.info("Applied migration %03d_%s (%d statements)", version, name, len(statements))

    return report
