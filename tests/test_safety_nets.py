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


# --------------------------------------------------------------------------- #
# Restore — the other half of a snapshot
# --------------------------------------------------------------------------- #

def test_a_backup_can_be_inspected_without_touching_it(db):
    ind = _busy_member(db)
    path = db.snapshot("inspect")
    try:
        before = os.path.getmtime(path)
        info = db.inspect_backup(path)
        assert info["ok"] and info["problem"] is None
        assert info["counts"]["individuals"] == 1
        assert info["counts"]["ledger"] == 2
        assert info["schema_version"] > 0
        assert os.path.getmtime(path) == before, "opened read-only"
        assert ind
    finally:
        os.remove(path)


def test_a_damaged_file_is_refused(db, tmp_path):
    bad = tmp_path / "rubbish.db"
    bad.write_bytes(b"this is not a database at all, not even close" * 40)
    info = db.inspect_backup(str(bad))
    assert not info["ok"] and info["problem"]
    with pytest.raises(ValueError):
        db.restore_from(str(bad))


def test_a_database_that_is_not_a_journal_is_refused(db, tmp_path):
    """A valid SQLite file is not the same thing as a LoanMaster journal."""
    other = tmp_path / "shopping.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE groceries (item TEXT)")
    conn.commit()
    conn.close()

    info = db.inspect_backup(str(other))
    assert not info["ok"] and "does not look like" in info["problem"]
    with pytest.raises(ValueError):
        db.restore_from(str(other))


def test_a_missing_file_is_refused(db):
    assert db.inspect_backup("/nowhere/at/all.db")["problem"]


def test_restoring_brings_back_deleted_records(db):
    """The point of the whole exercise."""
    ind = _busy_member(db)
    path = db.snapshot("before-delete")
    try:
        db.delete_individual(ind)
        assert db.conn.execute("SELECT COUNT(*) FROM individuals").fetchone()[0] == 0

        result = db.restore_from(path)
        assert db.conn.execute("SELECT COUNT(*) FROM individuals").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 2
        assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 1
        assert result["integrity_ok"]
        os.remove(result["safety_copy"])
    finally:
        os.remove(path)


def test_restoring_snapshots_the_current_state_first(db):
    """Restoring the wrong file is the commonest way to lose data with a
    restore feature, so restoring is itself reversible."""
    ind = _busy_member(db)
    old = db.snapshot("old-state")
    try:
        db.add_savings_transaction(ind, "2025-04-01", "Deposit", 4321, "since")
        result = db.restore_from(old)
        assert db.conn.execute(
            "SELECT COUNT(*) FROM savings WHERE amount=4321").fetchone()[0] == 0

        # The work done since is not lost — it is in the safety copy.
        db.restore_from(result["safety_copy"])
        assert db.conn.execute(
            "SELECT COUNT(*) FROM savings WHERE amount=4321").fetchone()[0] == 1
        os.remove(result["safety_copy"])
    finally:
        os.remove(old)


def test_the_connection_survives_a_restore(db):
    """The copy goes into the open connection rather than replacing the file,
    so every repository is still usable afterwards."""
    ind = _busy_member(db)
    path = db.snapshot("live")
    try:
        db.restore_from(path)
        assert db.get_savings_balance(ind) == 2000
        assert db.members.all_ids() if hasattr(db.members, "all_ids") else True
        new_ind = db.add_individual("After Restore", "07", "a@x")
        assert new_ind
        result_counts = db.member_record_counts(ind)
        assert result_counts["loans"] == 1
    finally:
        for f in [path] + [b["path"] for b in db.available_backups()]:
            if os.path.exists(f):
                os.remove(f)


def test_restoring_an_older_schema_re_applies_migrations(db, tmp_path):
    """A backup may predate the current code."""
    legacy = str(tmp_path / "legacy.db")
    old = DatabaseManager(legacy, auto_backup=False, bootstrap="legacy")
    old.add_individual("Legacy", "07", "l@x")
    old.conn.execute("DELETE FROM schema_migrations WHERE version >= 8")
    old.conn.commit()
    old.close()

    result = db.restore_from(legacy)
    from src import migrations
    assert migrations.schema_version(db.conn) == max(
        v for v, _, _ in migrations.discover())
    assert result["migration_report"].ok
    os.remove(result["safety_copy"])


def test_available_backups_lists_newest_first(db):
    import time
    first = db.snapshot("one")
    time.sleep(1.05)                       # mtime resolution
    second = db.snapshot("two")
    try:
        listed = db.available_backups()
        assert [b["path"] for b in listed][:2] == [second, first]
        assert all(b["size"] > 0 for b in listed)
    finally:
        os.remove(first)
        os.remove(second)


def test_available_backups_is_empty_when_there_are_none(db):
    assert db.available_backups() == []


def test_two_snapshots_in_the_same_second_do_not_collide(db):
    """Regression. The stamp is second-resolution and backup_to overwrites its
    destination, so a collision does not merely lose a backup — restoring twice
    within one second wrote the safety copy over the file being restored from,
    destroying it and restoring the wrong state."""
    _busy_member(db)
    paths = [db.snapshot("rapid") for _ in range(4)]
    try:
        assert len(set(paths)) == 4, paths
        assert all(os.path.exists(p) and os.path.getsize(p) > 0 for p in paths)
    finally:
        for p in paths:
            os.remove(p)
