"""Snapshots and record counts behind the irreversible UI actions.

Deletion cascades since migration 004 — removing a member removes their ledger,
savings, both funds, enrolments and suspensions with them. That is right for
integrity and unforgiving of a mis-click, so the UI now states the real cost and
takes a snapshot first. These cover the parts that are not Qt.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


@pytest.fixture
def db():
    mgr = DatabaseManager(os.path.join(tempfile.mkdtemp(), "j.db"), auto_backup=False)
    yield mgr
    mgr.close()


def _busy_member(db):
    ind = db.add_individual("Busy", "07", "b@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                       "2025-01-01", "2025-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-001", 100000, 0, 100000, "issue")
    db.add_transaction(ind, "2025-02-01", "Repayment", "L-001", 0, 5750, 94250, "pmt",
                       principal_portion=5000, interest_portion=750)
    db.add_savings_transaction(ind, "2025-01-05", "Deposit", 2000, "dep")
    db.fund_add_transaction("christmas_savings", ind, "2025-01-06", "Deposit", 500)
    return ind


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #

def test_a_snapshot_is_a_readable_copy(db):
    _busy_member(db)
    path = db.snapshot("delete-member-1")
    try:
        assert os.path.exists(path)
        copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert copy.execute("SELECT COUNT(*) FROM individuals").fetchone()[0] == 1
        assert copy.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
        copy.close()
    finally:
        os.remove(path)


def test_a_snapshot_captures_uncheckpointed_work(db):
    """Under WAL the committed state is split between the .db and its sidecar,
    so a file copy can silently miss the most recent work. The backup API
    always produces a fully-checkpointed file."""
    ind = _busy_member(db)
    db.add_savings_transaction(ind, "2025-03-01", "Deposit", 999, "just now")
    path = db.snapshot("wal-check")
    try:
        copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        assert copy.execute(
            "SELECT COUNT(*) FROM savings WHERE amount=999").fetchone()[0] == 1
        copy.close()
    finally:
        os.remove(path)


def test_a_snapshot_lands_beside_the_journal(db):
    path = db.snapshot("tagged")
    try:
        assert os.path.basename(os.path.dirname(path)) == "backups"
        assert "pre-tagged-" in os.path.basename(path)
    finally:
        os.remove(path)


def test_a_snapshot_tag_cannot_escape_the_folder(db):
    """The tag reaches the filename from a loan ref, which is user data."""
    path = db.snapshot("../../etc/passwd")
    try:
        assert os.path.basename(os.path.dirname(path)) == "backups"
        assert ".." not in os.path.basename(path)
    finally:
        os.remove(path)


def test_an_in_memory_journal_has_nothing_to_snapshot():
    mgr = DatabaseManager(":memory:", auto_backup=False)
    try:
        assert mgr.snapshot("x") is None
    finally:
        mgr.close()


# --------------------------------------------------------------------------- #
# Telling the user what deletion costs
# --------------------------------------------------------------------------- #

def test_record_counts_describe_what_would_be_deleted(db):
    ind = _busy_member(db)
    counts = db.member_record_counts(ind)
    assert counts["loan ledger entries"] == 2
    assert counts["loans"] == 1
    assert counts["savings transactions"] == 1
    assert counts["Christmas savings entries"] == 1


def test_record_counts_omit_empty_tables(db):
    """So the confirmation lists what exists rather than a wall of zeroes."""
    ind = db.add_individual("Quiet", "07", "q@x")
    assert db.member_record_counts(ind) == {}


def test_the_counts_match_what_deletion_actually_removes(db):
    """The number in the dialog has to be the number that disappears."""
    ind = _busy_member(db)
    counts = db.member_record_counts(ind)
    db.delete_individual(ind)

    for table in ("ledger", "loans", "savings", "christmas_savings"):
        assert db.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE individual_id=?", (ind,)).fetchone()[0] == 0
    assert sum(counts.values()) == 5
    assert db.find_orphaned_rows() == []
