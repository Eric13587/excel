"""Writing a loan's derived values back into the ledger.

:mod:`src.services.loan_derivation` computes what a loan's postings *should*
be. This puts that answer into the `ledger` and `loans` rows the rest of the
application reads — the same shape the general ledger used before it was
withdrawn, and the same shape the funds use now: one function derives, one step
writes the result back.

WHY THIS REPLACES FOUR RECALCULATORS
------------------------------------
`recalculate_balances`, `recalculate_loan_history`,
`recalculate_smart_loan_ledger` and `rebuild_loan_schedule` each answer a
different part of "what changed and what may I regenerate?", and they disagree.
Measured on a loan whose repayment was edited from 5,750 to 9,000:

    recalculate_balances        principal 85,000   keeps the stale split
    recalculate_loan_history    principal 85,000   rewrites splits, not the balance
    derive() from stored rows   principal 85,000   replays the stale split
    derive() with the split free principal 81,750  correct

The derivation was never the problem. The problem is that a UI edit changes
`deducted` and leaves `principal_portion` behind, and every recalculator then
has to guess whether that stale split is an instruction or a leftover.

THE FIX IS TO STOP STORING THE GUESS
------------------------------------
A split is *derived*, so :func:`project_loan` recomputes it rather than
preserving it, and :func:`clear_derived` is what an edit calls so the derivation
is not handed a stale one to replay. After that there is one answer, and it is
the arithmetic one.

WHAT IS STILL ASSERTED
----------------------
The amount, the date and the event type of every row a human or payroll entered.
Those are facts and are never touched. Only the running balances, the
principal/interest split and the loan's cached position are rewritten.
"""
import logging

from src.services.loan_derivation import derive
from src.services.loan_events import reconstruct_for_loan

logger = logging.getLogger(__name__)

#: Ledger columns this owns. Everything else on a row is an assertion.
DERIVED_COLUMNS = ("balance", "principal_balance", "interest_balance",
                   "principal_portion", "interest_portion")


def clear_derived(db, row_id):
    """Forget a row's derived split, so the next projection recomputes it.

    Called when an edit changes an amount. Without it the derivation replays a
    split that belonged to the old amount — which is exactly how a 9,000
    repayment kept reducing principal by 5,000.
    """
    db.conn.execute(
        "UPDATE ledger SET principal_portion=0, interest_portion=0 WHERE id=?",
        (row_id,))


def project_loan(db, individual_id, loan_ref, commit=True):
    """Re-derive one loan and write the result into its rows.

    Returns the number of ledger rows whose derived values changed, so a caller
    can tell a correction from a no-op. On a healthy loan this is 0.
    """
    events = reconstruct_for_loan(db, individual_id, loan_ref)
    if not events:
        return 0
    result = derive(events)

    # Match derived postings to stored rows by (date, kind). The derivation
    # emits them in the same order the ledger stores them, so position within a
    # date is enough; a mismatch means the reconstruction lost a row, and it is
    # safer to leave the loan alone than to write half an answer.
    stored = db.conn.execute(
        "SELECT id, date, event_type, balance, principal_balance, interest_balance, "
        "       principal_portion, interest_portion FROM ledger "
        "WHERE individual_id=? AND loan_id=? AND event_type IN "
        "('Interest Earned','Repayment','Loan Buyoff') ORDER BY date, id",
        (individual_id, loan_ref)).fetchall()
    postings = [p for p in result.postings if p.kind in ("Interest Earned", "Repayment")]

    if len(stored) != len(postings):
        logger.warning(
            "Loan %s/%s: %d stored rows vs %d derived — not projecting",
            individual_id, loan_ref, len(stored), len(postings))
        return 0

    changed = 0
    for row, posting in zip(stored, postings):
        row_id, date = row[0], row[1]
        if str(date)[:10] != posting.date:
            logger.warning("Loan %s/%s: row %s dated %s vs derived %s — not projecting",
                           individual_id, loan_ref, row_id, date, posting.date)
            return 0
        gross = round(posting.principal_balance + posting.interest_balance, 2)
        wanted = (gross, posting.principal_balance, posting.interest_balance,
                  posting.principal_portion, posting.interest_portion)
        if tuple(round(v or 0, 2) for v in row[3:]) == wanted:
            continue
        db.conn.execute(
            "UPDATE ledger SET balance=?, principal_balance=?, interest_balance=?, "
            "principal_portion=?, interest_portion=? WHERE id=?",
            (*wanted, row_id))
        changed += 1

    loan = db.get_loan_by_ref(individual_id, loan_ref)
    if loan:
        # unearned_interest is deliberately NOT written. It is what the loan
        # was written to charge and has not charged yet — a term, not a
        # derivation. reconstruct() seeds the derivation with the interest
        # already accrued, so result.unearned_interest lands at zero for any
        # fully-replayed loan; writing it would erase the remaining balance of a
        # loan still running.
        status = "Active" if result.principal_balance > 0 else "Paid"
        db.conn.execute(
            "UPDATE loans SET balance=?, interest_balance=?, status=?, "
            "next_due_date=? WHERE id=?",
            (result.principal_balance, result.interest_balance, status,
             _next_due(db, individual_id, loan_ref, loan), loan["id"]))
    if commit:
        db.conn.commit()
    return changed


def _next_due(db, individual_id, loan_ref, loan):
    """When the next deduction falls due.

    Deliberately the same rule the old recalculators used — a month after the
    last repayment, or after the issue date when none has been taken (honouring
    the deduct_same_month setting) — rather than the derivation's own cursor.
    The two agree on a healthy loan, and preserving the existing rule keeps this
    a like-for-like replacement rather than a behavioural change smuggled in
    alongside one.
    """
    from datetime import datetime

    from dateutil.relativedelta import relativedelta

    row = db.conn.execute(
        "SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Repayment'",          # a buy-off ends the loan; it
        (individual_id, loan_ref)).fetchone()  # does not schedule another month
    last_repayment = row[0] if row else None
    base = last_repayment
    if not base:
        issued = db.conn.execute(
            "SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? "
            "AND event_type='Loan Issued'", (individual_id, loan_ref)).fetchone()
        base = (issued[0] if issued else None) or loan.get("start_date")
    if not base:
        return loan.get("next_due_date")
    try:
        base_dt = datetime.strptime(str(base).split()[0], "%Y-%m-%d")
    except (ValueError, TypeError):
        return loan.get("next_due_date")
    if last_repayment:
        return (base_dt + relativedelta(months=1)).strftime("%Y-%m-%d")
    same_month = db.settings.deduct_same_month
    return (base_dt if same_month else base_dt + relativedelta(months=1)).strftime("%Y-%m-%d")


def project_member(db, individual_id, commit=True):
    """Re-derive every loan a member holds. Returns the rows changed in total."""
    refs = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT ref FROM loans WHERE individual_id=?", (individual_id,))]
    changed = sum(project_loan(db, individual_id, ref, commit=False) for ref in refs)
    if commit:
        db.conn.commit()
    return changed
