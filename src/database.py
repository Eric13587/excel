"""Database management module for LoanMaster."""
import glob
import logging
import os
import sqlite3
import pandas as pd
import json
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

from src import migrations
from src.db import import_rules
from src.db import (AuditRepository, FundRepository, ImportHistoryRepository,
                    LedgerRepository, LoanRepository, MemberRepository,
                    RepairRepository, SettingsRepository, SourceJournal)
from src.exceptions import TransactionError
from src.data_structures import StatementData

# How many timestamped on-open backups to keep per journal.
BACKUP_KEEP_COUNT = 10

# How long a writer waits for a competing writer before giving up. Mass
# operations (catch-up over thousands of loans) and the report thread's second
# connection routinely hold locks for seconds, and the previous 5s ceiling
# surfaced as a hard failure mid-operation rather than as latency.
BUSY_TIMEOUT_MS = 30000

# Page cache (negative == KiB rather than pages). 64 MiB keeps the hot ledger
# and journal pages resident during recalculation loops.
CACHE_SIZE_KIB = -65536

# Cap the WAL file after a checkpoint so a large batch cannot leave a
# permanently inflated sidecar next to the journal.
WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

# Filesystems where SQLite's locking is unreliable. WAL additionally needs a
# shared-memory (-shm) mapping that these do not provide, so a journal hosted
# on one stays in rollback-journal mode.
NETWORK_FILESYSTEMS = frozenset({
    "nfs", "nfs4", "cifs", "smbfs", "smb2", "smb3", "afs", "ncpfs",
    "9p", "davfs", "fuse.davfs", "fuse.sshfs", "glusterfs", "ceph", "lustre",
})

# Override the journal-mode decision: "wal", "delete", or "auto" (default).
JOURNAL_MODE_ENV = "LOANMASTER_JOURNAL_MODE"


class DatabaseManager:
    """Owns the connection and composes the repository layer.

    This is the composition root the Phase 2 design called ``DatabaseService``.
    It was not introduced as a separate class: ``DatabaseManager`` already owns
    the connection, already provides the transaction boundary, and after Phase 2
    exposes exactly one repository per aggregate. A second class beside it would
    have duplicated that for a name, and renaming this one would have touched
    every call site for no functional gain.

    Repositories (each owns every statement touching its tables)::

        db.members    individuals
        db.loans      loans, loan_suspensions
        db.ledger     ledger
        db.funds      savings, christmas_savings, benevolent_ledger,
                      benevolent_accounts
        db.settings   settings
        db.imports    import_history
        db.audit      audit_log
        db.repair     cross-table orphan queries

    ``SourceJournal`` wraps a *foreign* journal read-only during an import.

    The methods on this class are the API the rest of the application uses;
    each one now delegates to a repository and supplies the transaction. The
    remaining SQL here is schema DDL in ``create_tables``, which
    ``schema.sql`` plus the migration runner will take over.

    Repositories never commit — the caller owns the transaction — so a
    multi-statement operation is atomic.
    """

    def __init__(self, db_name="loan_master.db", auto_backup=False, bootstrap="auto"):
        """Open (or create) a journal.

        ``bootstrap`` controls how a brand-new journal is built. "auto" applies
        the generated final schema directly; "legacy" forces the historical
        path — legacy table shapes plus every migration — which is how
        tools/dump_schema regenerates that schema without circularity.
        """
        self.db_name = db_name
        pre_existing = db_name != ":memory:" and os.path.exists(db_name)
        self.conn = sqlite3.connect(db_name)
        self._closed = False
        self.integrity_ok = True
        # Set by _configure_connection; declared here so callers can read them
        # even if configuration failed.
        self.journal_mode = "delete"
        self.fk_enforced = False
        self.fk_violations = 0
        # Snapshot the journal before create_tables() runs schema migrations,
        # so a bad migration can always be rolled back from the backup.
        if auto_backup and pre_existing:
            self._backup_on_open()
        if pre_existing:
            self._check_integrity()
        self._configure_connection()
        # Repository layer (Phase 2). DatabaseManager delegates to these as each
        # one lands, so its public API is unchanged for existing callers.
        self.settings = SettingsRepository(self.conn)
        self.members = MemberRepository(self.conn)
        self.funds = FundRepository(self.conn)
        self.loans = LoanRepository(self.conn)
        self.ledger = LedgerRepository(self.conn)
        self.repair = RepairRepository(self.conn)
        self.imports = ImportHistoryRepository(self.conn)
        self.audit = AuditRepository(self.conn)
        if bootstrap == "auto" and self._is_empty_journal():
            # A brand-new journal is built in its final shape. The legacy path
            # below creates the pre-migration tables and then has migration 004
            # rebuild all fourteen of them — 52% of the cost of creating a
            # journal, spent undoing work that was just done.
            self.migration_report = self._bootstrap_new_journal()
        else:
            self.create_tables()
            # Versioned migrations run after the legacy bootstrap, so an
            # existing journal converges on the same schema a new one is built
            # with. Failure is reported, never fatal: a journal that cannot be
            # migrated must still open so its data can be exported or repaired.
            self.migration_report = migrations.apply_pending(self.conn)
        if not self.migration_report.ok:
            logger.error("Schema migration incomplete — %s", self.migration_report.summary())
        # Cascading deletes may have just become available, and a repaired
        # journal may now qualify for enforcement.
        self._evaluate_foreign_keys()

    def _storage_kind(self):
        """Classify where the journal lives: 'memory', 'local', 'network' or
        'unknown'.

        Only a *positively identified* local filesystem is allowed to run in
        WAL mode. Anything unrecognised is treated as unsafe, so the fallback
        is always the mode this application already shipped with.
        """
        if self.db_name == ":memory:" or not self.db_name:
            return "memory"
        try:
            path = os.path.abspath(self.db_name)
        except Exception:
            return "unknown"

        # Windows: UNC paths and mapped network drives.
        if os.name == "nt":
            if path.startswith("\\\\"):
                return "network"
            try:
                import ctypes
                drive = os.path.splitdrive(path)[0]
                if drive:
                    # 3 == DRIVE_FIXED, 4 == DRIVE_REMOTE
                    kind = ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\")
                    if kind == 4:
                        return "network"
                    if kind == 3:
                        return "local"
            except Exception:
                logger.debug("Could not classify drive for %s", path, exc_info=True)
            return "unknown"

        # Linux: resolve the path to its mount point via /proc/mounts.
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                mounts = []
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 3:
                        mounts.append((parts[1], parts[2]))
        except OSError:
            return "unknown"

        best_point, best_type = "", None
        for point, fstype in mounts:
            if (path == point or path.startswith(point.rstrip("/") + "/")) \
                    and len(point) >= len(best_point):
                best_point, best_type = point, fstype
        if best_type is None:
            return "unknown"
        return "network" if best_type.lower() in NETWORK_FILESYSTEMS else "local"

    def _select_journal_mode(self):
        """Decide the journal mode, honouring an explicit operator override.

        WAL lets the report thread's second connection read while a mass
        operation writes; in rollback-journal mode those block each other.
        It is only safe on local storage, so 'auto' enables it exclusively
        there.
        """
        override = (os.environ.get(JOURNAL_MODE_ENV) or "auto").strip().lower()
        if override in ("wal", "delete"):
            return override, f"explicit {JOURNAL_MODE_ENV}={override}"
        if override != "auto":
            logger.warning("Ignoring unrecognised %s=%r", JOURNAL_MODE_ENV, override)

        kind = self._storage_kind()
        if kind == "local":
            return "wal", "local storage"
        if kind == "memory":
            return "delete", "in-memory journal"
        return "delete", f"{kind} storage (WAL unsafe)"

    def _configure_connection(self):
        """Apply per-connection safety and durability settings.

        - busy_timeout: wait instead of failing immediately when another
          process (this is a multi-user app) locks the journal.
        - foreign_keys: enforce the FKs the schema already declares. Left OFF
          only when the existing data would immediately violate them —
          enabling enforcement on top of legacy orphan rows makes unrelated
          writes start failing at runtime. That fallback is now reported
          through ``fk_enforced``/``fk_violations`` so the UI can surface it
          rather than only logging.
        - journal_mode: WAL on local storage (concurrent reader + writer,
          cheaper commits, faster crash recovery); rollback journal anywhere
          it is not provably safe. See _select_journal_mode.
        - synchronous FULL: a financial journal must not lose committed
          transactions on power loss.
        """
        cur = self.conn.cursor()
        cur.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")

        desired, reason = self._select_journal_mode()
        try:
            actual = cur.execute(f"PRAGMA journal_mode = {desired}").fetchone()[0]
        except sqlite3.DatabaseError as e:
            actual, = ("unknown",)
            logger.warning("Could not set journal_mode=%s: %s", desired, e)
        self.journal_mode = (actual or "").lower()
        if desired == "wal" and self.journal_mode != "wal":
            # SQLite refused (typically a filesystem without shared memory).
            # It has already fallen back on its own; record what we ended up with.
            logger.warning("WAL requested (%s) but journal is in %r mode",
                           reason, self.journal_mode)
        else:
            logger.info("journal_mode=%s (%s)", self.journal_mode, reason)

        cur.execute("PRAGMA synchronous = FULL")
        cur.execute(f"PRAGMA cache_size = {CACHE_SIZE_KIB}")
        cur.execute("PRAGMA temp_store = MEMORY")
        if self.journal_mode == "wal":
            cur.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES}")

        self._evaluate_foreign_keys()

    def _evaluate_foreign_keys(self):
        """Turn foreign-key enforcement on unless the data would break instantly.

        Split out from _configure_connection so it can be re-run cheaply after a
        repair or a migration without redoing the whole pragma set (the
        foreign_key_check itself is a full scan and should not be paid twice on
        open). PRAGMA foreign_keys is a no-op inside a transaction, so this
        refuses to guess when one is in flight.
        """
        if self.conn.in_transaction:
            logger.debug("Skipping foreign-key evaluation: transaction in flight")
            return self.fk_enforced
        cur = self.conn.cursor()
        try:
            violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError:
            violations = None
        self.fk_violations = len(violations) if violations else 0
        if violations:
            tables = sorted({v[0] for v in violations})
            cur.execute("PRAGMA foreign_keys = OFF")
            self.fk_enforced = False
            logger.warning(
                "Foreign-key enforcement left OFF: %d orphaned row(s) in %s. "
                "Run the Repair tools to fix legacy data.",
                len(violations), ", ".join(tables),
            )
        elif violations is not None:
            cur.execute("PRAGMA foreign_keys = ON")
            self.fk_enforced = bool(cur.execute("PRAGMA foreign_keys").fetchone()[0])
        else:
            self.fk_enforced = False
        return self.fk_enforced

    def _check_integrity(self):
        """Run SQLite's quick_check and record the result.

        Does not raise: even a damaged journal should still open so data can
        be exported, but the UI is expected to warn when integrity_ok is
        False.
        """
        try:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as e:
            logger.critical("Integrity check could not run on %s: %s", self.db_name, e)
            self.integrity_ok = False
            return
        if row and row[0] == "ok":
            logger.info("Integrity check passed for %s", self.db_name)
        else:
            logger.critical("Integrity check FAILED for %s: %s", self.db_name, row)
            self.integrity_ok = False

    def _backup_on_open(self, keep=BACKUP_KEEP_COUNT):
        """Copy the journal into <journal dir>/backups/, keeping the last N.

        Uses the SQLite online-backup API (safe against a live database).
        Backup failure is logged but never blocks opening the journal.
        """
        try:
            db_path = os.path.abspath(self.db_name)
            backup_dir = os.path.join(os.path.dirname(db_path), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            stem = os.path.splitext(os.path.basename(db_path))[0]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest_path = os.path.join(backup_dir, f"{stem}.{stamp}.db")
            if not os.path.exists(dest_path):
                self.backup_to(dest_path)
            old_backups = sorted(glob.glob(os.path.join(backup_dir, f"{stem}.*.db")))
            for stale in old_backups[:-keep]:
                os.remove(stale)
        except Exception:
            logger.exception("Automatic backup failed (continuing without it)")

    def backup_to(self, dest_path):
        """Write a consistent copy of the journal to dest_path.

        Uses SQLite's online-backup API rather than a file copy. Under WAL the
        committed state is split between the database file and its -wal
        sidecar, so copying only the .db silently loses every transaction that
        has not been checkpointed yet — a backup that looks fine and is
        missing the most recent work. The backup API always produces a single
        fully-checkpointed file, and is safe against concurrent writers.

        Returns the destination path.
        """
        dest = sqlite3.connect(dest_path)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        logger.info("Journal backed up to %s", dest_path)
        return dest_path

    # ------------------------------------------------------------------
    # Orphaned-record repair
    #
    # Legacy journals were written without foreign-key enforcement, so a
    # deleted member could leave transactions behind in the child tables.
    # Those orphans silently skew any aggregate that sums a child table
    # directly, and they keep FK enforcement disabled (see
    # _configure_connection). These methods power the guided repair tool.
    # ------------------------------------------------------------------

    def _tables_with_individual_id(self):
        """All tables (except individuals) that carry an individual_id column."""
        return self.repair.child_tables()

    def find_orphaned_rows(self):
        """Group child rows whose individual_id no longer exists.

        A NULL individual_id also counts as orphaned — no row in a child
        table is meaningful without a member.

        Returns a list of dicts:
            {table, individual_id, rows, first_date, last_date}
        (dates are None for tables without a date column).
        """
        summary = []
        for table in self.repair.child_tables():
            for ind_id, n, first, last in self.repair.orphan_groups(table):
                summary.append({"table": table, "individual_id": ind_id, "rows": n,
                                "first_date": first, "last_date": last})
        return summary

    def export_orphaned_rows(self, path):
        """Write an Excel audit workbook of every orphaned row.

        Sheet 'Summary' lists the per-table/per-member groups; every table
        with orphans gets its own sheet holding the complete rows. Returns
        the number of data rows exported (excluding the summary).
        """
        summary = self.find_orphaned_rows()
        total = 0
        with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
            pd.DataFrame(summary, columns=["table", "individual_id", "rows",
                                           "first_date", "last_date"]) \
                .to_excel(writer, sheet_name="Summary", index=False)
            for table in self.repair.child_tables():
                df = self.repair.orphan_frame(table)
                if not df.empty:
                    df.to_excel(writer, sheet_name=table[:31], index=False)
                    total += len(df)
        logger.info("Exported %d orphaned row(s) to %s", total, path)
        return total

    def delete_orphaned_rows(self):
        """Delete every orphaned row in a single transaction.

        Matches find_orphaned_rows() exactly (NULL individual_id included).
        Afterwards foreign-key enforcement is re-evaluated, so it switches on
        immediately if the journal is now consistent.
        Returns {table: rows_deleted} for tables that had orphans.
        """
        counts = {}
        with self.transaction():
            for table in self.repair.child_tables():
                removed = self.repair.delete_orphans(table)
                if removed:
                    counts[table] = removed
        logger.info("Deleted orphaned rows: %s", counts or "none")
        self._evaluate_foreign_keys()
        return counts

    def close(self):
        """Close the database connection.

        PRAGMA optimize refreshes the query planner's statistics for indexes
        whose selectivity has shifted during the session. Without it SQLite
        plans against stale (or absent) sqlite_stat1 data, which is how a
        journal that was fast at 2,000 ledger rows starts full-scanning at
        200,000. Best effort — never let it block a clean shutdown.
        """
        # getattr, not attribute access: if __init__ raised before setting
        # these, __del__ still calls close(), and an AttributeError raised
        # there is swallowed by the interpreter and printed as "Exception
        # ignored" — burying the real failure under noise.
        if getattr(self, "conn", None) and not getattr(self, "_closed", False):
            try:
                self.conn.execute("PRAGMA optimize")
            except sqlite3.DatabaseError:
                logger.debug("PRAGMA optimize failed on close", exc_info=True)
            self.conn.close()
            self._closed = True
    
    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        self.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with proper cleanup."""
        self.close()
        return False
    
    def begin_transaction(self):
        """Start a manual transaction."""
        # SQLite starts implicitly, but we can ensure previous are committed or just pass.
        # For strictness we could execute "BEGIN", but Python sqlite3 context handles this.
        # We'll rely on the fact that the next execute() starts a transaction.
        pass

    def commit_transaction(self):
        """Commit the current transaction."""
        self.conn.commit()

    def rollback_transaction(self):
        """Rollback the current transaction."""
        self.conn.rollback()

    @contextmanager
    def transaction(self):
        """Context manager for database transactions with automatic rollback on failure.

        Usage:
            with db.transaction():
                db.add_individual(...)
                db.add_loan_record(...)

        If any exception occurs, the transaction is rolled back.

        Opens with BEGIN IMMEDIATE, which takes the write lock up front. The
        default deferred BEGIN acquires a read lock and only tries to upgrade
        at the first write; if another connection took the write lock in
        between, SQLite returns SQLITE_BUSY *without consulting the busy
        handler* — because waiting there would deadlock. So busy_timeout does
        not protect a read-then-write transaction, and this application has
        exactly that shape (read the ledger, compute, write it back) plus a
        second connection opened by the report thread.

        Re-entrant: a nested call joins the transaction already in flight
        rather than failing on "cannot start a transaction within a
        transaction", so composing two guarded operations stays safe.
        """
        if self.conn.in_transaction:
            yield          # already inside a transaction; the outermost owns commit/rollback
            return
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            raise TransactionError(f"Transaction failed: {str(e)}") from e
        except Exception:
            self.conn.rollback()
            raise

    def _is_empty_journal(self):
        """True when this connection holds no user tables at all.

        File existence is not the test: several callers create the file with
        tempfile.mkstemp and hand over an empty one, and an empty file is a new
        journal however it came to exist.
        """
        return self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0] == 0

    def _bootstrap_new_journal(self):
        """Create a new journal directly in its final shape.

        Applies src/migrations/schema_bootstrap.sql — generated from the legacy
        path plus every migration, and verified by tests to be schema-identical
        to it — then records those migrations as applied, because they have
        been, by construction.

        Falls back to the legacy path if the file is missing or unusable. It is
        an optimisation; being unable to read it must not stop a journal
        opening.
        """
        path = os.path.join(migrations.migrations_dir(), "schema_bootstrap.sql")
        try:
            with open(path, encoding="utf-8") as fh:
                script = fh.read()
            self.conn.executescript(script)
            self.conn.commit()
        except (OSError, sqlite3.DatabaseError) as e:
            logger.warning("Could not bootstrap from %s (%s); using the legacy path",
                           path, e)
            self.conn.rollback()
            self.create_tables()
            return migrations.apply_pending(self.conn)

        self._seed_reference_data(self.conn.cursor())
        self.conn.commit()
        return migrations.mark_all_applied(self.conn)

    # Migrations that carry *data*, not schema. schema_bootstrap.sql is dumped
    # from sqlite_master, so it holds DDL only; anything a migration inserts has
    # to be applied separately when a journal takes the fast path. Getting this
    # wrong gives a journal the right shape and the wrong contents, which is why
    # test_a_new_journal_has_the_same_reference_data_as_a_migrated_one exists.
    _SEED_MIGRATIONS = ("002_seed_default_settings.sql",)

    def _seed_reference_data(self, cursor):
        """Rows a journal is unusable without. Idempotent.

        Shared by both bootstrap paths, so a new journal cannot end up with the
        schema but not the chart of accounts or the default settings.
        """
        for filename in self._SEED_MIGRATIONS:
            path = os.path.join(migrations.migrations_dir(), filename)
            try:
                with open(path, encoding="utf-8") as fh:
                    statements = migrations.split_statements(fh.read())
            except OSError:
                logger.warning("Seed migration %s is missing", filename)
                continue
            for statement in statements:
                cursor.execute(statement)

    def _general_ledger_removed(self):
        """True once migration 011 has withdrawn the general ledger."""
        try:
            return self.conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version >= 11 LIMIT 1"
            ).fetchone() is not None
        except sqlite3.OperationalError:
            return False        # pre-migration journal: nothing recorded yet

    def _create_legacy_gl_tables(self, cursor):
        """The general-ledger tables as migrations 001 and 004 expect them.

        Retained only so already-applied history runs unmodified on a new
        journal; migration 011 drops all of it a moment later.
        """
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS general_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, category TEXT, type TEXT, amount REAL,
                notes TEXT, batch_id TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chart_of_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                normal_balance TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                description TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                memo TEXT, source TEXT, source_ref TEXT,
                status TEXT DEFAULT 'posted',
                reversal_of INTEGER, created_at TEXT, created_by TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS journal_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                account_code TEXT NOT NULL,
                debit REAL DEFAULT 0, credit REAL DEFAULT 0, line_memo TEXT,
                FOREIGN KEY(entry_id) REFERENCES journal_entries(id),
                FOREIGN KEY(account_code) REFERENCES chart_of_accounts(code)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_entry "
                       "ON journal_lines(entry_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_journal_lines_account "
                       "ON journal_lines(account_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_journal_entries_source "
                       "ON journal_entries(source, source_ref)")

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS individuals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                email TEXT,
                default_deduction REAL DEFAULT 0,
                created_at TEXT,
                import_id INTEGER
            )
        """)
        # Migration for existing table
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN default_deduction REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN import_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # Retirement flag migration
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN is_retired INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN retired_date TEXT")
        except sqlite3.OperationalError:
            pass
        # Employment status + Provident Fund number
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN employment_status TEXT DEFAULT 'Active'")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN pf_no TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE individuals ADD COLUMN id_no TEXT")
        except sqlite3.OperationalError:
            pass
        # PF and ID numbers must be unique, but only when set (NULL/'' are exempt
        # so the many members without one don't collide).
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_individuals_pf_no "
                           "ON individuals(pf_no) WHERE pf_no IS NOT NULL AND pf_no != ''")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_individuals_id_no "
                           "ON individuals(id_no) WHERE id_no IS NOT NULL AND id_no != ''")
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                date TEXT,
                event_type TEXT,
                loan_id TEXT,
                added REAL,
                deducted REAL,
                balance REAL,
                notes TEXT,
                installment_amount REAL DEFAULT 0,
                batch_id TEXT,
                interest_amount REAL DEFAULT 0,
                principal_balance REAL DEFAULT 0,
                interest_balance REAL DEFAULT 0,
                gross_balance REAL DEFAULT 0,
                principal_portion REAL DEFAULT 0,
                interest_portion REAL DEFAULT 0,
                previous_state TEXT,
                is_edited INTEGER DEFAULT 0,
                import_id INTEGER,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        # Migrations for ledger
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN installment_amount REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass 
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_amount REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN is_edited INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN import_id INTEGER")
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                ref TEXT,
                principal REAL,
                total_amount REAL,
                balance REAL,
                installment REAL,
                start_date TEXT,
                next_due_date TEXT,
                status TEXT,
                monthly_interest REAL DEFAULT 0,
                unearned_interest REAL DEFAULT 0,
                interest_balance REAL DEFAULT 0,
                import_id INTEGER,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        # Migration for existing table
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN monthly_interest REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN unearned_interest REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN interest_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN import_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # Loan suspension migration
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN is_suspended INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE loans ADD COLUMN suspend_until TEXT")
        except sqlite3.OperationalError:
            pass

        # Loan suspension history (audit trail of suspension spans).
        # The is_suspended/suspend_until columns above hold only the *current*
        # state; this table preserves every suspension span so statements can
        # render "no deductions for months x, y, z" even after a loan resumes.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS loan_suspensions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                loan_id INTEGER,
                individual_id INTEGER,
                loan_ref TEXT,
                start_date TEXT,      -- date the suspension took effect
                suspend_until TEXT,   -- intended resume date (NULL = indefinite)
                resumed_date TEXT,    -- actual resume date (NULL = still active)
                status TEXT DEFAULT 'active',  -- 'active' | 'resumed'
                created_at TEXT,
                FOREIGN KEY(loan_id) REFERENCES loans(id),
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)

        # Back-fill: loans currently suspended under the old flag-only model have
        # no audit span. Create a best-effort open span (start ≈ next_due_date,
        # the point deductions paused) so their statements annotate too. The
        # NOT EXISTS guard makes this idempotent — it only fires once per loan.
        cursor.execute("""
            INSERT INTO loan_suspensions
                (loan_id, individual_id, loan_ref, start_date, suspend_until, resumed_date, status, created_at)
            SELECT l.id, l.individual_id, l.ref,
                   COALESCE(NULLIF(l.next_due_date, ''), date('now')),
                   l.suspend_until, NULL, 'active', datetime('now')
            FROM loans l
            WHERE l.is_suspended = 1
              AND NOT EXISTS (
                  SELECT 1 FROM loan_suspensions s
                  WHERE s.loan_id = l.id AND s.resumed_date IS NULL
              )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS savings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                date TEXT,
                transaction_type TEXT, -- Deposit, Withdrawal, Interest
                amount REAL,
                balance REAL,
                notes TEXT,
                import_id INTEGER,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        # Migration for existing table
        try:
            cursor.execute("ALTER TABLE savings ADD COLUMN import_id INTEGER")
        except sqlite3.OperationalError:
            pass
        # batch_id was only ever added by create_savings_table(), which runs
        # lazily from the savings read/write helpers — so a freshly created
        # journal had a `savings` table without it until the first savings
        # operation. The two definitions of this table disagreed; this makes the
        # canonical one authoritative.
        try:
            cursor.execute("ALTER TABLE savings ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass

        # ===== Christmas fund (a second savings pot, withdrawals lock outside
        # the unlock month) and Benevolent fund (perpetual welfare contribution
        # with a loan-like deduction schedule). Both share the savings-style
        # ledger shape and the generic fund_* DB methods. =====
        for fund_table in ("christmas_savings", "benevolent_ledger"):
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {fund_table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    individual_id INTEGER,
                    date TEXT,
                    transaction_type TEXT,  -- Deposit | Withdrawal | Contribution | Interest
                    amount REAL,
                    balance REAL,
                    notes TEXT,
                    batch_id TEXT,
                    FOREIGN KEY(individual_id) REFERENCES individuals(id)
                )
            """)
        # Per-member Benevolent enrolment + recurring-deduction schedule.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS benevolent_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER UNIQUE,
                monthly_amount REAL DEFAULT 0,
                start_date TEXT,
                next_due_date TEXT,
                active INTEGER DEFAULT 1,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
            
        # Import History Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS import_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                source_file TEXT,
                details TEXT,
                item_count INTEGER
            )
        """)

            
        # Migrations for Ledger Splits
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN principal_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN principal_portion REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN interest_portion REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        try:
             cursor.execute("ALTER TABLE ledger ADD COLUMN gross_balance REAL DEFAULT 0")
        except sqlite3.OperationalError:
             pass

        # State Management Migration
        try:
            cursor.execute("ALTER TABLE ledger ADD COLUMN previous_state TEXT")
        except sqlite3.OperationalError:
            pass

        # Settings Table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        
        # ----------------------------------------------------------------- #
        # General-ledger tables, created only so the already-applied history
        # in migrations 001 and 004 can run over them; 011 drops them again.
        #
        # Skipped once 011 has run. create_tables executes on *every* open, so
        # without this guard each reopen would resurrect the tables that
        # migration had just dropped — and 011, already recorded as applied,
        # would never run again to clear them.
        # ----------------------------------------------------------------- #
        if not self._general_ledger_removed():
            self._create_legacy_gl_tables(cursor)


        self._seed_reference_data(cursor)

        # Normalise a legacy corruption where an edited repayment stored the
        # payment AMOUNT in is_edited (e.g. 5857) instead of the flag 1. The
        # recalculator already treats any nonzero as "edited", so collapsing to 1
        # changes no behaviour — it just restores the column to its 0/1 domain.
        try:
            cursor.execute("UPDATE ledger SET is_edited=1 WHERE is_edited NOT IN (0, 1)")
        except sqlite3.OperationalError:
            pass

        self._create_audit_infrastructure(cursor)

        self.conn.commit()

    def _create_audit_infrastructure(self, cursor):
        """created_at/updated_at timestamps + an audit_log trail, maintained by
        triggers so every write is captured regardless of code path.

        Timestamp triggers touch only their own row (cheap). The audit_log
        records entity-lifecycle events for individuals (all CRUD) and loans
        (create/delete) — deliberately NOT loan UPDATEs or ledger rows, which
        churn on every recalculation and would flood the log; the ledger itself
        already carries dated transaction history.
        """
        # 1. Timestamp columns (nullable; populated by triggers).
        for table, cols in (
            ("individuals", ["updated_at"]),          # created_at already exists
            ("loans", ["created_at", "updated_at"]),
            ("savings", ["created_at"]),
            ("christmas_savings", ["created_at"]),
            ("benevolent_ledger", ["created_at"]),
        ):
            for col in cols:
                try:
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

        # 2. Audit log table.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                operation TEXT,        -- INSERT | UPDATE | DELETE
                entity TEXT,           -- individual | loan
                entity_id INTEGER,
                summary TEXT
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id)")

        # 3. Triggers (recursive_triggers is OFF by default, so these never cascade).
        trigger_sql = [
            # created_at on insert (only if the caller didn't set one)
            """CREATE TRIGGER IF NOT EXISTS trg_loans_created AFTER INSERT ON loans
               WHEN NEW.created_at IS NULL BEGIN
                 UPDATE loans SET created_at=datetime('now') WHERE id=NEW.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_savings_created AFTER INSERT ON savings
               WHEN NEW.created_at IS NULL BEGIN
                 UPDATE savings SET created_at=datetime('now') WHERE id=NEW.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_xmas_created AFTER INSERT ON christmas_savings
               WHEN NEW.created_at IS NULL BEGIN
                 UPDATE christmas_savings SET created_at=datetime('now') WHERE id=NEW.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_benev_created AFTER INSERT ON benevolent_ledger
               WHEN NEW.created_at IS NULL BEGIN
                 UPDATE benevolent_ledger SET created_at=datetime('now') WHERE id=NEW.id; END""",
            # updated_at on update
            """CREATE TRIGGER IF NOT EXISTS trg_individuals_updated AFTER UPDATE ON individuals
               BEGIN UPDATE individuals SET updated_at=datetime('now') WHERE id=NEW.id; END""",
            """CREATE TRIGGER IF NOT EXISTS trg_loans_updated AFTER UPDATE ON loans
               BEGIN UPDATE loans SET updated_at=datetime('now') WHERE id=NEW.id; END""",
            # audit_log: individuals (all CRUD)
            """CREATE TRIGGER IF NOT EXISTS trg_audit_ind_ins AFTER INSERT ON individuals BEGIN
                 INSERT INTO audit_log(ts,operation,entity,entity_id,summary)
                 VALUES (datetime('now'),'INSERT','individual',NEW.id,NEW.name); END""",
            # Only meaningful identity edits — not default_deduction/updated_at churn.
            """CREATE TRIGGER IF NOT EXISTS trg_audit_ind_upd AFTER UPDATE ON individuals
               WHEN NEW.name IS NOT OLD.name OR NEW.phone IS NOT OLD.phone
                 OR NEW.email IS NOT OLD.email OR NEW.pf_no IS NOT OLD.pf_no
                 OR NEW.id_no IS NOT OLD.id_no
                 OR NEW.employment_status IS NOT OLD.employment_status
                 OR NEW.is_retired IS NOT OLD.is_retired BEGIN
                 INSERT INTO audit_log(ts,operation,entity,entity_id,summary)
                 VALUES (datetime('now'),'UPDATE','individual',NEW.id,NEW.name); END""",
            """CREATE TRIGGER IF NOT EXISTS trg_audit_ind_del AFTER DELETE ON individuals BEGIN
                 INSERT INTO audit_log(ts,operation,entity,entity_id,summary)
                 VALUES (datetime('now'),'DELETE','individual',OLD.id,OLD.name); END""",
            # audit_log: loans (create/delete only — UPDATEs churn on recalc)
            """CREATE TRIGGER IF NOT EXISTS trg_audit_loan_ins AFTER INSERT ON loans BEGIN
                 INSERT INTO audit_log(ts,operation,entity,entity_id,summary)
                 VALUES (datetime('now'),'INSERT','loan',NEW.id,NEW.ref); END""",
            """CREATE TRIGGER IF NOT EXISTS trg_audit_loan_del AFTER DELETE ON loans BEGIN
                 INSERT INTO audit_log(ts,operation,entity,entity_id,summary)
                 VALUES (datetime('now'),'DELETE','loan',OLD.id,OLD.ref); END""",
        ]
        for sql in trigger_sql:
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass

    def get_audit_log(self, limit=500, entity=None, entity_id=None):
        """Recent audit entries (most recent first)."""
        return self.audit.recent(limit, entity, entity_id)

    # ------------------------------------------------------------------
    # Individual operations — delegated to MemberRepository (Phase 2b).
    # These signatures and return shapes are unchanged; ten call sites index
    # get_individuals() positionally, so the raw-tuple shape is load-bearing.
    # ------------------------------------------------------------------

    def add_individual(self, name, phone, email, default_deduction=0,
                       employment_status='Active', pf_no='', id_no=''):
        with self.transaction():
            return self.members.add(name, phone, email, default_deduction,
                                    employment_status, pf_no, id_no)

    def individual_name_exists(self, name):
        """Check if an individual with the given name already exists (case-insensitive)."""
        return self.members.name_exists(name)

    def pf_no_owner(self, pf_no, exclude_id=None):
        """Return the name of the individual already using this PF number, or None."""
        return self.members.owner_of_pf_no(pf_no, exclude_id)

    def id_no_owner(self, id_no, exclude_id=None):
        """Return the name of the individual already using this ID number, or None."""
        return self.members.owner_of_id_no(id_no, exclude_id)

    def get_individuals(self):
        return self.members.rows()

    def get_individual_name(self, id):
        name = self.members.name_of(id)
        return name if name is not None else f"Individual {id}"

    def get_individual(self, id):
        return self.members.get(id)

    def update_individual(self, id, name, phone, email,
                          employment_status=None, pf_no=None, id_no=None):
        # Only touch the optional fields when supplied, so callers that pass
        # just the core details don't clobber an existing status / PF / ID.
        with self.transaction():
            self.members.update_core(id, name, phone, email)
            if employment_status is not None:
                self.members.set_employment_status(id, employment_status)
            if pf_no is not None:
                self.members.set_pf_no(id, pf_no)
            if id_no is not None:
                self.members.set_id_no(id, id_no)

    def update_individual_deduction(self, id, amount):
        with self.transaction():
            self.members.set_default_deduction(id, amount)

    def snapshot(self, tag):
        """Back the journal up before an irreversible change. Returns the path.

        Deletion cascades since migration 004 — removing a member removes their
        ledger, savings, both funds, enrolments and suspensions with them, which
        is right for integrity and unforgiving of a mis-click. A snapshot turns
        that from unrecoverable into a restore.

        Raises OSError or sqlite3.Error if it cannot be written; callers must
        decide whether to proceed, and the UI does not.
        """
        if not self.db_name or self.db_name == ":memory:":
            return None
        folder = os.path.join(os.path.dirname(os.path.abspath(self.db_name)), "backups")
        stem = os.path.splitext(os.path.basename(self.db_name))[0]
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(tag))[:60]
        os.makedirs(folder, exist_ok=True)
        # The stamp is only second-resolution, so two snapshots in the same
        # second would collide — and backup_to overwrites its destination. That
        # is not merely untidy: restoring twice in one second wrote the safety
        # copy over the very file being restored from, destroying it. Never
        # reuse a path.
        base = os.path.join(
            folder, f"{stem}.pre-{safe}-{datetime.now():%Y%m%d-%H%M%S}")
        dest, suffix = f"{base}.db", 1
        while os.path.exists(dest):
            dest = f"{base}-{suffix}.db"
            suffix += 1
        return self.backup_to(dest)

    # Tables a file must have before it is credible as a journal of this app.
    _JOURNAL_MARKER_TABLES = ("individuals", "loans", "ledger")

    def inspect_backup(self, path):
        """Describe a candidate backup without touching the live journal.

        Returns a dict with ``ok``, ``problem`` and, when readable, the row
        counts and schema version — enough for a restore screen to show what a
        file contains and for :meth:`restore_from` to refuse a bad one.

        Opened read-only, so inspecting a file can never modify it.
        """
        info = {"path": path, "ok": False, "problem": None, "counts": {},
                "schema_version": None}
        if not os.path.exists(path):
            info["problem"] = "The file does not exist."
            return info
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as e:
            info["problem"] = f"Cannot open the file: {e}"
            return info
        try:
            check = src.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                info["problem"] = f"The file is damaged: {check}"
                return info
            present = {r[0] for r in src.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in self._JOURNAL_MARKER_TABLES if t not in present]
            if missing:
                info["problem"] = ("This does not look like a LoanMaster journal "
                                   f"(missing {', '.join(missing)}).")
                return info
            for table in ("individuals", "loans", "ledger", "savings"):
                if table in present:
                    info["counts"][table] = src.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            info["schema_version"] = migrations.schema_version(src)
            info["ok"] = True
            return info
        except sqlite3.DatabaseError as e:
            info["problem"] = f"Could not read the file: {e}"
            return info
        finally:
            src.close()

    def restore_from(self, path):
        """Replace this journal's contents with a backup's. Returns a report dict.

        Two things make this safe enough to offer in the UI:

        * the source is inspected first and a file that is damaged, unreadable
          or not a journal is refused before anything is touched;
        * the current state is snapshotted first, so restoring is itself
          reversible — the commonest way to lose data with a restore feature is
          restoring the wrong file.

        The copy goes through SQLite's backup API *into the open connection*
        rather than replacing the file. That avoids the whole class of problems
        around overwriting a database that is currently open (which simply
        fails on Windows), and leaves every repository still pointing at a
        valid connection.

        A backup may predate the current schema, so migrations are re-applied
        afterwards and their report is returned with the rest.
        """
        info = self.inspect_backup(path)
        if not info["ok"]:
            raise ValueError(info["problem"])

        safety = self.snapshot("pre-restore")
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            src.backup(self.conn)
        finally:
            src.close()

        # The restored contents may be on an older schema than the code.
        report = migrations.apply_pending(self.conn)
        self._check_integrity()
        self._evaluate_foreign_keys()
        logger.warning("Journal restored from %s (safety copy: %s)", path, safety)
        return {"restored_from": path, "safety_copy": safety,
                "counts": info["counts"], "migration_report": report,
                "integrity_ok": self.integrity_ok}

    def available_backups(self):
        """Every backup beside this journal, newest first.

        Covers both the rotation written on open and the snapshots taken before
        an irreversible change; they share the folder and differ only in name.
        """
        if not self.db_name or self.db_name == ":memory:":
            return []
        folder = os.path.join(os.path.dirname(os.path.abspath(self.db_name)), "backups")
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        found = []
        for name in names:
            if not name.endswith(".db"):
                continue
            full = os.path.join(folder, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            found.append({"path": full, "name": name, "size": stat.st_size,
                          "modified": datetime.fromtimestamp(stat.st_mtime)})
        found.sort(key=lambda b: b["modified"], reverse=True)
        return found

    def member_record_counts(self, individual_id):
        """What deleting this member would remove, per table.

        Only non-empty tables, so a confirmation can state the real cost rather
        than "and all their records".
        """
        counts = {}
        for table, label in (("ledger", "loan ledger entries"),
                             ("loans", "loans"),
                             ("savings", "savings transactions"),
                             ("christmas_savings", "Christmas savings entries"),
                             ("benevolent_ledger", "benevolent entries"),
                             ("benevolent_accounts", "benevolent enrolments"),
                             ("loan_suspensions", "suspension records")):
            try:
                n = self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE individual_id=?",
                    (individual_id,)).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            if n:
                counts[label] = n
        return counts

    def delete_individual(self, id):
        """Delete a member and their records as a single atomic operation.

        The two statements were previously issued without a transaction. Once
        foreign-key enforcement engaged (which happens automatically as soon as
        a journal has no orphaned rows), the second began failing — the member
        still has savings, fund, enrolment and suspension rows that no code path
        removed — while the first had already succeeded. The exception
        propagated with the transaction still open, so the *next* unrelated
        write committed the half-delete: the member's entire loan ledger
        destroyed, the member still listed. Reproduced against a copy of a
        production journal before this change.

        Migration 001 additionally installs cascade triggers so the remaining
        child tables are cleared with the parent instead of being left behind.
        """
        with self.transaction():
            self.members.delete(id)

    def get_statement_data(self, individual_id, start_date=None, end_date=None) -> StatementData:
        """Fetch all data required for statement generation in one go."""
        individual = self.get_individual(individual_id)
        # Fetch FULL history for accurate running balance calculation
        ledger_df = self.get_ledger(individual_id) 
        # Savings history also needed fully if we want accurate running balances, 
        # but let's stick to ledger fix first. Actually, savings row balance works by snapshot usually? 
        # Let's fetch full savings too to be safe for running balance if needed.
        # But UI savings table shows just current balance? 
        # Verify get_savings_transactions implementation first? 
        # Let's just fetch full ledger as planned for Gross Balance.
        savings_df = self.get_savings_transactions(individual_id)
        savings_balance = self.get_savings_balance(individual_id)
        active_loans = self.get_active_loans(individual_id)
        loan_suspensions = self.get_loan_suspensions(individual_id)

        return StatementData(
            individual=individual,
            ledger_df=ledger_df,
            savings_df=savings_df,
            savings_balance=savings_balance,
            active_loans=active_loans,
            loan_suspensions=loan_suspensions
        )

    def get_earliest_record_date(self, individual_id):
        """Get the earliest transaction date for an individual across ledger and savings."""
        return self.ledger.earliest_date_for_members([individual_id])

    def get_earliest_record_date_for_ids(self, individual_ids):
        """Get the earliest transaction date across multiple individuals."""
        return self.ledger.earliest_date_for_members(list(individual_ids))

    # Ledger operations
    def get_ledger(self, individual_id, start_date=None, end_date=None):
        return self.ledger.for_member(individual_id, start_date, end_date)

    def get_transaction(self, trans_id):
        return self.ledger.get(trans_id)

    def add_transaction(self, individual_id, date, event_type, loan_id, added, deducted, balance, notes,
                       installment_amount=0, interest_amount=0, batch_id=None,
                       principal_balance=0, interest_balance=0, principal_portion=0, interest_portion=0,
                       previous_state=None):
        with self.transaction():
            new_id = self.ledger.add((
                individual_id, date, event_type, loan_id, added, deducted, balance, notes,
                installment_amount, interest_amount, batch_id,
                principal_balance, interest_balance, principal_portion, interest_portion,
                previous_state))
        return new_id

    def bulk_insert_transactions(self, transactions):
        """Bulk insert multiple transactions into the ledger."""
        if not transactions:
            return

        rows = [tuple(tx.get(col, default) for col, default in (
            ("individual_id", None), ("date", None), ("event_type", None), ("loan_id", None),
            ("added", 0), ("deducted", 0), ("balance", 0), ("notes", ""),
            ("installment_amount", 0), ("interest_amount", 0), ("batch_id", None),
            ("principal_balance", 0), ("interest_balance", 0),
            ("principal_portion", 0), ("interest_portion", 0), ("previous_state", None)))
            for tx in transactions]

        with self.transaction():
            self.ledger.add_many(rows)

    def update_transaction(self, id, date, added, deducted, notes, principal_portion=None,
                           interest_portion=None, mark_edited=False, interest_amount=None):
        """Edit a ledger row, then re-derive everything that follows from it.

        The principal/interest split and the running balances are *derived*, so
        changing an amount without recomputing them leaves the row asserting a
        split that belonged to a different payment. Every recalculator then
        replayed that stale split, which is how a repayment edited from 5,750 to
        9,000 kept reducing principal by 5,000.

        When the caller does not supply a split, the old one is cleared and the
        derivation works it out. When the caller does supply one it is an
        assertion and is kept.
        """
        from src.services.loan_projection import clear_derived, project_loan

        row = self.conn.execute(
            "SELECT individual_id, loan_id FROM ledger WHERE id=?", (id,)).fetchone()
        with self.transaction():
            self.ledger.update_core(id, date, added, deducted, notes, principal_portion,
                                    interest_portion, mark_edited, interest_amount)
            if principal_portion is None and interest_portion is None:
                clear_derived(self, id)
            if row and row[1]:
                project_loan(self, row[0], row[1], commit=False)

    def update_balance(self, id, balance):
        with self.transaction():
            self.ledger.set_balance(id, balance)

    def update_ledger_balances(self, id, balance, principal_bal, interest_bal, gross_bal=0):
        """Update all three balance types for a ledger entry."""
        with self.transaction():
            self.ledger.set_running_balances(id, balance, principal_bal, interest_bal, gross_bal)

    def delete_transaction(self, id):
        with self.transaction():
            self.ledger.delete(id)

    # Loan operations
    def add_loan_record(self, individual_id, ref, principal, total, balance, installment,
                        monthly_interest, start_date, next_due_date, unearned_interest=0,
                        duration_months=None, interest_rate=None):
        with self.transaction():
            self.loans.add(individual_id, ref, principal, total, balance, installment,
                           monthly_interest, start_date, next_due_date, unearned_interest,
                           duration_months, interest_rate)

    def get_active_loans(self, individual_id):
        return self.loans.active_for_member(individual_id)

    def get_loans(self, individual_id):
        """Get ALL loans (Active and Paid) for an individual."""
        return self.loans.for_member(individual_id)

    def get_all_active_loans(self):
        return self.loans.all_active()

    def get_overdue_count(self):
        return self.loans.overdue_count(datetime.now().strftime("%Y-%m-%d"))

    def get_loan_by_ref(self, individual_id, ref):
        return self.loans.by_ref(individual_id, ref)

    def update_loan_status(self, loan_id, balance, next_due_date, status,
                           interest_balance=None, unearned_interest=None):
        with self.transaction():
            self.loans.set_status(loan_id, balance, next_due_date, status,
                                  interest_balance, unearned_interest)

    def update_loan_recalc_state(self, loan_id, monthly_interest, unearned_interest):
        """Update loan terms derived from history replay."""
        with self.transaction():
            self.loans.set_recalc_state(loan_id, monthly_interest, unearned_interest)

    def unlock_future_interest(self, individual_id, loan_ref, date_str):
        """Reset is_edited flag for future interest rows, allowing re-simulation.

        Scoped to one member — loan refs (L-001) are not unique across individuals.
        """
        with self.transaction():
            self.ledger.set_interest_lock(individual_id, loan_ref, locked=False,
                                          from_date=date_str)

    def update_loan_details(self, loan_id, total_amount, balance, installment, monthly_interest,
                            next_due_date, unearned_interest=None, principal_update=None,
                            interest_balance=None):
        with self.transaction():
            self.loans.set_details(loan_id, total_amount, balance, installment, monthly_interest,
                                   next_due_date, unearned_interest, principal_update,
                                   interest_balance)

    def delete_loan(self, individual_id, loan_ref):
        """Delete a loan and its transactions as a single atomic operation.

        Same shape as the delete_individual bug fixed in Phase 1a: two
        statements with one trailing commit, so a failure on the second (a
        suspension row still referencing the loan on a pre-migration journal,
        say) left the ledger delete pending, to be committed by the next
        unrelated write. Scoped by individual_id because loan refs are unique
        per member only.
        """
        with self.transaction():
            self.loans.delete_with_ledger(individual_id, loan_ref)

    # ========== LOAN SUSPENSION OPERATIONS ==========

    def suspend_loan(self, loan_id, until_date=None, start_date=None):
        """Suspend deductions for a loan and record the suspension span.

        Args:
            loan_id: The loan's primary key ID.
            until_date: Optional YYYY-MM-DD date string. If None, suspended indefinitely.
            start_date: Optional YYYY-MM-DD date the suspension takes effect.
                Defaults to today.
        """
        if start_date is None:
            start_date = datetime.now().strftime("%Y-%m-%d")
        with self.transaction():
            self.loans.set_suspended(loan_id, until_date)
            loan_ref, individual_id = self.loans.identity(loan_id)
            # Re-suspending or extending an already-suspended loan updates the
            # open span in place rather than creating a duplicate. Only the
            # intended resume date moves; the original start_date is preserved
            # so the recorded span still covers the full skipped period.
            existing = self.loans.open_suspension_id(loan_id)
            if existing:
                self.loans.extend_suspension(existing, until_date)
            else:
                self.loans.add_suspension(loan_id, individual_id, loan_ref,
                                          start_date, until_date)

    def resume_loan(self, loan_id, resumed_date=None):
        """Resume deductions for a suspended loan and close its suspension span.

        Args:
            loan_id: The loan's primary key ID.
            resumed_date: Optional YYYY-MM-DD date deductions effectively resume.
                Defaults to today. Auto-resume during catch-up passes the
                intended ``suspend_until`` boundary so the recorded span reflects
                the real skipped period rather than when the job happened to run.
        """
        if resumed_date is None:
            resumed_date = datetime.now().strftime("%Y-%m-%d")
        with self.transaction():
            self.loans.clear_suspended(loan_id)
            self.loans.close_open_suspension(loan_id, resumed_date)

    def get_loan_suspensions(self, individual_id):
        """Return all recorded suspension spans for an individual.

        Returns:
            List of dicts with keys: id, loan_id, individual_id, loan_ref,
            start_date, suspend_until, resumed_date, status.
        """
        return self.loans.suspensions_for_member(individual_id)

    def get_loan_suspensions_for_loan(self, loan_id):
        """Every recorded suspension span for one loan, earliest first."""
        return self.loans.suspensions_for_loan(loan_id)

    def delete_suspension(self, suspension_id):
        """Remove a recorded suspension span.

        Not the same as resuming. Resuming says the suspension *ended*, which
        leaves its months permanently skipped; deleting says it never applied,
        so catch-up will deduct those months again. There was previously no way
        to do this at all — a backdated span leaves ``is_suspended`` clear, so
        no Resume button ever appears for it and the record was permanent.

        If the deleted span was the live one, the loan's suspended flag is
        cleared with it; otherwise the flag belongs to a different span and is
        left alone. Returns the deleted row, or None if it did not exist.
        """
        span = self.loans.suspension(suspension_id)
        if span is None:
            return None
        with self.transaction():
            was_open = self.loans.open_suspension_id(span["loan_id"]) == suspension_id
            self.loans.delete_suspension(suspension_id)
            if was_open:
                self.loans.clear_suspended(span["loan_id"])
        return span

    def record_suspension(self, loan_id, start_date, end_date, today=None):
        """Record a suspension span anywhere in time (past, present or future).

        If the span has already ended (``end_date`` on or before today) it is
        stored as a *closed historical* record only — the loan's live
        ``is_suspended`` flag is left untouched, because the loan is not
        suspended now. Otherwise the loan is suspended live until ``end_date``
        (exactly like ``suspend_loan``), honouring the backdated ``start_date``.

        Returns 'historical' or 'active' so callers can tailor their message.
        """
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")

        if end_date and end_date <= today:
            with self.transaction():
                loan_ref, individual_id = self.loans.identity(loan_id)
                self.loans.add_suspension(loan_id, individual_id, loan_ref, start_date,
                                          end_date, resumed_date=end_date, status='resumed')
            return 'historical'

        self.suspend_loan(loan_id, until_date=end_date, start_date=start_date)
        return 'active'

    def count_deductions_in_period(self, individual_id, loan_ref, start_date, end_date):
        """Count repayment rows for one member's loan within [start_date, end_date].

        Scoped to individual_id — loan refs (L-001) are not unique across members.
        Used to warn before recording a 'no-deductions' suspension over a window
        that already has deductions (which would contradict the statement).
        """
        return self.ledger.count_events(individual_id, loan_ref,
                                       ("Repayment", "Loan Buyoff"), start_date, end_date)

    # ========== RETIREMENT OPERATIONS ==========

    def set_membership_status(self, ind_id, status, effective_date=None):
        """Record that a member has left the SACCO — or come back.

        The one supported way to change membership, because three columns have
        to move together: `employment_status` (what the policy rules read),
        `is_retired` (what the reports still read) and `retired_date`. Migration
        004 constrains the last two to agree, and that CHECK is evaluated before
        any AFTER trigger can mirror a value, so a bare UPDATE of one column is
        rejected rather than quietly half-applied.

        Args:
            ind_id: the member.
            status: one of :data:`src.services.policy.MEMBERSHIP_STATUSES`.
            effective_date: when it took effect, ``YYYY-MM-DD``. Required for
                every status except Active; defaults to today.
        """
        from src.services.policy import ACTIVE, MEMBERSHIP_STATUSES
        if status not in MEMBERSHIP_STATUSES:
            raise ValueError(f"{status!r} is not a membership status "
                             f"({', '.join(MEMBERSHIP_STATUSES)})")
        if status == ACTIVE:
            with self.transaction():
                self.conn.execute(
                    "UPDATE individuals SET employment_status=?, is_retired=0, "
                    "retired_date=NULL WHERE id=?", (ACTIVE, ind_id))
            return
        when = effective_date or datetime.now().strftime("%Y-%m-%d")
        with self.transaction():
            self.conn.execute(
                "UPDATE individuals SET employment_status=?, is_retired=1, "
                "retired_date=? WHERE id=?", (status, when, ind_id))

    def membership_status(self, ind_id):
        """``(status, effective_date)``; status is 'Active' when nothing is set."""
        from src.services.policy import ACTIVE, RETIRED
        row = self.conn.execute(
            "SELECT employment_status, is_retired, retired_date FROM individuals "
            "WHERE id=?", (ind_id,)).fetchone()
        if row is None:
            return (None, None)
        status, is_retired, when = row
        return (status or (RETIRED if is_retired else ACTIVE), when)

    def retire_individual(self, ind_id, date_str):
        """Mark an individual as retired.

        Kept for existing callers; delegates to set_membership_status so the
        three columns cannot drift apart.

        Args:
            ind_id: The individual's ID.
            date_str: Retirement date in YYYY-MM-DD format.
        """
        from src.services.policy import RETIRED
        self.set_membership_status(ind_id, RETIRED, date_str)

    def reinstate_individual(self, ind_id):
        """Return a member to active membership."""
        from src.services.policy import ACTIVE
        self.set_membership_status(ind_id, ACTIVE)
        return

    def _legacy_reinstate(self, ind_id):
        with self.transaction():
            self.members.reinstate(ind_id)

    def has_outstanding_loans(self, ind_id):
        """Check if an individual has any active (outstanding) loans."""
        return self.loans.has_active(ind_id)

    def delete_batch(self, batch_id):
        """Delete all transactions associated with a batch_id from ledger."""
        with self.transaction():
            self.ledger.delete_batch(batch_id)

    def delete_savings_batch(self, batch_id):
        """Delete all transactions associated with a batch_id from savings."""
        with self.transaction():
            self.funds.delete_batch("savings", batch_id)

    # ========== SAVINGS OPERATIONS ==========
    
    def create_savings_table(self):
        """Create savings table if not exists."""
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS savings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                individual_id INTEGER,
                date TEXT,
                transaction_type TEXT,
                amount REAL,
                balance REAL,
                notes TEXT,
                batch_id TEXT,
                FOREIGN KEY(individual_id) REFERENCES individuals(id)
            )
        """)
        try:
            cursor.execute("ALTER TABLE savings ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()
    
    def add_savings_transaction(self, individual_id, date, transaction_type, amount, notes="", batch_id=None):
        """Add a deposit, withdrawal or interest credit to savings.

        Goes through src.services.posting, which is also what the Christmas and
        Benevolent funds use. This path used to have its own sign rule —
        anything that was not a 'Deposit' reduced the balance — so an Interest
        credit subtracted, disagreeing with FundRepository and with the other
        two funds. Returns the member's new balance.
        """
        self.create_savings_table()
        return self._post_fund("savings", individual_id, date, transaction_type,
                               amount, notes, batch_id)
    
    def get_savings_balance(self, individual_id):
        """Get current savings balance for an individual."""
        self.create_savings_table()
        return self.funds.balance("savings", individual_id)

    def recalculate_savings_balance(self, individual_id, cursor=None):
        """Recalculate the stored running balance for a member's savings.

        Delegates to the single implementation in FundRepository. The optional
        cursor lets the import path fold this into its own transaction — which
        is why this method does not commit when one is supplied.
        """
        if cursor is not None:
            self.funds.recalculate("savings", individual_id, cursor=cursor)
            return
        with self.transaction():
            self.funds.recalculate("savings", individual_id)

    def _mutate_fund(self, table, trans_id, change):
        """Apply a change to one fund row, then re-derive that member's balances.

        The `balance` column is a *running total*, so it is a property of the
        whole ordered series, not of the row holding it. Editing an amount,
        moving a date, or deleting a row invalidates every row from that point
        on — and the API used to leave that to the caller ("caller recalculates
        afterwards"), which meant the column was only correct where somebody had
        remembered. It was not correct: an edited middle row kept its old total,
        and a row moved to an earlier date carried its old total to the top of
        the table.

        Recalculating inside the same transaction makes it structural instead of
        remembered.
        """
        row = self.funds.get(table, trans_id)
        if row is None:
            return
        member_id = row["individual_id"]
        with self.transaction():
            change()
            self.funds.recalculate(table, member_id)

    def _is_back_dated(self, table, individual_id, date):
        """True when this row is not the member's latest in that fund.

        Cheap enough to ask on every insert: one indexed MAX over one member.
        """
        t = self.funds._table(table)
        latest = self.conn.execute(
            f"SELECT MAX(date) FROM {t} WHERE individual_id=?", (individual_id,)).fetchone()[0]
        return latest is not None and str(date) < str(latest)

    def get_savings_transactions(self, individual_id, start_date=None, end_date=None):
        """A member's savings history, in the order it happened.

        Ordered by date rather than by insertion, so a back-dated deposit shows
        where it belongs instead of at the bottom of the table. `christmas` and
        `benevolent` already read this way; savings was the outlier.
        """
        self.create_savings_table()
        return self.funds.transactions("savings", individual_id, start_date, end_date,
                                       order="date, id")

    def delete_savings_transaction(self, trans_id):
        """Delete a savings transaction and re-derive the running balances."""
        self._mutate_fund("savings", trans_id,
                          lambda: self.funds.delete("savings", trans_id))

    def update_savings_transaction(self, trans_id, new_date, new_amount, new_notes):
        """Edit a savings transaction and re-derive the running balances."""
        self._mutate_fund("savings", trans_id,
                          lambda: self.funds.update("savings", trans_id, new_date,
                                                    new_amount, new_notes))

    def recalculate_savings_balances(self, individual_id):
        """Recalculate running balances for savings.

        Retained as the name the ledger view calls; identical to
        recalculate_savings_balance since Phase 3 collapsed the two. It used to
        order by id alone and treat any non-Deposit row as a decrease, which is
        what corrupted two members' balances (finding D1).
        """
        self.create_savings_table()
        with self.transaction():
            self.funds.recalculate("savings", individual_id)

    # ========== GENERIC FUND LEDGER (Savings / Christmas / Benevolent) ==========
    # Delegated to FundRepository, which whitelists the table name.
    _FUND_TABLES = ("christmas_savings", "benevolent_ledger")

    def fund_balance(self, table, individual_id):
        return self.funds.balance(table, individual_id)

    def fund_add_transaction(self, table, individual_id, date, transaction_type, amount,
                             notes="", batch_id=None):
        """Append a fund transaction. Withdrawals reduce the balance; everything
        else (Deposit/Contribution/Interest) increases it.

        Returns the member's new balance.
        """
        return self._post_fund(table, individual_id, date, transaction_type,
                               amount, notes, batch_id)

    def _post_fund(self, table, individual_id, date, transaction_type, amount,
                   notes="", batch_id=None):
        """The one write path for all three funds.

        Normalise, validate, check membership, insert, re-derive the running
        totals — see src/services/posting.py for why each stage is there.
        """
        from src.services.posting import Posting, post
        return post(self, Posting(table=table, member_id=individual_id, date=date,
                                  kind=transaction_type, amount=amount,
                                  notes=notes, batch_id=batch_id))

    def fund_transactions(self, table, individual_id):
        return self.funds.transactions(table, individual_id)

    def fund_recalculate(self, table, individual_id):
        with self.transaction():
            self.funds.recalculate(table, individual_id)

    def fund_delete_transaction(self, table, trans_id):
        """Delete a fund transaction and re-derive the running balances."""
        self._mutate_fund(table, trans_id, lambda: self.funds.delete(table, trans_id))

    def fund_delete_batch(self, table, batch_id):
        """Delete a whole batch and re-derive every affected member's balances."""
        members = self.funds.members_in_batch(table, batch_id)
        with self.transaction():
            self.funds.delete_batch(table, batch_id)
            for member_id in members:
                self.funds.recalculate(table, member_id)

    def fund_delete_all(self, table, individual_id):
        with self.transaction():
            self.funds.delete_all_for_member(table, individual_id)

    def fund_get_transaction(self, table, trans_id):
        return self.funds.get(table, trans_id)

    def fund_update_transaction(self, table, trans_id, date, amount, notes):
        """Edit a fund transaction and re-derive the running balances."""
        self._mutate_fund(table, trans_id,
                          lambda: self.funds.update(table, trans_id, date, amount, notes))

    # ========== BENEVOLENT ENROLMENT ==========

    def get_benevolent_account(self, individual_id):
        return self.funds.get_enrolment(individual_id)

    def upsert_benevolent_account(self, individual_id, monthly_amount, start_date, next_due_date):
        with self.transaction():
            self.funds.upsert_enrolment(individual_id, monthly_amount, start_date, next_due_date)

    def set_benevolent_next_due(self, individual_id, next_due_date):
        with self.transaction():
            self.funds.set_next_due(individual_id, next_due_date)

    def get_setting(self, key, default=None):
        """Get a setting value (delegates to SettingsRepository).

        Retained as the public API every caller already uses; prefer the typed
        accessors on ``db.settings`` in new code.
        """
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        """Set a setting value (delegates to SettingsRepository)."""
        with self.transaction():
            self.settings.set(key, value)

    def import_individuals_from_external_db(self, source_db_path):
        """
        Import individuals (empty shells) from another SQLite database.
        Skips individuals that already exist (by Name).
        Returns the number of imported records, or -1 when the source has no
        usable individuals table.
        """
        try:
            with SourceJournal(source_db_path) as src:
                try:
                    rows = src.basic_members()
                except sqlite3.OperationalError:
                    return -1

            existing = set(self.members.names_to_ids())
            imported = 0
            with self.transaction():
                for row in rows:
                    name = row["name"]
                    if name in existing:
                        continue
                    self.members.add(name, row["phone"] or "", row["email"] or "",
                                     row["default_deduction"] or 0)
                    existing.add(name)
                    imported += 1
            return imported
        except Exception:
            logger.exception("Import error")
            return 0

    def get_import_preview(self, source_db_path):
        """
        Connect to source DB and return list of individuals for preview.
        Returns: list of dicts [{'id': 1, 'name': '...', 'phone': '...', 'email': '...'}]
        """
        try:
            with SourceJournal(source_db_path) as src:
                return src.member_summaries()
        except Exception:
            return None

    def check_import_conflicts(self, source_db_path, selected_ids):
        """
        Check for potential duplicates between source and destination DBs.

        Args:
            source_db_path: Path to source DB.
            selected_ids: List of source individual IDs to check.

        Returns:
            List of conflict dicts:
            [
                {
                    "src": {"id": 1, "name": "John", "phone": "123", "email": "a@b.com"},
                    "matches": [
                        {"id": 5, "name": "john", "phone": "123", "email": "a@b.com", "reason": "..."}
                    ]
                }, ...
            ]
        """
        conflicts = []
        try:
            with SourceJournal(source_db_path) as src:
                for src_ind in src.member_summaries(selected_ids):
                    matches = []
                    seen = {}

                    def note(candidates, reason, matches=matches, seen=seen):
                        for m in candidates:
                            if m["id"] in seen:
                                if reason not in seen[m["id"]]["reason"]:
                                    seen[m["id"]]["reason"] += f", {reason}"
                                continue
                            entry = {**m, "reason": reason}
                            seen[m["id"]] = entry
                            matches.append(entry)

                    note(self.members.match_by_name(src_ind["name"]),
                         "Name (Case-insensitive)")
                    if src_ind["phone"]:
                        note(self.members.match_by_phone(src_ind["phone"]), "Phone Match")
                    if src_ind["email"]:
                        note(self.members.match_by_email(src_ind["email"]), "Email Match")

                    if matches:
                        conflicts.append({"src": src_ind, "matches": matches})
        except Exception:
            logger.exception("Error checking import conflicts")
            return []
        return conflicts

    def generate_import_preview(self, source_db_path, selected_ids, options):
        """
        Generate a preview of the import operation.
        
        Returns:
            dict: {
                "summary": { "individuals_new": 0, "individuals_merged": 0, "conflicts": 0, "loans": 0, "ledger": 0, "savings": 0 },
                "conflicts": [], # List of conflict objects from check_import_conflicts
                "details": { "new_names": [], "merged_names": [] }
            }
        """
        preview = {
            "summary": {"individuals_new": 0, "individuals_merged": 0, "conflicts": 0, "loans": 0, "ledger": 0, "savings": 0, "loans_renamed": 0},
            "conflicts": [],
            "details": {"new_names": [], "merged_names": [], "loan_renames": []}
        }
        
        if not selected_ids:
            return preview

        try:
            # 1. Get All Potential Conflicts first
            raw_conflicts = self.check_import_conflicts(source_db_path, selected_ids)
            
            # 2. Filter Conflicts: Separate Exact Matches (Auto-Merge) from Real Conflicts
            real_conflicts = []
            auto_merged_ids = set()
            
            for c in raw_conflicts:
                src = c['src']
                matches = c['matches']
                
                # Check for Exact Match Condition
                if len(matches) == 1 and matches[0]['name'] == src['name']:
                    # Exact name match -> Auto Merge
                    preview["summary"]["individuals_merged"] += 1
                    preview["details"]["merged_names"].append(src['name'])
                    auto_merged_ids.add(src['id'])
                else:
                    # Ambiguous or non-exact match -> Conflict
                    real_conflicts.append(c)
            
            preview["conflicts"] = real_conflicts
            preview["summary"]["conflicts"] = len(real_conflicts)
            
            # IDs that are either real conflicts OR auto-merged
            processed_ids = {c['src']['id'] for c in real_conflicts}.union(auto_merged_ids)
            
            # 3. Existing loan refs, for collision detection
            existing_loan_refs = self.loans.all_refs()

            # 4. Source details for the remaining new individuals and their loans
            with SourceJournal(source_db_path) as src:
                for sid, name in src.member_names(selected_ids):
                    # Count related data for every selected member
                    if options.get("import_loans"):
                        refs = src.loan_refs(sid)
                        preview["summary"]["loans"] += len(refs)
                        for ref in refs:
                            if ref in existing_loan_refs:
                                preview["summary"]["loans_renamed"] += 1
                                if len(preview["details"]["loan_renames"]) < 10:
                                    preview["details"]["loan_renames"].append(ref)
                        preview["summary"]["ledger"] += src.count("ledger", sid)

                    if options.get("import_savings"):
                        preview["summary"]["savings"] += src.count("savings", sid)

                    # Categorize as New if not processed
                    if sid not in processed_ids:
                        preview["summary"]["individuals_new"] += 1
                        preview["details"]["new_names"].append(name)
            
        except Exception:
            logger.exception("Import preview error")
            return None
            
        return preview

    def _import_aux_tables(self, src, id_map, loan_id_map, options):
        """Import Christmas/Benevolent funds and loan suspensions for the members
        already mapped in id_map. Writes through the repositories on the shared
        connection and never commits, so it composes with the caller's manual
        import transaction. Returns a stats dict.
        """
        stats = {"christmas": 0, "benevolent": 0, "benevolent_accounts": 0, "suspensions": 0}
        if not id_map:
            return stats

        if options.get("import_funds", False):
            for table, key in (("christmas_savings", "christmas"),
                               ("benevolent_ledger", "benevolent")):
                if not src.has_table(table):
                    continue
                cols, rows = src.all_rows(table)
                affected = set()
                for tup in rows:
                    row = dict(zip(cols, tup))
                    new_id = id_map.get(row.get('individual_id'))
                    if new_id is None:
                        continue
                    self.funds.add_imported(
                        table, new_id, row.get('date'), row.get('transaction_type'),
                        row.get('amount'), row.get('balance'), row.get('notes'),
                        row.get('batch_id'))
                    affected.add(new_id)
                    stats[key] += 1
                for member_id in affected:
                    self.funds.recalculate(table, member_id)

            if src.has_table("benevolent_accounts"):
                cols, rows = src.all_rows("benevolent_accounts")
                for tup in rows:
                    row = dict(zip(cols, tup))
                    new_id = id_map.get(row.get('individual_id'))
                    if new_id is None:
                        continue
                    self.funds.replace_enrolment(
                        new_id, row.get('monthly_amount', 0) or 0, row.get('start_date'),
                        row.get('next_due_date'), row.get('active', 1))
                    stats["benevolent_accounts"] += 1

        if options.get("import_loans", False) and src.has_table("loan_suspensions"):
            cols, rows = src.all_rows("loan_suspensions")
            for tup in rows:
                row = dict(zip(cols, tup))
                new_id = id_map.get(row.get('individual_id'))
                if new_id is None:
                    continue
                old_ref = row.get('loan_ref')
                new_ref = (loan_id_map.get(old_ref, old_ref) if old_ref else old_ref)
                new_loan_id = self.loans.id_for_ref(new_id, new_ref) if new_ref else None
                self.loans.add_suspension(
                    new_loan_id, new_id, new_ref, row.get('start_date'),
                    row.get('suspend_until'), row.get('resumed_date'),
                    row.get('status', 'active'), created_at=row.get('created_at'))
                stats["suspensions"] += 1

        return stats

    def import_selected_data(self, source_db_path, selected_ids, options,
                             progress_callback=None, decision_map=None):
        """
        Import ONLY selected individuals from external database with granular checkpoints.

        Args:
            source_db_path: Path to source DB
            selected_ids: List of individual IDs to import
            options: Dict with import options
            progress_callback: Callable(current, total, message)
            decision_map: Dict {src_id: "new" | "skip" | int(dest_id)}

        Returns a dict:
        {
            "status": "success" | "partial" | "failed",
            "stats": { "individuals": 0, "loans": 0, ... },
            "errors": ["Error message 1", ...]
        }

        NOTE ON THE SAVEPOINT POLICY. Each phase takes a savepoint on success,
        and phase 2 rolls back to ``individuals_imported`` on failure so members
        survive a loan failure. Phase 3 does **not** roll back: a savings
        failure leaves whatever rows it managed to write, and relies on the user
        undoing the whole run instead. That gap is inherited, is documented in
        the code it replaced, and is preserved here — deciding what a partial
        savings import should do is a business call, not a refactoring one.
        """
        stats = {"individuals": 0, "loans": 0, "ledger": 0, "savings": 0, "rejected": 0}
        errors = []
        status = "success"
        # Bound before the try: the original assigned it *inside*, so a failure
        # while opening the source raised NameError from the handler and masked
        # the real error.
        original_isolation = self.conn.isolation_level
        src = None

        try:
            src = SourceJournal(source_db_path)

            # Pre-check table creation to avoid DDL inside the transaction
            if options.get("import_savings", False):
                self.create_savings_table()

            # --- PHASE 1: INDIVIDUALS ---
            if not selected_ids:
                return {"status": "success", "stats": stats, "errors": []}
            try:
                src_inds = src.members(selected_ids)
            except sqlite3.OperationalError as e:
                src.close()
                return {"status": "failed", "stats": stats, "errors": [f"Source DB Error: {e}"]}

            # Start Manual Transaction
            self.conn.isolation_level = None
            dest_cur = self.conn.cursor()
            dest_cur.execute("BEGIN")
            dest_cur.execute("SAVEPOINT import_start")

            import_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            import_id = self.imports.start(import_timestamp, source_db_path)

            id_map = {}        # Source member id -> destination member id
            loan_id_map = {}   # Source loan ref  -> destination loan ref

            existing_inds = self.members.names_to_ids()
            # Track PF/ID numbers already in use so an imported member with a
            # colliding number doesn't violate the unique indexes (we drop the
            # clashing field for that member instead of failing the whole import).
            existing_pf = self.members.used_pf_numbers()
            existing_id = self.members.used_id_numbers()

            # Estimate total steps for the progress bar
            loan_count = ledger_count = savings_count = 0
            if options.get("import_loans", False):
                loan_count = src.count("loans")
                ledger_count = src.count("ledger")
            if options.get("import_savings", False):
                savings_count = src.count("savings")

            total_operations = len(src_inds) + loan_count + ledger_count + savings_count
            current_op = 0

            try:
                for src_ind in src_inds:
                    if progress_callback:
                        progress_callback(current_op, total_operations,
                                          f"Importing Individual: {src_ind['name']}")

                    name = src_ind['name']
                    src_id = src_ind['id']

                    # Determine Action
                    action = "new"
                    if decision_map and src_id in decision_map:
                        action = decision_map[src_id]
                    elif name in existing_inds:
                        action = existing_inds[name]        # merge by exact name

                    if action == "skip":
                        current_op += 1
                        continue

                    if isinstance(action, int):
                        id_map[src_id] = action             # merge with existing
                    else:
                        keys = src_ind.keys()
                        norm = import_rules.normalise_member(
                            {k: src_ind[k] for k in keys})
                        errors.extend(norm.warnings)
                        if norm.rejected:
                            errors.append(f"Skipped a member: {norm.reason}")
                            stats["rejected"] += 1
                            current_op += 1
                            continue
                        v = norm.values
                        name = v["name"]
                        pf_no, id_no = v["pf_no"], v["id_no"]

                        # Drop a colliding PF/ID rather than fail the import.
                        if pf_no and pf_no in existing_pf:
                            errors.append(f"PF '{pf_no}' ({name}) already in use — imported without PF No.")
                            pf_no = ''
                        if id_no and id_no in existing_id:
                            errors.append(f"ID '{id_no}' ({name}) already in use — imported without ID No.")
                            id_no = ''

                        new_id = self.members.add_imported(
                            name, v["phone"], v["email"], v["default_deduction"],
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            v["employment_status"], pf_no, id_no,
                            v["is_retired"], v["retired_date"], import_id)
                        id_map[src_id] = new_id
                        if pf_no:
                            existing_pf.add(pf_no)
                        if id_no:
                            existing_id.add(id_no)
                        stats["individuals"] += 1

                    current_op += 1

                dest_cur.execute("SAVEPOINT individuals_imported")

            except Exception:
                dest_cur.execute("ROLLBACK TO import_start")
                self.conn.commit()      # Nothing happened effectively

            # --- PHASE 2: LOANS & LEDGER ---
            if options.get("import_loans", False):
                try:
                    loan_id_map = {}
                    existing_loan_refs = self.loans.all_refs()

                    for ln in src.loans(list(id_map.keys())):
                        if progress_callback:
                            current_op += 1
                            if current_op % 5 == 0:
                                progress_callback(current_op, total_operations, "Importing Loans...")

                        src_ind_id = ln['individual_id']
                        if src_ind_id not in id_map:
                            continue
                        dest_ind_id = id_map[src_ind_id]

                        # Handle ref collision by auto-renaming
                        original_ref = ln['ref']
                        new_ref = original_ref
                        if new_ref in existing_loan_refs:
                            suffix_idx = 0
                            base_suffix = "-Import"
                            while new_ref in existing_loan_refs:
                                suffix = base_suffix if suffix_idx == 0 else f"{base_suffix}-{suffix_idx}"
                                new_ref = f"{original_ref}{suffix}"
                                suffix_idx += 1
                        existing_loan_refs.add(new_ref)
                        loan_id_map[original_ref] = new_ref

                        norm = import_rules.normalise_loan(
                            {k: ln[k] for k in ln.keys()},
                            fallback_start=src.min_ledger_date(src_ind_id, original_ref))
                        errors.extend(norm.warnings)
                        if norm.rejected:
                            errors.append(f"Skipped a loan: {norm.reason}")
                            stats["rejected"] += 1
                            existing_loan_refs.discard(new_ref)
                            loan_id_map.pop(original_ref, None)
                            continue
                        v = norm.values
                        self.loans.add_imported(
                            dest_ind_id, new_ref, v['principal'], v['total_amount'],
                            v['balance'], v['installment'], v['start_date'],
                            v['next_due_date'], v['status'], v['monthly_interest'],
                            v['unearned_interest'], v['interest_balance'], import_id)
                        stats["loans"] += 1

                    # --- Ledger ---
                    try:
                        cols, src_entries = src.ledger(list(id_map.keys()),
                                                       options.get("date_range"))
                        for entry_tuple in src_entries:
                            if progress_callback:
                                current_op += 1
                                if current_op % 10 == 0:
                                    progress_callback(current_op, total_operations, "Importing Ledger...")

                            entry = dict(zip(cols, entry_tuple))
                            src_ind_id = entry['individual_id']
                            if src_ind_id not in id_map:
                                continue
                            dest_ind_id = id_map[src_ind_id]

                            norm = import_rules.normalise_ledger(entry)
                            errors.extend(norm.warnings)
                            if norm.rejected:
                                errors.append(f"Skipped a transaction: {norm.reason}")
                                stats["rejected"] += 1
                                continue
                            v = norm.values

                            # `loan_id` holds the loan REFERENCE string, so it is
                            # remapped alongside any rename above.
                            old_ref = v['loan_id']
                            new_ref = loan_id_map.get(old_ref, old_ref) if old_ref else old_ref

                            self.ledger.add_imported((
                                dest_ind_id, v['date'], v['event_type'], new_ref,
                                v['added'], v['deducted'], v['balance'], v['notes'],
                                v['installment_amount'], v['batch_id'], v['interest_amount'],
                                v['principal_balance'], v['interest_balance'],
                                v['principal_portion'], v['interest_portion'],
                                v['previous_state'], v['is_edited'], import_id))
                            stats["ledger"] += 1

                    except sqlite3.OperationalError:
                        pass    # Ledger may be missing or differently shaped in very old backups

                    dest_cur.execute("SAVEPOINT loans_imported")

                except Exception as e:
                    # Partial failure: roll back loans but KEEP individuals
                    dest_cur.execute("ROLLBACK TO individuals_imported")
                    status = "partial"
                    errors.append(f"Failed to import loans/ledger: {e}")
                    stats["loans"] = 0
                    stats["ledger"] = 0
                    logger.exception("Import error (loans)")

            # --- PHASE 3: SAVINGS ---
            if options.get("import_savings", False):
                try:
                    savings_affected_ids = set()
                    if src.has_table("savings"):
                        cols, src_savings = src.savings(options.get("date_range"))
                        for sav_tuple in src_savings:
                            if progress_callback:
                                current_op += 1
                                if current_op % 5 == 0:
                                    progress_callback(current_op, total_operations, "Importing Savings...")

                            sav = dict(zip(cols, sav_tuple))
                            old_ind_id = sav['individual_id']
                            if old_ind_id not in id_map:
                                continue
                            new_ind_id = id_map[old_ind_id]

                            norm = import_rules.normalise_fund_row(sav)
                            errors.extend(norm.warnings)
                            if norm.rejected:
                                errors.append(f"Skipped a savings row: {norm.reason}")
                                stats["rejected"] += 1
                                continue
                            v = norm.values

                            self.funds.add_imported_savings(
                                new_ind_id, v['date'], v['transaction_type'],
                                v['amount'], v['balance'], v['notes'], import_id)
                            savings_affected_ids.add(new_ind_id)
                            stats["savings"] += 1

                        if savings_affected_ids:
                            if progress_callback:
                                progress_callback(current_op, total_operations,
                                                  "Recalculating Savings Balances...")
                            # Same connection, so the recalculation sees the rows
                            # written above even though nothing is committed yet.
                            for ind_id in savings_affected_ids:
                                self.recalculate_savings_balance(ind_id, cursor=dest_cur)

                        dest_cur.execute("SAVEPOINT savings_imported")

                except Exception as e:
                    # No rollback here — see the savepoint-policy note in the
                    # docstring. Whatever was written stays, and the user is
                    # expected to undo the whole run if the result looks wrong.
                    status = "partial"
                    errors.append(f"Failed to import savings: {e}")
                    stats["savings"] = 0
                    logger.exception("Import error (savings)")

            # --- PHASE 4: FUNDS & SUSPENSIONS (best-effort) ---
            try:
                if progress_callback:
                    progress_callback(current_op, total_operations, "Importing funds & suspensions...")
                stats.update(self._import_aux_tables(src, id_map, loan_id_map, options))
                dest_cur.execute("SAVEPOINT aux_imported")
            except Exception as e:
                status = "partial"
                errors.append(f"Failed to import funds/suspensions: {e}")
                logger.exception("Import error (aux tables)")

            self.imports.finish(import_id, json.dumps(stats), total_operations)

            # Final commit — release the initial savepoint and commit
            dest_cur.execute("RELEASE import_start")
            self.conn.commit()
            src.close()
            self.conn.isolation_level = original_isolation

        except Exception as e:
            if src:
                src.close()
            self.conn.rollback()
            self.conn.isolation_level = original_isolation
            logger.exception("Critical import error")
            return {"status": "failed", "stats": stats, "errors": [str(e)]}

        return {
            "status": status,
            "stats": stats,
            "errors": errors,
            "import_id": import_id
        }

    def get_import_history(self):
        """Fetch all import history records."""
        return self.imports.all()

    def undo_import(self, import_id):
        """
        Revert an import operation by deleting all records associated with the given import_id.

        Only rows the import *created* carry an import_id, so a member who
        already existed and was merged into is never removed.

        Args:
            import_id (int): ID of the import batch to undo.

        Returns:
            bool: True if successful, False otherwise.
        """
        if not import_id:
            return False
        try:
            with self.transaction():
                self.imports.undo(import_id)
            return True
        except Exception:
            logger.exception("Undo import error")
            return False

    def validate_source_schema(self, source_path):
        """
        Validates that the source file is a valid SQLite database and has the required schema.
        Returns: (is_valid, error_message)
        """
        return SourceJournal.validate(source_path)

