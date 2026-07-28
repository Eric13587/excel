"""Recording, resuming and *removing* loan suspensions.

The gap these cover: a backdated suspension whose window has already closed is
stored with `is_suspended` left clear, because the loan is not suspended now.
That is correct — but the Resume button only appears when `is_suspended` is set,
so such a span had no route out of the application at all. Catch-up kept
skipping its months forever.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


@pytest.fixture
def db():
    mgr = DatabaseManager(os.path.join(tempfile.mkdtemp(), "s.db"), auto_backup=False)
    yield mgr
    mgr.close()


def _loan(db, ref="L-001", member="Member"):
    """A loan with its disbursement actually in the ledger.

    The issue row matters: without it the recalculator derives the balance from
    repayments alone, drives it negative and flips the loan to Paid. That is not
    hypothetical — it is what happened to loan 483 in the production journal.
    """
    ind = db.add_individual(member, "07", "m@x")
    db.add_loan_record(ind, ref, 100000, 115000, 100000, 5750, 750, "2025-01-01", "2025-02-01",
                       unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", ref, 100000, 0, 100000,
                       "Principal: 100000.0, Total Interest: 15000.0, Duration: 20")
    loan = db.get_loan_by_ref(ind, ref)
    return ind, loan["id"]


def test_a_past_suspension_leaves_the_loan_unsuspended(db):
    """Why the Resume button never appears for one — the behaviour is right,
    the missing exit was the bug."""
    ind, loan_id = _loan(db)
    kind = db.record_suspension(loan_id, "2025-03-01", "2025-06-01", today="2026-01-01")
    assert kind == "historical"

    assert db.get_loan_by_ref(ind, "L-001")["is_suspended"] == 0
    spans = db.get_loan_suspensions_for_loan(loan_id)
    assert len(spans) == 1 and spans[0]["status"] == "resumed"


def test_a_past_suspension_can_be_deleted(db):
    ind, loan_id = _loan(db)
    db.record_suspension(loan_id, "2025-03-01", "2025-06-01", today="2026-01-01")
    span_id = db.get_loan_suspensions_for_loan(loan_id)[0]["id"]

    removed = db.delete_suspension(span_id)
    assert removed["start_date"] == "2025-03-01"
    assert db.get_loan_suspensions_for_loan(loan_id) == []


def test_deleting_the_live_span_clears_the_suspended_flag(db):
    ind, loan_id = _loan(db)
    db.record_suspension(loan_id, "2026-01-01", "2030-01-01", today="2026-01-01")
    assert db.get_loan_by_ref(ind, "L-001")["is_suspended"] == 1
    span_id = db.get_loan_suspensions_for_loan(loan_id)[0]["id"]

    db.delete_suspension(span_id)
    loan = db.get_loan_by_ref(ind, "L-001")
    assert loan["is_suspended"] == 0 and not loan["suspend_until"]


def test_deleting_a_closed_span_leaves_a_live_one_alone(db):
    """The flag belongs to whichever span is open; removing a historical one
    must not un-suspend a loan that really is suspended."""
    ind, loan_id = _loan(db)
    db.record_suspension(loan_id, "2025-03-01", "2025-06-01", today="2026-01-01")
    db.record_suspension(loan_id, "2026-01-01", "2030-01-01", today="2026-01-01")
    old = [s for s in db.get_loan_suspensions_for_loan(loan_id)
           if s["status"] == "resumed"][0]

    db.delete_suspension(old["id"])
    assert db.get_loan_by_ref(ind, "L-001")["is_suspended"] == 1
    assert len(db.get_loan_suspensions_for_loan(loan_id)) == 1


def test_a_suspension_makes_catch_up_skip_those_months(db):
    ind, loan_id = _loan(db)
    db.record_suspension(loan_id, "2025-02-01", "2025-05-01", today="2026-01-01")

    from src.engine import LoanEngine
    LoanEngine(db).loan_service.catch_up_loan(ind, "L-001", target_date="2025-07-01")
    dates = [r[0] for r in db.conn.execute(
        "SELECT date FROM ledger WHERE individual_id=? AND event_type='Repayment' "
        "ORDER BY date", (ind,))]
    assert dates and all(not ("2025-02" <= d[:7] <= "2025-04") for d in dates), dates


def test_deleting_a_span_does_not_backfill_by_itself(db):
    """Worth pinning, because it is the thing a user will assume.

    Deleting a span stops *future* catch-ups skipping those months, but
    catch-up only moves forward from the loan's next due date — it never goes
    back. Restoring the missed months needs a schedule rebuild, which is why the
    dialog says so rather than leaving the user to discover it.
    """
    ind, loan_id = _loan(db)
    db.record_suspension(loan_id, "2025-02-01", "2025-05-01", today="2026-01-01")

    from src.engine import LoanEngine
    engine = LoanEngine(db)
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-07-01")
    skipped = db.ledger.count_events(ind, "L-001", ("Repayment",))

    db.delete_suspension(db.get_loan_suspensions_for_loan(loan_id)[0]["id"])
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-07-01")
    assert db.ledger.count_events(ind, "L-001", ("Repayment",)) == skipped

    engine.loan_service.rebuild_loan_schedule(ind, "L-001", target_date="2025-07-01")
    assert db.ledger.count_events(ind, "L-001", ("Repayment",)) > skipped


def test_deleting_a_span_that_does_not_exist_is_harmless(db):
    assert db.delete_suspension(9999) is None


def test_suspensions_are_scoped_to_the_loan_not_the_ref(db):
    """Loan refs are unique per member only, so a per-loan query has to use the
    primary key or it leaks another member's spans."""
    ind_a, loan_a = _loan(db)
    ind_b, loan_b = _loan(db, member="Other")

    db.record_suspension(loan_a, "2025-03-01", "2025-06-01", today="2026-01-01")
    assert len(db.get_loan_suspensions_for_loan(loan_a)) == 1
    assert db.get_loan_suspensions_for_loan(loan_b) == []
