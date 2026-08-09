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


# --------------------------------------------------------------------------- #
# Complex histories: rebuild only where a replay is actually faithful
# --------------------------------------------------------------------------- #

def _edit_first_repayment(db, ind, amount=20000):
    row_id = db.conn.execute(
        "SELECT id FROM ledger WHERE individual_id=? AND event_type='Repayment' "
        "ORDER BY date LIMIT 1", (ind,)).fetchone()[0]
    date = db.conn.execute("SELECT date FROM ledger WHERE id=?", (row_id,)).fetchone()[0]
    db.update_transaction(row_id, date, 0, amount, "hand-corrected", mark_edited=True)
    return row_id


def test_a_rebuild_is_refused_when_it_would_wipe_a_hand_edit(loan):
    """rebuild_loan_schedule regenerates every repayment at the scheduled
    amount, so a correction of 20,000 came back as 5,750 — silently, and the
    balance moved by 750. There are 89 such rows across 19 production loans."""
    db, engine, ind, lid = loan
    _edit_first_repayment(db, ind)
    before_rows = db.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=?", (ind,)).fetchone()[0]
    before_balance = db.get_loan_by_ref(ind, "L-001")["balance"]
    span = db.get_loan_suspensions_for_loan(lid)[0]

    with pytest.raises(ValueError, match="hand-edited"):
        engine.loan_service.remove_suspension(ind, "L-001", span["id"], rebuild=True)

    assert db.conn.execute("SELECT COUNT(*) FROM ledger WHERE individual_id=?",
                           (ind,)).fetchone()[0] == before_rows
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == before_balance
    assert db.get_loan_suspensions_for_loan(lid), "nothing removed when it cannot finish"


def test_the_edit_survives_because_the_rebuild_refused(loan):
    db, engine, ind, lid = loan
    row_id = _edit_first_repayment(db, ind)
    span = db.get_loan_suspensions_for_loan(lid)[0]
    with pytest.raises(ValueError):
        engine.loan_service.remove_suspension(ind, "L-001", span["id"], rebuild=True)
    assert db.conn.execute("SELECT deducted FROM ledger WHERE id=?",
                           (row_id,)).fetchone()[0] == 20000


def test_removing_without_rebuilding_still_works_on_an_edited_loan(loan):
    """The refusal is about regenerating rows, not about removing the record."""
    db, engine, ind, lid = loan
    _edit_first_repayment(db, ind)
    span = db.get_loan_suspensions_for_loan(lid)[0]

    removed, created = engine.loan_service.remove_suspension(ind, "L-001", span["id"])
    assert removed is not None and created == 0
    assert db.get_loan_suspensions_for_loan(lid) == []


def test_a_rebuilt_plain_loan_agrees_with_its_own_replay(loan):
    """Where a rebuild *is* allowed, the result must be what the derivation
    says it should be — otherwise the rebuild is inventing history."""
    from src.services.loan_derivation import derive
    from src.services.loan_events import reconstruct_for_loan

    db, engine, ind, lid = loan
    span = db.get_loan_suspensions_for_loan(lid)[0]
    engine.loan_service.remove_suspension(ind, "L-001", span["id"], rebuild=True)

    derived = derive(reconstruct_for_loan(db, ind, "L-001"))
    assert abs(derived.principal_balance
               - db.get_loan_by_ref(ind, "L-001")["balance"]) < 0.51


def test_has_manual_edits_sees_only_this_loan(loan):
    """Scoped by member and ref, since a ref is unique per member only."""
    db, engine, ind, _lid = loan
    assert not engine.loan_service.has_manual_edits(ind, "L-001")
    _edit_first_repayment(db, ind)
    assert engine.loan_service.has_manual_edits(ind, "L-001")
    assert not engine.loan_service.has_manual_edits(ind, "L-999")
