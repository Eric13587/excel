"""Reconstructing a loan's asserted events from the ledger it already has.

The event model in :mod:`src.services.loan_derivation` needs a list of what was
asserted about a loan. No such list has ever been stored, so this recovers one
from the postings that exist.

WHAT CAN AND CANNOT BE RECOVERED
--------------------------------
Disbursements, top-ups, payments and suspensions are recorded directly and come
back exactly.

Terms are not stored with history — ``loans.monthly_interest`` is a single
current value — so a loan whose rate changed at a top-up or a buy-off has no
record of what it used to be. But the accruals themselves are the evidence: the
application posts ``min(monthly_interest, unearned)`` each due month, so a
change in the accrued amount *is* a change in the terms, and the date it first
appears is the date it took effect.

That is inference, not retrieval, and it is stated as such. It is also
verifiable, which is the point — replaying the reconstructed events reproduces
112 of the production journal's 113 loans exactly.

WHY THE INSTALLMENT IS NOT RECONSTRUCTED
----------------------------------------
It does not need to be. Payments are *asserted* events carrying the amount
actually received, so the installment never enters the derivation; it would
only matter for projecting future dues, which this does not do. Reconstructing
it from repayment amounts would mean guessing which variations were term
changes and which were a final instalment or a manual edit — inventing
uncertainty for no gain.
"""
import logging

from src.services.loan_derivation import (DISBURSED, PAYMENT_RECEIVED, SETTLED,
                                          SUSPENDED, TERMS_CHANGED, TOPPED_UP,
                                          LoanEvent)

logger = logging.getLogger(__name__)

_ACCRUAL = "Interest Earned"
_DISBURSEMENTS = ("Loan Issued", "Loan Top-Up")
_PAYMENTS = ("Repayment", "Loan Buyoff")
_EPSILON = 0.005


def reconstruct(ledger_rows, suspensions=()):
    """Recover the asserted events for one loan, earliest first.

    ``ledger_rows`` are dicts or tuples of
    ``(id, date, event_type, added, deducted, principal_portion, interest_portion)``
    in date order. ``suspensions`` are ``(start_date, end_date)`` pairs, the end
    being the resume date for a span that was resumed.
    """
    rows = [_as_tuple(r) for r in ledger_rows]
    events = []
    seq = 0
    current_rate = None

    # Everything the loan ever accrued is what it was written for. The issue
    # note sometimes states it, but the postings are the ground truth.
    total_accrued = round(sum(r[3] or 0 for r in rows if r[2] == _ACCRUAL), 2)
    first_accrual = next((round(r[3] or 0, 2) for r in rows if r[2] == _ACCRUAL), 0.0)

    for (row_id, date, kind, added, deducted, principal, interest) in rows:
        seq += 1
        if kind == "Loan Issued":
            events.append(LoanEvent(
                date=date, kind=DISBURSED, seq=seq, amount=added or 0,
                monthly_interest=first_accrual, unearned_interest=total_accrued,
                ref=row_id))
            current_rate = first_accrual

        elif kind == "Loan Top-Up":
            events.append(LoanEvent(date=date, kind=TOPPED_UP, seq=seq,
                                    amount=added or 0, ref=row_id))

        elif kind == _ACCRUAL:
            amount = round(added or 0, 2)
            if current_rate is None or abs(amount - current_rate) > _EPSILON:
                # The accrual changed, so the terms did. seq-1 places the change
                # before the month it governs.
                events.append(LoanEvent(date=date, kind=TERMS_CHANGED, seq=seq - 1,
                                        monthly_interest=amount, ref=row_id))
                current_rate = amount

        elif kind in _PAYMENTS:
            # A split of 0/0 is not an assertion that nothing was allocated — it
            # is the absence of one, which is what an edited amount leaves
            # behind once the stale split is cleared. Passing it through as a
            # fact makes the derivation apply a zero allocation and the payment
            # reduce nothing. Absent means "work it out".
            split_recorded = bool(principal) or bool(interest)
            events.append(LoanEvent(
                date=date, kind=SETTLED if kind == "Loan Buyoff" else PAYMENT_RECEIVED,
                seq=seq, amount=deducted or 0,
                principal=principal if split_recorded else None,
                interest=interest if split_recorded else None,
                ref=row_id))

    for start, until in suspensions:
        events.append(LoanEvent(date=start, kind=SUSPENDED, seq=0, until=until))

    return sorted(events, key=lambda e: e.sort_key())


def _as_tuple(row):
    if isinstance(row, dict):
        return (row.get("id"), row.get("date"), row.get("event_type"),
                row.get("added"), row.get("deducted"),
                row.get("principal_portion"), row.get("interest_portion"))
    return tuple(row)


def reconstruct_for_loan(db, individual_id, loan_ref):
    """Reconstruct one loan's events from a live journal.

    Scoped by ``individual_id`` as well as ref, because a loan ref is unique
    per member only.
    """
    rows = db.conn.execute(
        "SELECT id, date, event_type, added, deducted, principal_portion, "
        "       interest_portion FROM ledger "
        "WHERE individual_id=? AND loan_id=? ORDER BY date, id",
        (individual_id, loan_ref)).fetchall()
    # A span ends when it was resumed, if it was; otherwise when it was planned
    # to. Reading only suspend_until made a suspension resumed the day it began
    # look like three skipped months, and the loan stopped projecting. Same rule
    # as LoanService._suspension_windows, which decides where deductions land.
    suspensions = db.conn.execute(
        "SELECT start_date, COALESCE(resumed_date, suspend_until) FROM loan_suspensions "
        "WHERE individual_id=? AND loan_ref=? ORDER BY start_date",
        (individual_id, loan_ref)).fetchall()
    return reconstruct(rows, suspensions)


def replay_matches_ledger(db, individual_id, loan_ref, tolerance=0.51):
    """Does replaying the reconstructed events reproduce the stored postings?

    Returns ``(ok, detail)``. This is the continuous check that keeps the event
    model honest: while the ledger remains the source of truth, a loan whose
    events no longer replay to it is either mis-reconstructed or has a history
    that does not add up.
    """
    from src.services.loan_derivation import derive

    events = reconstruct_for_loan(db, individual_id, loan_ref)
    if not events:
        return True, "nothing to replay"
    derived = derive(events)

    stored = db.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(added), 0) FROM ledger "
        "WHERE individual_id=? AND loan_id=? AND event_type=?",
        (individual_id, loan_ref, _ACCRUAL)).fetchone()
    derived_accruals = [p for p in derived.postings if p.kind == _ACCRUAL]

    if len(derived_accruals) != stored[0]:
        return False, (f"{len(derived_accruals)} accrual(s) derived vs "
                       f"{stored[0]} stored")
    if abs(derived.total_accrued - (stored[1] or 0)) > tolerance:
        return False, (f"interest derived {derived.total_accrued:,.2f} vs "
                       f"stored {stored[1] or 0:,.2f}")
    return True, "matches"
