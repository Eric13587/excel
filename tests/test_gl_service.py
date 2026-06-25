"""Tests for the double-entry General Ledger foundation (GLService)."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.services.gl_service import GLService
from src.exceptions import UnbalancedJournalError, UnknownAccountError
from src.config import (
    DEFAULT_CHART_OF_ACCOUNTS, GL_CASH, GL_LOANS_RECEIVABLE,
    GL_INTEREST_RECEIVABLE, GL_MEMBER_DEPOSITS, GL_LOAN_INTEREST_INCOME,
)


@pytest.fixture
def gl():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "gl.db"))
    return GLService(db), db


def _legacy_gl(db, date, category, gtype, amount, notes=""):
    """Insert a row into the frozen legacy single-entry table (test bridge).

    The production add_gl_entry method has been retired; migration tests still
    need to simulate pre-existing legacy data, so they write the table directly.
    """
    db.conn.execute(
        "INSERT INTO general_ledger (date, category, type, amount, notes) VALUES (?, ?, ?, ?, ?)",
        (date, category, gtype, amount, notes),
    )
    db.conn.commit()


# --------------------------------------------------------------------------- #
# Chart of accounts
# --------------------------------------------------------------------------- #
def test_chart_seeded(gl):
    svc, _ = gl
    accounts = svc.get_accounts()
    assert len(accounts) == len(DEFAULT_CHART_OF_ACCOUNTS)
    cash = next(a for a in accounts if a['code'] == GL_CASH)
    assert cash['type'] == 'Asset' and cash['normal_balance'] == 'debit'


def test_seed_is_idempotent(gl):
    svc, db = gl
    db.create_tables()  # run the seed again
    assert len(svc.get_accounts()) == len(DEFAULT_CHART_OF_ACCOUNTS)


# --------------------------------------------------------------------------- #
# post_journal core invariants
# --------------------------------------------------------------------------- #
def test_balanced_entry_posts_and_trial_balances(gl):
    svc, _ = gl
    svc.post_journal("2026-01-01", [
        {'account': GL_CASH, 'debit': 1000},
        {'account': GL_MEMBER_DEPOSITS, 'credit': 1000},
    ])
    rows, balanced = svc.get_trial_balance()
    assert balanced
    assert svc.get_account_balance(GL_CASH) == 1000.0
    # credit-normal account is positive when credit-heavy
    assert svc.get_account_balance(GL_MEMBER_DEPOSITS) == 1000.0


def test_unbalanced_entry_rejected(gl):
    svc, _ = gl
    with pytest.raises(UnbalancedJournalError):
        svc.post_journal("2026-01-01", [
            {'account': GL_CASH, 'debit': 1000},
            {'account': GL_MEMBER_DEPOSITS, 'credit': 999},
        ])
    # nothing was written
    assert svc.get_account_balance(GL_CASH) == 0.0


def test_unknown_account_rejected(gl):
    svc, _ = gl
    with pytest.raises(UnknownAccountError):
        svc.post_journal("2026-01-01", [
            {'account': '9999', 'debit': 10},
            {'account': GL_CASH, 'credit': 10},
        ])


def test_empty_entry_rejected(gl):
    svc, _ = gl
    with pytest.raises(ValueError):
        svc.post_journal("2026-01-01", [])


def test_source_ref_idempotent(gl):
    svc, _ = gl
    a = svc.post_journal("2026-01-01", [
        {'account': GL_CASH, 'debit': 50}, {'account': GL_MEMBER_DEPOSITS, 'credit': 50},
    ], source="savings", source_ref="savings:1")
    b = svc.post_journal("2026-01-01", [
        {'account': GL_CASH, 'debit': 50}, {'account': GL_MEMBER_DEPOSITS, 'credit': 50},
    ], source="savings", source_ref="savings:1")
    assert a == b  # same entry returned, not double-posted
    assert svc.get_account_balance(GL_CASH) == 50.0


# --------------------------------------------------------------------------- #
# Member-activity posting
# --------------------------------------------------------------------------- #
def test_disbursement_repayment_interest_flow(gl):
    svc, _ = gl
    svc.post_loan_disbursement(10000, "2026-01-01", "ledger:1")
    svc.post_interest_accrual(150, "2026-02-01", "ledger:2")
    svc.post_repayment(800, 150, "2026-02-15", "ledger:3")

    # Loans receivable: +10000 disbursed - 800 principal repaid
    assert svc.get_account_balance(GL_LOANS_RECEIVABLE) == 9200.0
    # Interest receivable: +150 accrued - 150 repaid
    assert svc.get_account_balance(GL_INTEREST_RECEIVABLE) == 0.0
    # Interest income earned
    assert svc.get_account_balance(GL_LOAN_INTEREST_INCOME) == 150.0
    # Cash: -10000 out + 950 repaid
    assert svc.get_account_balance(GL_CASH) == -9050.0
    _, balanced = svc.get_trial_balance()
    assert balanced


def test_repayment_with_zero_amount_noops(gl):
    svc, _ = gl
    assert svc.post_repayment(0, 0, "2026-01-01", "ledger:9") is None


def test_reverse_entry_nets_to_zero(gl):
    svc, _ = gl
    eid = svc.post_journal("2026-01-01", [
        {'account': GL_CASH, 'debit': 500}, {'account': GL_MEMBER_DEPOSITS, 'credit': 500},
    ])
    svc.reverse_entry(eid, "2026-01-02")
    assert svc.get_account_balance(GL_CASH) == 0.0
    assert svc.get_account_balance(GL_MEMBER_DEPOSITS) == 0.0
    _, balanced = svc.get_trial_balance()
    assert balanced


# --------------------------------------------------------------------------- #
# Backfill from existing subledgers
# --------------------------------------------------------------------------- #
def _seed_subledgers(db):
    ind = db.add_individual("M", "0", "m@x")
    db.add_loan_record(ind, "L1", 10000, 11500, 11500, 1000, 0, "2026-01-01", "2026-02-01")
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L1", 10000, 0, 11500, "issue")
    db.add_transaction(ind, "2026-02-01", "Interest Earned", "L1", 150, 0, 11650, "accr",
                       interest_amount=150)
    db.add_transaction(ind, "2026-02-10", "Repayment", "L1", 0, 1000, 10650, "pmt",
                       principal_portion=850, interest_portion=150)
    db.add_savings_transaction(ind, "2026-01-05", "Deposit", 2000, "dep")
    db.add_savings_transaction(ind, "2026-03-01", "Withdrawal", 500, "wd")
    return ind


def test_backfill_balances_and_is_idempotent(gl):
    svc, db = gl
    _seed_subledgers(db)

    n1 = svc.backfill_from_subledgers()
    assert n1 == 5  # issue, interest, repayment, deposit, withdrawal
    rows, balanced = svc.get_trial_balance()
    assert balanced

    # Loans receivable = 10000 - 850
    assert svc.get_account_balance(GL_LOANS_RECEIVABLE) == 9150.0
    # Member deposits = 2000 - 500
    assert svc.get_account_balance(GL_MEMBER_DEPOSITS) == 1500.0
    assert svc.get_account_balance(GL_LOAN_INTEREST_INCOME) == 150.0

    # Running again posts nothing new.
    assert svc.backfill_from_subledgers() == 0
    _, balanced2 = svc.get_trial_balance()
    assert balanced2


# --------------------------------------------------------------------------- #
# Legacy single-entry migration
# --------------------------------------------------------------------------- #
def test_migrate_legacy_gl_balances_and_idempotent(gl):
    svc, db = gl
    _legacy_gl(db, "2026-01-01", "Initial Bank Capital", "Asset", 50000, "seed capital")
    _legacy_gl(db, "2026-01-10", "Office Rent", "Expense", 12000, "jan rent")
    _legacy_gl(db, "2026-01-15", "Fines & Fees (Income)", "Income", 3000, "late fees")
    _legacy_gl(db, "2026-01-20", "External Loan Received", "Liability/Equity", 20000, "bank loan")

    n1 = svc.migrate_legacy_gl()
    assert n1 == 4
    rows, balanced = svc.get_trial_balance()
    assert balanced
    # Cash = +50000 (capital) -12000 (rent) +3000 (income) +20000 (borrowing)
    assert svc.get_account_balance(GL_CASH) == 61000.0
    assert svc.get_account_balance("5100") == 12000.0  # Office Rent expense

    assert svc.migrate_legacy_gl() == 0  # idempotent
    _, balanced2 = svc.get_trial_balance()
    assert balanced2


def test_backfill_and_migration_together_balance(gl):
    svc, db = gl
    _seed_subledgers(db)
    _legacy_gl(db, "2026-01-10", "Office Rent", "Expense", 12000, "rent")
    svc.backfill_from_subledgers()
    svc.migrate_legacy_gl()
    rows, balanced = svc.get_trial_balance()
    assert balanced
    # Trial balance debit column equals credit column exactly.
    assert round(sum(r['debit'] for r in rows), 2) == round(sum(r['credit'] for r in rows), 2)


# --------------------------------------------------------------------------- #
# Financial statements
# --------------------------------------------------------------------------- #
def test_income_statement_is_period_bounded(gl):
    svc, _ = gl
    # Jan income + Feb income; expense in Feb.
    svc.post_journal("2026-01-15", [{'account': GL_CASH, 'debit': 1000},
                                    {'account': GL_LOAN_INTEREST_INCOME, 'credit': 1000}])
    svc.post_journal("2026-02-15", [{'account': GL_CASH, 'debit': 500},
                                    {'account': GL_LOAN_INTEREST_INCOME, 'credit': 500}])
    svc.post_journal("2026-02-20", [{'account': "5100", 'debit': 300},
                                    {'account': GL_CASH, 'credit': 300}])

    feb = svc.get_income_statement("2026-02-01", "2026-02-28")
    assert feb['total_revenue'] == 500.0   # Jan revenue excluded
    assert feb['total_expenses'] == 300.0
    assert feb['net_surplus'] == 200.0

    all_time = svc.get_income_statement()
    assert all_time['total_revenue'] == 1500.0


def test_balance_sheet_balances_after_backfill(gl):
    svc, db = gl
    _seed_subledgers(db)
    svc.backfill_from_subledgers()

    bs = svc.get_balance_sheet()
    assert bs['is_balanced']
    assert bs['total_assets'] == bs['total_liabilities_and_equity']
    # Equity is entirely the current surplus here (no capital posted): == net income
    inc = svc.get_income_statement()
    assert bs['total_equity'] == inc['net_surplus']


def test_balance_sheet_with_capital_and_expense_balances(gl):
    svc, db = gl
    _seed_subledgers(db)
    _legacy_gl(db, "2026-01-01", "Initial Bank Capital", "Asset", 50000, "capital")
    _legacy_gl(db, "2026-01-10", "Office Rent", "Expense", 12000, "rent")
    svc.sync()  # rebuild projection + migrate legacy in one call

    bs = svc.get_balance_sheet()
    assert bs['is_balanced']
    # Identity holds exactly.
    assert round(bs['total_assets'] - bs['total_liabilities_and_equity'], 2) == 0.0


# --------------------------------------------------------------------------- #
# Rebuild / sync keep the projection consistent with edits & undos
# --------------------------------------------------------------------------- #
def test_rebuild_reflects_ledger_deletion(gl):
    svc, db = gl
    ind = _seed_subledgers(db)
    svc.backfill_from_subledgers()
    before = svc.get_account_balance(GL_LOANS_RECEIVABLE)

    # Undo the repayment by deleting that ledger row, then rebuild.
    cur = db.conn.cursor()
    cur.execute("DELETE FROM ledger WHERE event_type='Repayment'")
    db.conn.commit()
    svc.rebuild_auto_journals()

    after = svc.get_account_balance(GL_LOANS_RECEIVABLE)
    # Removing an 850 principal repayment raises receivable back up.
    assert after == before + 850.0
    _, balanced = svc.get_trial_balance()
    assert balanced


def test_rebuild_preserves_manual_and_migration_entries(gl):
    svc, db = gl
    _seed_subledgers(db)
    _legacy_gl(db, "2026-01-10", "Office Rent", "Expense", 12000, "rent")
    svc.backfill_from_subledgers()
    svc.migrate_legacy_gl()

    manual_id = svc.post_journal("2026-01-01", [
        {'account': GL_CASH, 'debit': 100}, {'account': GL_LOAN_INTEREST_INCOME, 'credit': 100},
    ], source="manual", source_ref="manual:test")

    svc.rebuild_auto_journals()  # wipes only the projection slice

    # Manual + migration entries survive the rebuild.
    assert svc._find_entry("manual", "manual:test") == manual_id
    assert svc.get_account_balance("5100") == 12000.0  # migrated rent expense intact
    _, balanced = svc.get_trial_balance()
    assert balanced


# --------------------------------------------------------------------------- #
# Journal & account drill-down
# --------------------------------------------------------------------------- #
def test_account_ledger_running_balance(gl):
    svc, _ = gl
    svc.post_loan_disbursement(10000, "2026-01-01", "ledger:1")
    svc.post_repayment(800, 0, "2026-02-01", "ledger:2")
    led = svc.get_account_ledger(GL_LOANS_RECEIVABLE)
    assert [r['balance'] for r in led] == [10000.0, 9200.0]  # debit-normal runs up then down
    assert led[0]['debit'] == 10000.0 and led[1]['credit'] == 800.0


def test_get_journal_entries_lists_recent(gl):
    svc, _ = gl
    svc.post_journal("2026-01-01", [{'account': GL_CASH, 'debit': 100},
                                    {'account': GL_LOAN_INTEREST_INCOME, 'credit': 100}], memo="m1")
    entries = svc.get_journal_entries()
    assert len(entries) == 1
    assert entries[0]['amount'] == 100.0 and entries[0]['memo'] == "m1"


def test_is_auto_source(gl):
    svc, _ = gl
    assert svc.is_auto_source("repayment") is True
    assert svc.is_auto_source("manual") is False


# --------------------------------------------------------------------------- #
# Cash flow statement
# --------------------------------------------------------------------------- #
def test_cash_flow_ties_to_cash_and_categorises(gl):
    svc, _ = gl
    svc.post_loan_disbursement(10000, "2026-01-01", "ledger:1")   # cash out, Lending
    svc.post_repayment(800, 150, "2026-02-01", "ledger:2")        # Lending +800, Operating +150
    svc.post_savings_deposit(2000, "2026-03-01", "ledger:3")      # Financing

    cf = svc.get_cash_flow()
    sect = {s['label']: s for s in cf['sections']}
    assert sect['Lending to members']['subtotal'] == -9200.0
    assert sect['Operating']['subtotal'] == 150.0
    assert sect['Financing']['subtotal'] == 2000.0
    assert cf['net_change'] == -7050.0
    # Closing cash from the statement equals the cash account balance exactly.
    assert cf['closing_cash'] == svc.get_account_balance(GL_CASH)


def test_cash_flow_period_opening_and_closing(gl):
    svc, _ = gl
    svc.post_savings_deposit(1000, "2026-01-10", "s:1")  # before the period
    svc.post_savings_deposit(500, "2026-02-10", "s:2")   # within the period
    cf = svc.get_cash_flow("2026-02-01", "2026-02-28")
    assert cf['opening_cash'] == 1000.0
    assert cf['net_change'] == 500.0
    assert cf['closing_cash'] == 1500.0


def test_bulk_sync_is_one_transaction_and_balances(gl):
    svc, db = gl
    _seed_subledgers(db)
    svc.sync()
    _, balanced = svc.get_trial_balance()
    assert balanced
    # Re-sync is a no-op count-wise (rebuild then identical backfill).
    n = svc.backfill_from_subledgers()
    assert n == 0
