"""A loan's running balance belongs to that loan alone.

Running balances in ``ledger`` are stored per loan, but the issue, top-up and
buy-off paths read "the member's last row" for their starting position. With a
second loan that row is usually another loan's, so its balance was added in:
a 200,000 loan opened at 493,597. The old recalculators rewrote every row
afterwards and hid it; the projection that replaced them did not touch
disbursement rows, so the wrong figure stuck.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services.loan_projection import project_loan


@pytest.fixture
def member():
    """A member whose first loan has run a few months, so its rows come last."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "b.db"), auto_backup=False)
    engine = LoanEngine(db)
    ind = db.add_individual("M", "07", "m@x")
    engine.loan_service.add_loan_event(ind, 100000, 20, "2025-01-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-04-01")
    yield db, engine, ind
    db.close()


def _row(db, ind, ref, event):
    return db.conn.execute(
        "SELECT balance, principal_balance, interest_balance, deducted FROM ledger "
        "WHERE individual_id=? AND loan_id=? AND event_type=? ORDER BY id DESC LIMIT 1",
        (ind, ref, event)).fetchone()


def test_a_second_loan_opens_at_its_own_principal(member):
    db, engine, ind = member
    engine.loan_service.add_loan_event(ind, 50000, 10, "2025-04-15")
    balance, principal, interest, _ = _row(db, ind, "L-002", "Loan Issued")
    assert (balance, principal, interest) == (50000, 50000, 0)


def test_a_back_dated_second_loan_opens_at_its_own_principal(member):
    """The production case: issued with a date earlier than the other loan's rows."""
    db, engine, ind = member
    engine.loan_service.add_loan_event(ind, 50000, 10, "2024-12-01")
    assert _row(db, ind, "L-002", "Loan Issued")[:3] == (50000, 50000, 0)
    assert db.get_loan_by_ref(ind, "L-002")["balance"] == 50000


def test_a_top_up_adds_to_its_own_loan_not_the_last_one_moved(member):
    db, engine, ind = member
    engine.loan_service.add_loan_event(ind, 50000, 10, "2025-04-15")
    before = db.get_loan_by_ref(ind, "L-001")["balance"]
    engine.loan_service.top_up_loan(ind, "L-001", 10000, 20, "2025-04-20")
    assert _row(db, ind, "L-001", "Loan Top-Up")[1] == before + 10000


def test_a_buyoff_settles_its_own_loan_not_the_last_one_moved(member):
    db, engine, ind = member
    engine.loan_service.add_loan_event(ind, 50000, 10, "2025-04-15")
    loan = db.get_loan_by_ref(ind, "L-001")
    owed = loan["balance"] + loan["interest_balance"] + loan["unearned_interest"]
    paid = engine.loan_service.buyoff_loan(ind, "L-001", "2025-04-20")
    assert paid == pytest.approx(owed)
    assert _row(db, ind, "L-001", "Loan Buyoff")[3] == pytest.approx(owed)


def test_projection_heals_an_issue_row_written_with_another_loans_balance(member):
    db, engine, ind = member
    engine.loan_service.add_loan_event(ind, 50000, 10, "2025-04-15")
    db.conn.execute(
        "UPDATE ledger SET balance=143000, principal_balance=143000 "
        "WHERE individual_id=? AND loan_id='L-002' AND event_type='Loan Issued'", (ind,))
    assert project_loan(db, ind, "L-002") == 1
    assert _row(db, ind, "L-002", "Loan Issued")[:3] == (50000, 50000, 0)
    assert project_loan(db, ind, "L-002") == 0


def test_a_restructure_row_carries_the_loans_position(member):
    db, engine, ind = member
    engine.loan_service.restructure_loan(ind, "L-001", 30)
    project_loan(db, ind, "L-001")
    loan = db.get_loan_by_ref(ind, "L-001")
    balance, principal, interest, _ = _row(db, ind, "L-001", "Loan Restructure")
    assert principal == loan["balance"]
    assert balance == pytest.approx(principal + interest)
