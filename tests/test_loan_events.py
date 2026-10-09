"""Recovering a loan's asserted events from the postings it already has.

The reconstruction is inference, not retrieval — terms have never been stored
with history — so what matters is that the inference is principled and
verifiable. These pin the principle.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.loan_derivation import (DISBURSED, SETTLED, SUSPENDED,
                                          TERMS_CHANGED, TOPPED_UP, derive, terms_on)
from src.services.loan_events import reconstruct


def _row(rid, date, kind, added=0, deducted=0, principal=0, interest=0):
    return (rid, date, kind, added, deducted, principal, interest)


def _kinds(events):
    return [e.kind for e in events]


def test_a_plain_loan_recovers_its_disbursement_and_payments():
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-02-01", "Interest Earned", added=750),
        _row(3, "2025-02-01", "Repayment", deducted=5750, principal=5000, interest=750),
    ])
    assert _kinds(events) == [DISBURSED, "PaymentReceived"]
    assert events[0].amount == 100000
    assert events[1].amount == 5750


def test_the_accrual_is_not_itself_an_event():
    """It is the thing being derived. Recording it as an assertion would make
    the replay tautological."""
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-02-01", "Interest Earned", added=750),
    ])
    assert "Interest Earned" not in _kinds(events)


def test_a_change_in_the_accrued_amount_is_a_change_in_the_terms():
    """The inference the whole reconstruction rests on. The application posts
    min(monthly_interest, unearned) each month, so a different amount means
    different terms, effective the date it first appears."""
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-02-01", "Interest Earned", added=750),
        _row(3, "2025-03-01", "Interest Earned", added=750),
        _row(4, "2025-04-01", "Interest Earned", added=1250),
        _row(5, "2025-05-01", "Interest Earned", added=1250),
    ])
    changes = [e for e in events if e.kind == TERMS_CHANGED]
    assert len(changes) == 1, "one change, not one per month"
    assert changes[0].date == "2025-04-01"
    assert changes[0].monthly_interest == 1250


def test_the_opening_rate_comes_from_the_first_accrual_not_the_loan_row():
    """loans.monthly_interest is the *current* value. Applying it to a loan's
    opening months is exactly the error the model exists to remove."""
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-02-01", "Interest Earned", added=250),
        _row(3, "2025-03-01", "Interest Earned", added=1326),
    ])
    assert events[0].monthly_interest == 250
    result = derive(events)
    assert terms_on(result.terms_timeline, "2025-02-01").monthly_interest == 250
    assert terms_on(result.terms_timeline, "2025-03-01").monthly_interest == 1326


def test_everything_accrued_becomes_the_interest_the_loan_was_written_for():
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-02-01", "Interest Earned", added=750),
        _row(3, "2025-03-01", "Interest Earned", added=750),
    ])
    assert events[0].unearned_interest == 1500


def test_a_buy_off_is_a_settlement_not_an_ordinary_payment():
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-06-01", "Loan Buyoff", deducted=95000, principal=95000),
    ])
    assert events[-1].kind == SETTLED
    assert derive(events).settled


def test_a_top_up_is_recovered():
    events = reconstruct([
        _row(1, "2025-01-01", "Loan Issued", added=100000),
        _row(2, "2025-06-01", "Loan Top-Up", added=50000),
    ])
    assert TOPPED_UP in _kinds(events)
    assert derive(events).principal_balance == 150000


def test_suspensions_join_the_timeline_in_date_order():
    events = reconstruct(
        [_row(1, "2025-01-01", "Loan Issued", added=100000),
         _row(2, "2025-06-01", "Repayment", deducted=5750)],
        suspensions=[("2025-03-01", "2025-05-01")])
    assert [e.date for e in events] == sorted(e.date for e in events)
    assert SUSPENDED in _kinds(events)


def test_a_loan_with_no_disbursement_recovers_no_opening_event():
    """Production loan 483. The reconstruction does not invent one, which is
    why that loan is the single one that will not replay."""
    events = reconstruct([
        _row(1, "2025-08-01", "Repayment", deducted=11717, principal=10188, interest=1529),
    ])
    assert DISBURSED not in _kinds(events)
    assert derive(events).principal_balance < 0


def test_an_empty_ledger_is_not_a_crash():
    assert reconstruct([]) == []


def test_rows_may_be_dicts_or_tuples():
    as_dicts = reconstruct([
        {"id": 1, "date": "2025-01-01", "event_type": "Loan Issued", "added": 100000,
         "deducted": 0, "principal_portion": 0, "interest_portion": 0}])
    assert as_dicts[0].kind == DISBURSED and as_dicts[0].amount == 100000
