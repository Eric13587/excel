"""Who is behind on a loan, and by how much.

The dashboard has always shown a count of loans in arrears and nothing else:
the number told you there was work to do but not whose. This turns that same
population into rows you can read, sort, export and act on.

WHAT COUNTS AS ARREARS
----------------------
Exactly what the KPI tile counts — an ``Active`` loan whose ``next_due_date``
has passed — so the list can never disagree with the number above it. A
suspended loan meets that test too (its due date only moves when catch-up
runs), and it is carried here *flagged* rather than dropped: a deliberate hold
is not a default, but it is also not something to hide from whoever is chasing
arrears. Callers filter on :data:`suspended` when they want one or the other.

WHAT IS DERIVED, AND HOW HONESTLY
---------------------------------
Days and missed instalments are arithmetic on the due date and are exact.
The *amount* overdue is not stored anywhere — the ledger records what was
received, not what was expected — so it is reconstructed as
``missed × installment``, capped at the outstanding balance. That is the
standard arrears figure and it is right for the ordinary case of a fixed
monthly instalment; on a loan whose instalment has changed it is an estimate,
which is why the column is labelled as one wherever it is shown.

Classification uses the SASRA bands already declared in :mod:`src.config`, for
their labels only. No provisioning is computed: the general ledger that would
have consumed it was withdrawn in migration 011.
"""
import logging
import math
from datetime import datetime

from dateutil.relativedelta import relativedelta

from src.config import SASRA_PROVISION_BANDS

logger = logging.getLogger(__name__)

_DATE = "%Y-%m-%d"

#: Band labels in order, for anything offering them as a filter.
BAND_LABELS = [label for label, *_rest in SASRA_PROVISION_BANDS]


def classify(days):
    """The SASRA band label for a number of days in arrears."""
    for label, low, high, _rate in SASRA_PROVISION_BANDS:
        if days >= low and (high is None or days <= high):
            return label
    return SASRA_PROVISION_BANDS[-1][0]


def _months_missed(due, as_of):
    """Instalments unpaid at ``as_of``, counting the overdue one itself.

    A loan due on the 1st and unread on the 15th has missed one instalment, not
    zero: the due date passed. Every whole month after that adds another.
    """
    elapsed = relativedelta(as_of, due)
    return max(1, elapsed.years * 12 + elapsed.months + 1)


def build_rows(loans, as_of=None):
    """Turn overdue loan records into arrears rows, worst first.

    ``loans`` are the dicts :meth:`LoanRepository.overdue` returns. Pure: no
    database and no clock of its own, so it can be tested against fixed dates.
    A row whose due date will not parse is skipped and logged rather than
    taking the whole report down with it.
    """
    as_of = as_of or datetime.now().strftime(_DATE)
    today = datetime.strptime(as_of, _DATE)

    rows = []
    for loan in loans:
        due_str = str(loan.get("next_due_date") or "")[:10]
        try:
            due = datetime.strptime(due_str, _DATE)
        except ValueError:
            logger.warning("Skipping loan %s: unreadable due date %r",
                           loan.get("ref"), loan.get("next_due_date"))
            continue

        days = (today - due).days
        if days <= 0:
            continue

        balance = float(loan.get("balance") or 0)
        installment = float(loan.get("installment") or 0)
        missed = _months_missed(due, today)
        # Never claim more is overdue than is owed at all.
        overdue_amount = min(missed * installment, balance) if installment else 0.0

        rows.append({
            "loan_id": loan.get("loan_id"),
            "individual_id": loan.get("individual_id"),
            "name": loan.get("name") or "",
            "pf_no": loan.get("pf_no") or "",
            "phone": loan.get("phone") or "",
            "ref": loan.get("ref") or "",
            "next_due_date": due_str,
            "days": days,
            "months_missed": missed,
            "installment": math.ceil(installment),
            "overdue_amount": math.ceil(overdue_amount),
            "balance": math.ceil(balance),
            "band": classify(days),
            "suspended": bool(loan.get("is_suspended")),
            "suspend_until": loan.get("suspend_until") or "",
            "retired": bool(loan.get("is_retired")),
            "employment_status": loan.get("employment_status") or "Active",
        })

    rows.sort(key=lambda r: (-r["days"], r["name"]))
    return rows


def summarize(rows):
    """Headline totals for a set of arrears rows.

    ``members`` counts people, not loans: one member three months behind on two
    loans is one person to call.
    """
    return {
        "loans": len(rows),
        "members": len({r["individual_id"] for r in rows}),
        "overdue_amount": sum(r["overdue_amount"] for r in rows),
        "balance": sum(r["balance"] for r in rows),
        "suspended": sum(1 for r in rows if r["suspended"]),
        "worst_days": max((r["days"] for r in rows), default=0),
    }


def get_arrears(db, as_of=None):
    """Arrears rows straight from a journal."""
    as_of = as_of or datetime.now().strftime(_DATE)
    return build_rows(db.loans.overdue(as_of), as_of)
