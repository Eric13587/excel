"""Tests for the orphaned-record repair layer (find/export/delete)."""
import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    mgr = DatabaseManager(path)
    yield mgr
    mgr.close()
    os.remove(path)


def _seed_with_orphans(db):
    """Two members with activity; member 2 is then deleted the 'legacy' way
    (FK enforcement off), leaving orphans. Also plants a NULL-member row.

    Migration 001 installs cascade triggers, so a parent DELETE now clears the
    children and orphans cannot be produced this way any more. The triggers are
    therefore dropped first, reproducing the schema every journal written before
    migration 001 is on — which is the only state the repair tool exists to
    clean up. test_deleting_a_member_leaves_no_orphans covers the migrated
    behaviour.
    """
    db.conn.execute("DROP TRIGGER IF EXISTS trg_cascade_individual_delete")
    db.conn.execute("DROP TRIGGER IF EXISTS trg_cascade_loan_delete")
    db.conn.commit()

    keep = db.add_individual("Keep Me", "0700", "keep@x.com")
    gone = db.add_individual("Delete Me", "0711", "gone@x.com")

    cur = db.conn.cursor()
    for ind in (keep, gone):
        cur.execute("INSERT INTO ledger (individual_id, date, event_type, deducted, balance, loan_id) "
                    "VALUES (?, '2026-01-15', 'Repayment', 500, 1000, 'L-001')", (ind,))
        cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                    "VALUES (?, '2026-02-01', 'Deposit', 250, 250)", (ind,))
        cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                    "installment, status) VALUES (?, 'L-001', 5000, 5750, 1000, 500, 'active')", (ind,))
    # NULL-member garbage row
    cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                "VALUES (NULL, '2026-03-01', 'Deposit', 99, 99)")

    # Simulate the legacy bug: delete the parent with enforcement off.
    # (The pragma is a no-op while a transaction is open, so commit first.)
    db.conn.commit()
    cur.execute("PRAGMA foreign_keys = OFF")
    cur.execute("DELETE FROM individuals WHERE id = ?", (gone,))
    db.conn.commit()
    return keep, gone


def test_find_orphans_groups_by_table_and_member(db):
    keep, gone = _seed_with_orphans(db)
    orphans = db.find_orphaned_rows()

    by_table = {(o["table"], o["individual_id"]): o for o in orphans}
    assert by_table[("ledger", gone)]["rows"] == 1
    assert by_table[("loans", gone)]["rows"] == 1
    assert by_table[("savings", gone)]["rows"] == 1
    assert by_table[("savings", None)]["rows"] == 1
    # Date ranges come from the rows themselves
    assert by_table[("ledger", gone)]["first_date"] == "2026-01-15"
    # The surviving member's rows are never reported
    assert not any(o["individual_id"] == keep for o in orphans)


def test_find_orphans_clean_db_returns_empty(db):
    db.add_individual("Solo", "0700", "s@x.com")
    assert db.find_orphaned_rows() == []


def test_export_orphans_writes_audit_workbook(db):
    _, gone = _seed_with_orphans(db)
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        total = db.export_orphaned_rows(path)
        assert total == 4  # ledger + loans + savings(orphan) + savings(NULL)

        sheets = pd.read_excel(path, sheet_name=None)
        assert "Summary" in sheets
        assert len(sheets["Summary"]) == 4  # 4 orphan groups
        assert len(sheets["ledger"]) == 1
        assert len(sheets["savings"]) == 2
        assert sheets["loans"].iloc[0]["individual_id"] == gone
    finally:
        os.remove(path)


def test_delete_orphans_removes_only_orphans_and_enables_fk(db):
    keep, gone = _seed_with_orphans(db)
    counts = db.delete_orphaned_rows()

    assert counts == {"ledger": 1, "loans": 1, "savings": 2}
    assert db.find_orphaned_rows() == []

    # The surviving member's data is untouched
    cur = db.conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM ledger WHERE individual_id=?", (keep,)).fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM savings WHERE individual_id=?", (keep,)).fetchone()[0] == 1

    # Journal is consistent again, so enforcement engaged immediately
    assert cur.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_delete_orphans_noop_on_clean_db(db):
    db.add_individual("Solo", "0700", "s@x.com")
    assert db.delete_orphaned_rows() == {}


def test_deleting_a_member_leaves_no_orphans(db):
    """Regression guard for the bug the repair tool was built to clean up after.

    delete_individual only removed `ledger` rows; savings, both funds, the
    benevolent enrolment and suspension history were left pointing at a member
    id that no longer existed. Migration 001's cascade triggers remove them with
    the parent, so no repair is ever needed on a migrated journal.
    """
    ind = db.add_individual("Full Life", "0700", "f@x.com")
    cur = db.conn.cursor()
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, deducted, balance) "
                "VALUES (?, '2026-01-15', 'Repayment', 'L-001', 500, 1000)", (ind,))
    cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                "VALUES (?, '2026-02-01', 'Deposit', 250, 250)", (ind,))
    cur.execute("INSERT INTO christmas_savings (individual_id, date, transaction_type, amount, balance) "
                "VALUES (?, '2026-02-01', 'Deposit', 100, 100)", (ind,))
    cur.execute("INSERT INTO benevolent_ledger (individual_id, date, transaction_type, amount, balance) "
                "VALUES (?, '2026-02-01', 'Contribution', 50, 50)", (ind,))
    cur.execute("INSERT INTO benevolent_accounts (individual_id, monthly_amount, start_date, "
                "next_due_date, active) VALUES (?, 50, '2026-01-01', '2026-03-01', 1)", (ind,))
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, status, start_date) "
                "VALUES (?, 'L-001', 5000, 5750, 1000, 500, 'Active', '2026-01-01')", (ind,))
    loan_id = cur.lastrowid
    cur.execute("INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, status) "
                "VALUES (?, ?, 'L-001', '2026-02-01', 'active')", (loan_id, ind))
    db.conn.commit()

    assert db.fk_enforced, "the journal is clean, so enforcement should be on"
    db.delete_individual(ind)

    assert db.find_orphaned_rows() == []
    for table in ("ledger", "savings", "christmas_savings", "benevolent_ledger",
                  "benevolent_accounts", "loans", "loan_suspensions"):
        remaining = db.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE individual_id=?", (ind,)).fetchone()[0]
        assert remaining == 0, f"{table} kept {remaining} row(s) after the member was deleted"
