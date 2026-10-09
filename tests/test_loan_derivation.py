"""The proposed loan model, as executable claims.

Each test is an argument the design makes. If one fails the design is wrong,
not the test.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.loan_derivation import (DISBURSED, PAYMENT_RECEIVED, SUSPENDED,
                                          TERMS_CHANGED, TOPPED_UP, LoanEvent,
                                          Terms, derive, terms_on)


def _disbursed(date="2025-01-01", principal=100000, installment=5750,
               monthly_interest=750, unearned=15000):
    return LoanEvent(date=date, kind=DISBURSED, seq=1, amount=principal,
                     installment=installment, monthly_interest=monthly_interest,
                     unearned_interest=unearned)


def _pay(date, amount=5750, seq=5):
    return LoanEvent(date=date, kind=PAYMENT_RECEIVED, seq=seq, amount=amount)


def _accruals(result):
    return [p for p in result.postings if p.kind == "Interest Earned"]


# --------------------------------------------------------------------------- #
# It is a function, not a procedure
# --------------------------------------------------------------------------- #

def test_the_same_events_always_give_the_same_answer():
    events = [_disbursed(), _pay("2025-02-01"), _pay("2025-03-01")]
    first, second = derive(events), derive(list(reversed(events)))
    assert first.principal_balance == second.principal_balance
    assert [(p.date, p.kind, p.deducted) for p in first.postings] == \
           [(p.date, p.kind, p.deducted) for p in second.postings]


def test_order_comes_from_the_events_not_from_insertion():
    """A back-dated entry belongs where its date puts it. Today the running
    balance column is computed from the balance at insert time, so a
    back-dated savings row lands with the wrong figure until something
    recalculates."""
    late = derive([_disbursed(), _pay("2025-03-01"), _pay("2025-02-01")])
    dates = [p.date for p in late.postings if p.kind == "Repayment"]
    assert dates == sorted(dates)


def test_no_events_is_not_a_crash():
    result = derive([])
    assert result.postings == [] and result.principal_balance == 0


# --------------------------------------------------------------------------- #
# Terms have a history — the piece the current schema is missing
# --------------------------------------------------------------------------- #

def test_terms_can_be_looked_up_for_any_date():
    result = derive([
        _disbursed(installment=5750),
        LoanEvent(date="2025-06-01", kind=TERMS_CHANGED, seq=1, installment=9000),
        _pay("2025-07-01", amount=9000),
    ])
    assert terms_on(result.terms_timeline, "2025-03-15").installment == 5750
    assert terms_on(result.terms_timeline, "2025-06-01").installment == 9000
    assert terms_on(result.terms_timeline, "2025-12-31").installment == 9000


def test_terms_before_the_first_event_are_empty_not_guessed():
    result = derive([_disbursed(date="2025-05-01")])
    assert terms_on(result.terms_timeline, "2025-01-01") == Terms()


# --------------------------------------------------------------------------- #
# The headline claim: deleting a past suspension
# --------------------------------------------------------------------------- #

def test_a_suspension_skips_months_without_charging_interest():
    events = [_disbursed(), LoanEvent(date="2025-03-01", kind=SUSPENDED, seq=0,
                                      until="2025-06-01")]
    result = derive(events, up_to="2025-08-01")
    skipped = [a.date for a in _accruals(result) if "2025-03" <= a.date[:7] <= "2025-05"]
    assert skipped == [], "no interest accrues in a suspended month"


def test_removing_a_suspension_restores_those_months_under_the_right_terms():
    """The question that has no answer today: which installment applied to the
    months a deleted suspension skipped? With dated terms it is a lookup, not
    a heuristic — and the answer is the terms in force *then*, not now."""
    disbursed = _disbursed(installment=5750, monthly_interest=750)
    suspension = LoanEvent(date="2025-03-01", kind=SUSPENDED, seq=0, until="2025-06-01")
    later_change = LoanEvent(date="2025-09-01", kind=TERMS_CHANGED, seq=1,
                             installment=9000, monthly_interest=1200)

    with_suspension = derive([disbursed, suspension, later_change], up_to="2025-12-01")
    without = derive([disbursed, later_change], up_to="2025-12-01")

    assert len(_accruals(without)) > len(_accruals(with_suspension))
    restored = [a for a in _accruals(without) if "2025-03" <= a.date[:7] <= "2025-05"]
    assert restored, "the skipped months come back"
    assert all(a.added == 750 for a in restored), \
        "under the terms in force then, not the 1200 that came later"


def test_an_indefinite_suspension_never_reopens():
    result = derive([_disbursed(), LoanEvent(date="2025-03-01", kind=SUSPENDED,
                                             seq=0, until=None)],
                    up_to="2026-06-01")
    assert not [a for a in _accruals(result) if a.date >= "2025-03-01"]


# --------------------------------------------------------------------------- #
# Top-ups and manual amounts stop being special cases
# --------------------------------------------------------------------------- #

def test_a_top_up_is_an_event_not_a_recalculation_mode():
    result = derive([
        _disbursed(principal=100000, unearned=15000),
        LoanEvent(date="2025-06-01", kind=TOPPED_UP, seq=1, amount=50000,
                  installment=9000, monthly_interest=1200, unearned_interest=7500),
    ], up_to="2025-07-01")
    assert result.principal_balance == 150000
    assert terms_on(result.terms_timeline, "2025-07-01").installment == 9000


def test_a_manual_payment_amount_needs_no_edit_flag():
    """is_edited exists so a hand-changed repayment survives a replay that
    would otherwise regenerate it. Asserting the amount removes the need."""
    result = derive([_disbursed(), _pay("2025-02-01", amount=20000)])
    payment = [p for p in result.postings if p.kind == "Repayment"][0]
    assert payment.deducted == 20000
    assert result.principal_balance < 100000 - 5750


def test_an_asserted_split_is_respected():
    result = derive([
        _disbursed(),
        LoanEvent(date="2025-02-01", kind=PAYMENT_RECEIVED, seq=5, amount=6389,
                  principal=5555, interest=834),
    ])
    payment = [p for p in result.postings if p.kind == "Repayment"][0]
    assert (payment.principal_portion, payment.interest_portion) == (5555, 834)


def test_interest_is_settled_before_principal_when_no_split_is_given():
    result = derive([_disbursed(), _pay("2025-02-01", amount=5750)])
    payment = [p for p in result.postings if p.kind == "Repayment"][0]
    assert payment.interest_portion == 750
    assert payment.principal_portion == 5000


# --------------------------------------------------------------------------- #
# Accrual behaviour
# --------------------------------------------------------------------------- #

def test_accrual_stops_when_the_interest_is_used_up():
    """min(monthly_interest, unearned) — the loan never charges more than it
    was written for."""
    result = derive([_disbursed(monthly_interest=750, unearned=1500)],
                    up_to="2026-01-01")
    accruals = _accruals(result)
    assert len(accruals) == 2
    assert result.unearned_interest == 0


def test_replaying_as_at_a_past_date_stops_there():
    result = derive([_disbursed()], up_to="2025-04-01")
    assert all(a.date <= "2025-04-01" for a in _accruals(result))
    assert len(_accruals(result)) == 3
