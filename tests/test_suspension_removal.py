"""Undoing a suspension, and what it means for the months it skipped.

Removing the record and re-collecting the months are different acts. Conflating
them is how a member ends up either double-charged or quietly let off, so the
service makes the caller say which one it wants.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine


@pytest.fixture
def loan():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "s.db"), auto_backup=False)
    engine = LoanEngine(db)
    ind = db.add_individual("M", "07", "m@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                       "2025-01-01", "2025-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-001", 100000, 0, 100000, "issue")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(loan_id, "2025-03-01", "2025-06-01", today="2026-01-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-08-01")
    yield db, engine, ind, loan_id
    db.close()


def _repayments(db, ind):
    return db.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND event_type='Repayment'",
        (ind,)).fetchone()[0]


def test_the_suspension_skipped_months_in_the_first_place(loan):
    db, _engine, ind, _lid = loan
    assert _repayments(db, ind) == 4          # Feb, then Jun-Aug; Mar-May skipped


def test_removing_without_rebuilding_leaves_the_months_skipped(loan):
    """The record was entered in error but the deductions really did not happen
    — or happened somewhere this journal does not know about."""
    db, engine, ind, lid = loan
    span = db.get_loan_suspensions_for_loan(lid)[0]
    before = _repayments(db, ind)

    removed, created = engine.loan_service.remove_suspension(ind, "L-001", span["id"])

    assert removed["start_date"] == "2025-03-01"
    assert created == 0
    assert _repayments(db, ind) == before
    assert db.get_loan_suspensions_for_loan(lid) == []


def test_removing_with_a_rebuild_collects_the_skipped_months(loan):
    """The months should have been collected, so the schedule is re-derived."""
    db, engine, ind, lid = loan
    span = db.get_loan_suspensions_for_loan(lid)[0]
    before_balance = db.get_loan_by_ref(ind, "L-001")["balance"]

    _removed, created = engine.loan_service.remove_suspension(
        ind, "L-001", span["id"], rebuild=True)

    assert created > 0
    assert _repayments(db, ind) == 7, "March, April and May are now deducted"
    assert db.get_loan_by_ref(ind, "L-001")["balance"] < before_balance
    assert db.get_loan_suspensions_for_loan(lid) == []


def test_the_rebuild_is_refused_on_a_restructured_loan(loan):
    """rebuild_loan_schedule cannot safely replay a loan with top-ups, and
    silently doing something else would be worse than refusing."""
    db, engine, ind, lid = loan
    db.add_transaction(ind, "2025-07-01", "Loan Top-Up", "L-001", 50000, 0, 0, "top-up")
    span = db.get_loan_suspensions_for_loan(lid)[0]

    with pytest.raises(ValueError, match="top-ups or buy-offs"):
        engine.loan_service.remove_suspension(ind, "L-001", span["id"], rebuild=True)
    assert db.get_loan_suspensions_for_loan(lid), "nothing removed when it cannot finish"


def test_removing_a_span_that_is_gone_is_harmless(loan):
    db, engine, ind, _lid = loan
    removed, created = engine.loan_service.remove_suspension(ind, "L-001", 9999)
    assert removed is None and created == 0


def test_catch_up_deducts_the_months_once_the_span_is_gone(loan):
    """The point of removing without rebuilding: future catch-ups stop skipping."""
    db, engine, ind, lid = loan
    span = db.get_loan_suspensions_for_loan(lid)[0]
    engine.loan_service.remove_suspension(ind, "L-001", span["id"])

    db.loans.set_next_due(lid, "2025-03-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-05-01")
    assert _repayments(db, ind) > 4


# --------------------------------------------------------------------------- #
# Catching up to a chosen month
# --------------------------------------------------------------------------- #

def test_a_single_loan_catch_up_accepts_a_target(loan):
    """It used to run to today, which invents deductions nobody took when the
    SACCO is reconciling an older payroll run."""
    db, engine, ind, lid = loan
    span = db.get_loan_suspensions_for_loan(lid)[0]
    engine.loan_service.remove_suspension(ind, "L-001", span["id"])
    db.loans.set_next_due(lid, "2025-09-01")

    engine.catch_up_loan(ind, "L-001", target_date="2025-11-01")
    dates = [r[0] for r in db.conn.execute(
        "SELECT date FROM ledger WHERE individual_id=? AND event_type='Repayment' "
        "ORDER BY date", (ind,))]
    assert max(dates) <= "2025-11-01"


def test_a_savings_catch_up_accepts_a_target(loan):
    db, engine, ind, _lid = loan
    db.add_savings_transaction(ind, "2025-01-01", "Deposit", 1000, "")
    from datetime import datetime
    engine.catch_up_savings(ind, 1000, target_date=datetime(2025, 4, 1))
    dates = [r[0] for r in db.conn.execute(
        "SELECT date FROM savings WHERE individual_id=? ORDER BY date", (ind,))]
    assert max(dates) <= "2025-04-30"
