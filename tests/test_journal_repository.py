"""Phase 2e — JournalRepository.

`GLService` keeps the accounting logic; this holds the SQL. The tests below
cover the repository directly, plus the one behaviour that is preserved
because it is wrong: a dated trial balance does not filter by date.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.services.gl_service import GLService


@pytest.fixture
def gl():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "j.db"))
    yield GLService(db), db
    db.close()


# --------------------------------------------------------------------------- #
# Chart of accounts
# --------------------------------------------------------------------------- #

def test_accounts_are_seeded_and_readable(gl):
    _, db = gl
    codes = db.journal.account_codes()
    assert {"1000", "1100", "2000", "4000", "5900"} <= codes
    assert len(db.journal.accounts()) == 25
    assert db.journal.normal_balance("1000") == "debit"
    assert db.journal.normal_balance("2000") == "credit"
    assert db.journal.normal_balance("9999") is None


def test_inactive_accounts_can_be_excluded(gl):
    _, db = gl
    with db.transaction():
        db.conn.execute("UPDATE chart_of_accounts SET is_active=0 WHERE code='5500'")
    assert len(db.journal.accounts(active_only=True)) == 24
    assert len(db.journal.accounts(active_only=False)) == 25


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #

def test_post_writes_a_header_and_its_lines(gl):
    svc, db = gl
    entry_id = svc.post_journal("2026-01-01", [
        {"account": "1000", "debit": 500},
        {"account": "4900", "credit": 500}], memo="test")

    assert db.journal.entry_header(entry_id) == ("2026-01-01", "test")
    lines = db.journal.entry_lines(entry_id)
    assert sorted(lines) == [("1000", 500.0, 0.0), ("4900", 0.0, 500.0)]
    assert db.journal.entry_count() == 1


def test_source_ref_makes_posting_idempotent(gl):
    svc, db = gl
    first = svc.post_journal("2026-01-01", [{"account": "1000", "debit": 10},
                                            {"account": "4900", "credit": 10}],
                             source="savings", source_ref="savings:1")
    second = svc.post_journal("2026-01-01", [{"account": "1000", "debit": 10},
                                             {"account": "4900", "credit": 10}],
                              source="savings", source_ref="savings:1")
    assert first == second
    assert db.journal.entry_count() == 1
    assert db.journal.find_by_source_ref("savings", "savings:1") == first
    assert db.journal.find_by_source_ref("savings", "savings:2") is None


def test_reversal_marks_both_entries(gl):
    svc, db = gl
    original = svc.post_journal("2026-01-01", [{"account": "1000", "debit": 100},
                                               {"account": "4900", "credit": 100}])
    reversal = svc.reverse_entry(original, reversal_date="2026-02-01")

    status = dict(db.conn.execute(
        "SELECT id, status FROM journal_entries WHERE id IN (?, ?)", (original, reversal)))
    assert status[original] == "reversed"
    assert status[reversal] == "reversal"
    assert db.conn.execute("SELECT reversal_of FROM journal_entries WHERE id=?",
                           (reversal,)).fetchone()[0] == original
    # The pair nets to zero.
    assert svc.get_account_balance("1000") == 0.0


def test_delete_by_sources_removes_headers_and_lines(gl):
    svc, db = gl
    svc.post_journal("2026-01-01", [{"account": "1000", "debit": 10},
                                    {"account": "4900", "credit": 10}],
                     source="savings", source_ref="savings:1")
    kept = svc.post_journal("2026-01-01", [{"account": "1000", "debit": 20},
                                           {"account": "4900", "credit": 20}],
                            source="manual")

    with db.transaction():
        db.journal.delete_by_sources(("savings",))

    assert db.journal.entry_count() == 1
    assert db.journal.entry_header(kept) is not None
    assert db.conn.execute("SELECT COUNT(*) FROM journal_lines").fetchone()[0] == 2


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #

def test_account_net_respects_as_of(gl):
    svc, db = gl
    svc.post_journal("2026-01-01", [{"account": "1000", "debit": 100},
                                    {"account": "4900", "credit": 100}])
    svc.post_journal("2026-06-01", [{"account": "1000", "debit": 400},
                                    {"account": "4900", "credit": 400}])

    assert db.journal.account_net("1000") == 500
    assert db.journal.account_net("1000", as_of="2026-03-01") == 100
    assert db.journal.account_net("1000", as_of="2020-01-01") == 0


def test_statement_rows_respect_the_period(gl):
    svc, db = gl
    svc.post_journal("2026-01-15", [{"account": "1000", "debit": 100},
                                    {"account": "4900", "credit": 100}])
    svc.post_journal("2026-06-15", [{"account": "1000", "debit": 400},
                                    {"account": "4900", "credit": 400}])

    all_income = dict((c, n) for c, _, n in db.journal.statement_rows("Income"))
    q1_income = dict((c, n) for c, _, n in
                     db.journal.statement_rows("Income", "2026-01-01", "2026-03-31"))
    assert all_income["4900"] == -500      # credit-heavy, so net is negative
    assert q1_income["4900"] == -100


def test_trial_balance_as_of_does_not_filter(gl):
    """Preserved defect. The date lands on a LEFT JOIN's ON clause, so a line
    whose entry falls after it still contributes — the join simply fails to
    match the header while the line survives the outer join.

    views/treasury.py passes a date, so the Treasury trial balance shows
    all-time figures whatever date is chosen. Pinned here so the fix is a
    deliberate, reviewed change rather than an accident.
    """
    svc, db = gl
    svc.post_journal("2026-06-01", [{"account": "1000", "debit": 100},
                                    {"account": "4900", "credit": 100}])

    unfiltered = db.journal.trial_balance_rows()
    dated = db.journal.trial_balance_rows(as_of="2020-01-01")
    assert dict((c, n) for c, _, _, n in dated) == dict((c, n) for c, _, _, n in unfiltered)

    # By contrast the account-level query, which uses a WHERE, does filter.
    assert db.journal.account_net("1000", as_of="2020-01-01") == 0


def test_cash_helpers(gl):
    svc, db = gl
    svc.post_journal("2026-01-01", [{"account": "1000", "debit": 300},
                                    {"account": "2000", "credit": 300}])
    svc.post_journal("2026-06-01", [{"account": "1000", "debit": 700},
                                    {"account": "4900", "credit": 700}])

    assert db.journal.cash_before("1000", "2026-03-01") == 300
    counterparts = db.journal.counterpart_lines("1000", "2026-01-01", "2026-12-31")
    assert sorted((c, d, cr) for c, _, d, cr in counterparts) == [
        ("2000", 0.0, 300.0), ("4900", 0.0, 700.0)]


# --------------------------------------------------------------------------- #
# Drill-down
# --------------------------------------------------------------------------- #

def test_recent_entries_are_newest_first_with_totals(gl):
    svc, db = gl
    svc.post_journal("2026-01-01", [{"account": "1000", "debit": 10},
                                    {"account": "4900", "credit": 10}], memo="older")
    svc.post_journal("2026-05-01", [{"account": "1000", "debit": 20},
                                    {"account": "4900", "credit": 20}], memo="newer")

    entries = db.journal.recent_entries(limit=10)
    assert [e["memo"] for e in entries] == ["newer", "older"]
    assert entries[0]["amount"] == 20


def test_account_postings_are_ordered_for_a_running_balance(gl):
    svc, db = gl
    svc.post_journal("2026-03-01", [{"account": "1000", "debit": 30},
                                    {"account": "4900", "credit": 30}])
    svc.post_journal("2026-01-01", [{"account": "1000", "debit": 10},
                                    {"account": "4900", "credit": 10}])

    postings = db.journal.account_postings("1000")
    assert [p[1] for p in postings] == ["2026-01-01", "2026-03-01"]
    assert [p[4] for p in postings] == [10.0, 30.0]

    assert len(db.journal.account_postings("1000", as_of="2026-02-01")) == 1


def test_legacy_rows_are_readable(gl):
    _, db = gl
    with db.transaction():
        db.conn.execute("INSERT INTO general_ledger (date, category, type, amount, notes) "
                        "VALUES ('2025-01-01', 'Office Rent', 'Expense', 5000, 'Jan rent')")
    rows = db.journal.legacy_rows()
    assert len(rows) == 1
    assert rows[0][1:] == ("2025-01-01", "Office Rent", "Expense", 5000.0, "Jan rent")


# --------------------------------------------------------------------------- #
# GLService still behaves identically through the repository
# --------------------------------------------------------------------------- #

def test_service_end_to_end_still_balances(gl):
    svc, db = gl
    ind = db.add_individual("GL Member", "0700", "g@x.com")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-001", 5000, 0, 5000, "issued")

    svc.sync()
    rows, balanced = svc.get_trial_balance()
    assert balanced and rows
    assert svc.get_account_balance("1100") == 5000
    assert svc.get_account_balance("2000") == 1000
    assert svc.get_balance_sheet()["is_balanced"]


def test_projection_rebuild_is_idempotent(gl):
    svc, db = gl
    ind = db.add_individual("GL Member", "0700", "g@x.com")
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-001", 5000, 0, 5000, "issued")

    svc.sync()
    first = db.journal.entry_count()
    svc.sync()
    assert db.journal.entry_count() == first
