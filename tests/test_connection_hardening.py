"""Phase 1a — connection, transaction and backup hardening.

Covers the guarantees that protect a live journal: write-lock acquisition
order, atomic member deletion, WAL-safe backups, and the storage
classification that decides whether WAL may be used at all.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import BUSY_TIMEOUT_MS, JOURNAL_MODE_ENV, DatabaseManager
from src.exceptions import TransactionError


def _block_member_deletion(db):
    """Recreate `savings` referencing individuals with ON DELETE RESTRICT.

    Migration 004 made the cascade declarative, so a member delete now succeeds
    and clears the children. To test that a *failing* delete is atomic we need a
    child that refuses to go — the situation every pre-004 journal was in, where
    nothing cleared savings and the foreign key blocked the parent.
    """
    db.conn.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TRIGGER IF EXISTS trg_savings_created;
        ALTER TABLE savings RENAME TO savings_cascading;
        CREATE TABLE savings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            individual_id INTEGER NOT NULL REFERENCES individuals(id) ON DELETE RESTRICT,
            date TEXT, transaction_type TEXT, amount REAL, balance REAL, notes TEXT,
            import_id INTEGER, batch_id TEXT, created_at TEXT);
        INSERT INTO savings SELECT * FROM savings_cascading;
        DROP TABLE savings_cascading;
    """)
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")


def _block_loan_deletion(db):
    """Same idea for loans: a suspension row that refuses to cascade."""
    db.conn.executescript("""
        PRAGMA foreign_keys = OFF;
        ALTER TABLE loan_suspensions RENAME TO loan_suspensions_cascading;
        CREATE TABLE loan_suspensions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id INTEGER REFERENCES loans(id) ON DELETE RESTRICT,
            individual_id INTEGER, loan_ref TEXT, start_date TEXT, suspend_until TEXT,
            resumed_date TEXT, status TEXT, created_at TEXT);
        INSERT INTO loan_suspensions SELECT * FROM loan_suspensions_cascading;
        DROP TABLE loan_suspensions_cascading;
    """)
    db.conn.commit()
    db.conn.execute("PRAGMA foreign_keys = ON")


@pytest.fixture
def journal():
    """A file-backed journal (WAL only applies to real files)."""
    d = tempfile.mkdtemp()
    db = DatabaseManager(os.path.join(d, "j.db"))
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# Pragmas
# --------------------------------------------------------------------------- #

def test_busy_timeout_is_raised(journal):
    assert journal.conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS


def test_synchronous_is_full(journal):
    # 2 == FULL. A financial journal must not lose committed transactions.
    assert journal.conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_wal_enabled_on_local_storage(journal):
    """tmp is local, so the journal should have been put into WAL mode."""
    if journal._storage_kind() != "local":
        pytest.skip("test filesystem is not positively identified as local")
    assert journal.journal_mode == "wal"
    assert journal.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_memory_journal_never_uses_wal():
    db = DatabaseManager(":memory:")
    try:
        assert db._storage_kind() == "memory"
        assert db._select_journal_mode()[0] == "delete"
    finally:
        db.close()


def test_journal_mode_override_is_honoured(monkeypatch):
    monkeypatch.setenv(JOURNAL_MODE_ENV, "delete")
    d = tempfile.mkdtemp()
    db = DatabaseManager(os.path.join(d, "override.db"))
    try:
        assert db.journal_mode == "delete"
    finally:
        db.close()


def test_unknown_storage_falls_back_to_rollback_journal(journal, monkeypatch):
    """Anything we cannot positively identify must keep the pre-existing mode."""
    monkeypatch.setattr(journal, "_storage_kind", lambda: "unknown")
    mode, reason = journal._select_journal_mode()
    assert mode == "delete"
    assert "unsafe" in reason


def test_network_storage_falls_back_to_rollback_journal(journal, monkeypatch):
    monkeypatch.setattr(journal, "_storage_kind", lambda: "network")
    assert journal._select_journal_mode()[0] == "delete"


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #

def test_transaction_takes_the_write_lock_immediately(journal):
    """BEGIN IMMEDIATE, not a deferred BEGIN that must upgrade later."""
    other = sqlite3.connect(journal.db_name)
    other.execute("PRAGMA busy_timeout = 0")
    try:
        with journal.transaction():
            journal.conn.execute("INSERT INTO individuals (name) VALUES ('Locker')")
            # The write lock is already held, so a competing writer is refused.
            with pytest.raises(sqlite3.OperationalError, match="locked|busy"):
                other.execute("INSERT INTO individuals (name) VALUES ('Other')")
                other.commit()
    finally:
        other.close()


def test_nested_transaction_joins_the_outer_one(journal):
    """Composing two guarded operations must not raise, and must stay atomic."""
    with pytest.raises(ValueError):
        with journal.transaction():
            journal.conn.execute("INSERT INTO individuals (name) VALUES ('Outer')")
            with journal.transaction():          # nested — joins, does not commit
                journal.conn.execute("INSERT INTO individuals (name) VALUES ('Inner')")
            raise ValueError("boom")
    names = [r[0] for r in journal.conn.execute("SELECT name FROM individuals")]
    assert names == []          # the inner block did not commit independently


def test_transaction_rolls_back_on_error(journal):
    try:
        with journal.transaction():
            journal.conn.execute("INSERT INTO individuals (name) VALUES ('Doomed')")
            raise RuntimeError("fail")
    except RuntimeError:
        pass
    assert journal.conn.execute("SELECT COUNT(*) FROM individuals").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Atomic member deletion (the data-loss regression)
# --------------------------------------------------------------------------- #

def test_delete_individual_is_atomic_when_children_block_it(journal):
    """The regression: ledger rows must never survive their member's deletion
    failing, and must never be committed by a later unrelated write.

    Simulates a pre-cascade journal by dropping the cascade triggers, which is
    the state every existing installation is in before migration 001 runs.
    """
    ind = journal.add_individual("Blocked", "0700", "b@x.com")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, deducted, balance) "
                "VALUES (?, '2026-01-15', 'Repayment', 'L-001', 500, 1000)", (ind,))
    cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                "VALUES (?, '2026-01-15', 'Deposit', 250, 250)", (ind,))
    journal.conn.commit()

    _block_member_deletion(journal)

    # TransactionError wraps the underlying sqlite3.IntegrityError.
    with pytest.raises(TransactionError):
        journal.delete_individual(ind)

    # A perfectly ordinary later write must not commit a half-finished delete.
    journal.set_setting("unrelated", "1")

    assert journal.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=?", (ind,)).fetchone()[0] == 1
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM individuals WHERE id=?", (ind,)).fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #

def test_backup_to_captures_uncheckpointed_wal_content(journal):
    """A file copy would miss commits still living in the -wal sidecar."""
    ind = journal.add_individual("Recent", "0700", "r@x.com")
    dest = os.path.join(tempfile.mkdtemp(), "backup.db")
    journal.backup_to(dest)

    copy = sqlite3.connect(dest)
    try:
        assert copy.execute("SELECT COUNT(*) FROM individuals WHERE id=?", (ind,)).fetchone()[0] == 1
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()


def test_auto_backup_writes_into_backups_dir():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "auto.db")
    DatabaseManager(path).close()                       # create
    db = DatabaseManager(path, auto_backup=True)        # re-open -> snapshot
    try:
        backups = os.listdir(os.path.join(d, "backups"))
        assert any(f.startswith("auto.") and f.endswith(".db") for f in backups)
    finally:
        db.close()


def test_delete_loan_is_atomic(journal):
    """Same class of bug as delete_individual: two statements, one commit."""
    ind = journal.add_individual("Borrower", "0700", "b@x.com")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, balance, status, start_date) "
                "VALUES (?, 'L-001', 1000, 1000, 'Active', '2026-01-01')", (ind,))
    loan_pk = cur.lastrowid
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, balance) "
                "VALUES (?, '2026-01-01', 'Loan Issued', 'L-001', 1000, 1000)", (ind,))
    cur.execute("INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, status) "
                "VALUES (?, ?, 'L-001', '2026-02-01', 'active')", (loan_pk, ind))
    journal.conn.commit()

    _block_loan_deletion(journal)

    with pytest.raises(TransactionError):
        journal.delete_loan(ind, "L-001")
    journal.set_setting("unrelated", "1")        # must not commit a half-delete

    assert journal.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=?", (ind,)).fetchone()[0] == 1
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM loans WHERE individual_id=?", (ind,)).fetchone()[0] == 1
