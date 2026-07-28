"""Replay every loan from asserted events and diff against what is stored.

Read-only. Opens the journal with ``mode=ro``, writes nothing, and changes
nothing. The point is to answer one question before any migration is
contemplated:

    if loans were stored as asserted events and everything else derived,
    would we get back the numbers we already have?

Where it agrees, the event model is sound for that loan. Where it disagrees,
either the derivation is wrong or the loan's history genuinely does not add up
— and both are worth knowing. That second case is how loan 483 surfaced.

    python -m tools.replay_loans loan_master.db
    python -m tools.replay_loans loan_master.db --verbose --limit 20

Events are reconstructed from what exists today:

    Loan Issued / Loan Top-Up rows -> Disbursed / ToppedUp
    Repayment / Loan Buyoff rows   -> PaymentReceived (the amount, asserted)
    loan_suspensions rows          -> Suspended

Interest accrual is deliberately *not* reconstructed. It is the thing being
derived — if replay reproduces the stored accruals from terms alone, the model
holds.

One reconstruction is approximate and worth stating: terms are read from the
current ``loans`` row, because no history of them is stored. That is precisely
the gap the proposal fills, so a loan whose terms changed mid-life is expected
to diverge here and would not diverge under the real model.
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.loan_derivation import (DISBURSED, PAYMENT_RECEIVED, SUSPENDED,
                                          TOPPED_UP, LoanEvent, derive)

TOLERANCE = 0.51        # money is float with ceil rounding in places


def load_loans(conn):
    return conn.execute(
        "SELECT l.id, l.individual_id, l.ref, i.name, l.status, l.principal, "
        "       l.balance, l.installment, l.monthly_interest, l.unearned_interest, "
        "       l.interest_balance, l.next_due_date "
        "FROM loans l JOIN individuals i ON i.id = l.individual_id "
        "ORDER BY l.id").fetchall()


def events_for(conn, loan):
    """Reconstruct the asserted events for one loan from existing rows."""
    (_lid, ind, ref, _name, _status, principal, _bal, installment,
     monthly_interest, unearned, _ibal, _ndd) = loan

    rows = conn.execute(
        "SELECT id, date, event_type, added, deducted, principal_portion, "
        "       interest_portion, is_edited "
        "FROM ledger WHERE individual_id=? AND loan_id=? ORDER BY date, id",
        (ind, ref)).fetchall()

    events, seq = [], 0
    for (rid, date, kind, added, deducted, p_por, i_por, _edited) in rows:
        seq += 1
        if kind == "Loan Issued":
            events.append(LoanEvent(
                date=date, kind=DISBURSED, seq=seq, amount=added or 0,
                installment=installment, monthly_interest=monthly_interest,
                # The issue row carries no unearned figure, so the loan's total
                # is attributed to the disbursement it belongs to.
                unearned_interest=_original_unearned(conn, ind, ref, principal),
                ref=rid))
        elif kind == "Loan Top-Up":
            events.append(LoanEvent(date=date, kind=TOPPED_UP, seq=seq,
                                    amount=added or 0, ref=rid))
        elif kind in ("Repayment", "Loan Buyoff"):
            events.append(LoanEvent(
                date=date, kind=PAYMENT_RECEIVED, seq=seq, amount=deducted or 0,
                principal=p_por, interest=i_por, ref=rid))
        # 'Interest Earned' is intentionally omitted: it is derived.

    for (start, until) in conn.execute(
            "SELECT start_date, suspend_until FROM loan_suspensions "
            "WHERE individual_id=? AND loan_ref=? ORDER BY start_date", (ind, ref)):
        events.append(LoanEvent(date=start, kind=SUSPENDED, seq=0, until=until))

    return events, rows


def _original_unearned(conn, ind, ref, principal):
    """Total interest the loan was written for, from its issue note.

    Migration 003 parses the same note for duration and rate. Falls back to the
    accruals actually posted, which is what the loan turned out to charge.
    """
    note = conn.execute(
        "SELECT notes FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Loan Issued' ORDER BY date, id LIMIT 1", (ind, ref)).fetchone()
    if note and note[0]:
        import re
        m = re.search(r"Total Interest:\s*([0-9.]+)", str(note[0]))
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    accrued = conn.execute(
        "SELECT COALESCE(SUM(added), 0) FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Interest Earned'", (ind, ref)).fetchone()[0]
    return float(accrued or 0)


def compare(loan, derived, rows):
    """Diff a replay against the stored ledger. Returns a list of findings."""
    (_lid, _ind, _ref, _name, status, _principal, balance, _inst,
     _mi, _un, _ibal, _ndd) = loan
    findings = []

    if abs((derived.principal_balance or 0) - (balance or 0)) > TOLERANCE:
        findings.append(("balance", f"derived {derived.principal_balance:,.2f} "
                                    f"vs stored {balance or 0:,.2f}"))

    stored_accruals = [r for r in rows if r[2] == "Interest Earned"]
    derived_accruals = [p for p in derived.postings if p.kind == "Interest Earned"]
    if len(stored_accruals) != len(derived_accruals):
        findings.append(("accrual count",
                         f"derived {len(derived_accruals)} vs stored {len(stored_accruals)}"))
    else:
        stored_total = round(sum(r[3] or 0 for r in stored_accruals), 2)
        if abs(derived.total_accrued - stored_total) > TOLERANCE:
            findings.append(("accrual total",
                             f"derived {derived.total_accrued:,.2f} vs "
                             f"stored {stored_total:,.2f}"))

    if status == "Paid" and abs(derived.principal_balance) > TOLERANCE:
        findings.append(("settled", f"marked Paid but replay leaves "
                                    f"{derived.principal_balance:,.2f}"))
    return findings


def classify(loan, rows, findings):
    """Why a loan diverged, in terms someone can act on."""
    kinds = {r[2] for r in rows}
    if not any(k in kinds for k in ("Loan Issued", "Loan Top-Up")):
        return "no disbursement recorded"
    if "Loan Top-Up" in kinds:
        return "has a top-up (terms changed mid-life, not stored)"
    if "Loan Buyoff" in kinds:
        return "has a buy-off"
    if any(r[7] for r in rows):
        return "has a manually edited entry"
    if any(f[0] == "accrual count" for f in findings):
        return "accrual schedule differs"
    return "other"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("journal")
    ap.add_argument("--verbose", action="store_true", help="list every divergence")
    ap.add_argument("--limit", type=int, default=12, help="divergences to show")
    args = ap.parse_args(argv)

    if not os.path.exists(args.journal):
        print(f"Cannot open {args.journal}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{args.journal}?mode=ro", uri=True)

    loans = load_loans(conn)
    exact, diverged, empty = [], [], []
    reasons = Counter()

    for loan in loans:
        events, rows = events_for(conn, loan)
        if not events:
            empty.append(loan)
            continue
        derived = derive(events)
        findings = compare(loan, derived, rows)
        if findings:
            reason = classify(loan, rows, findings)
            reasons[reason] += 1
            diverged.append((loan, findings, reason))
        else:
            exact.append(loan)

    total = len(loans)
    print(f"Replaying {total} loan(s) in {args.journal}\n")
    print(f"  reproduced exactly : {len(exact):>4}"
          f"   ({len(exact) / total * 100:.0f}%)" if total else "")
    print(f"  diverged           : {len(diverged):>4}")
    print(f"  no events to replay: {len(empty):>4}")

    if reasons:
        print("\nwhy they diverged:")
        for reason, n in reasons.most_common():
            print(f"  {n:>4}  {reason}")

    if diverged:
        print(f"\nfirst {min(args.limit, len(diverged))} divergence(s):")
        for loan, findings, reason in diverged[:args.limit]:
            print(f"  loan {loan[0]:<5} {loan[2]:<7} {loan[3][:26]:<28} {loan[4]:<7} {reason}")
            for what, detail in findings:
                print(f"        {what}: {detail}")

    if args.verbose and empty:
        print("\nloans with nothing to replay:")
        for loan in empty:
            print(f"  loan {loan[0]:<5} {loan[2]:<7} {loan[3][:26]}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
