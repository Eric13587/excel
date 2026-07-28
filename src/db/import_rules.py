"""Normalisation of rows copied from a foreign journal.

An import reads a database the application did not write. Until now it copied
every value straight through: a blank member name, a loan with no start date, a
date like ``01/02/2026``, an ``event_type`` the code has no branch for, a ledger
row that both adds and deducts, a negative savings amount — all landed in the
destination and the import reported ``success`` with no errors.

That is how a journal quietly acquires data no report can interpret, and it is
what stands between the current schema and the strict one in
``docs/db/proposed/004_strict_schema.sql``.

Two policies, chosen per field:

**Repair** where the intent is unambiguous — trim a name, title-case a status,
map a known synonym, collapse a nonzero edit-lock to 1. A warning is recorded
so the user sees what was changed.

**Reject** where it is not. A row whose ``event_type`` has no branch in the
code cannot be replayed into a balance; a date that could be either
``DD/MM`` or ``MM/DD`` cannot be guessed without risking a wrong figure in a
financial ledger. Rejected rows are counted and reported, never silently
dropped.

Nothing here writes; callers apply the result.
"""
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Vocabularies — these mirror the CHECK constraints migration 004 will add.
# --------------------------------------------------------------------------- #

LEDGER_EVENT_TYPES = ("Loan Issued", "Loan Top-Up", "Interest Earned",
                      "Repayment", "Loan Buyoff", "Loan Restructure")
LOAN_STATUSES = ("Active", "Paid")
SAVINGS_TYPES = ("Deposit", "Withdrawal", "Interest")
FUND_TYPES = ("Contribution", "Withdrawal", "Deposit", "Interest")

#: Legacy spellings seen in real journals, mapped to the current vocabulary.
EVENT_SYNONYMS = {
    "loan disbursed": "Loan Issued",
    "disbursement": "Loan Issued",
    "loan disbursement": "Loan Issued",
    "top-up": "Loan Top-Up",
    "top up": "Loan Top-Up",
    "loan topup": "Loan Top-Up",
    "interest": "Interest Earned",
    "interest accrued": "Interest Earned",
    "payment": "Repayment",
    "buyoff": "Loan Buyoff",
    "settlement": "Loan Buyoff",
    "restructure": "Loan Restructure",
}
SAVINGS_SYNONYMS = {"contribution": "Deposit", "credit": "Deposit", "debit": "Withdrawal"}

_ISO = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


@dataclass
class Normalised:
    """The outcome of normalising one row."""
    values: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    reason: str = None          # set when the row must not be imported

    @property
    def rejected(self):
        return self.reason is not None


def normalise_date(value):
    """An unambiguous ``YYYY-MM-DD``, or None.

    Accepts ``YYYY-MM-DD``, ``YYYY/MM/DD`` and ``YYYYMMDD``, plus anything with
    a trailing time part. Deliberately refuses ``01/02/2026``: it is either
    1 February or 2 January depending on the writer's locale, and guessing wrong
    puts a transaction in the wrong month. A rejected date is reported, not
    invented.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in (_ISO, _COMPACT):
        m = pattern.match(text)
        if m:
            year, month, day = m.groups()
            if 1 <= int(month) <= 12 and 1 <= int(day) <= 31:
                return f"{year}-{month}-{day}"
    return None


def _amount(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #

def normalise_member(row):
    """Trim identity fields; reject a member with no usable name.

    A nameless member cannot be matched, displayed or reconciled, and every
    loan and transaction imported for them would be unattributable.
    """
    out = Normalised()
    name = (row.get("name") or "").strip()
    if not name:
        out.reason = "member has no name"
        return out

    out.values["name"] = name
    if name != (row.get("name") or ""):
        out.warnings.append(f"name {row.get('name')!r} trimmed to {name!r}")

    for key in ("phone", "email", "pf_no", "id_no"):
        out.values[key] = (row.get(key) or "").strip()
    out.values["employment_status"] = (row.get("employment_status") or "Active").strip() or "Active"
    out.values["default_deduction"] = max(_amount(row.get("default_deduction")) or 0.0, 0.0)

    retired_date = normalise_date(row.get("retired_date"))
    is_retired = 1 if row.get("is_retired") else 0
    # The strict schema pairs the flag with the date; reconcile them here rather
    # than letting the constraint reject the member later.
    if is_retired and not retired_date:
        retired_date = None
        is_retired = 0
        out.warnings.append(f"{name}: retired flag dropped (no retirement date)")
    elif not is_retired and retired_date:
        retired_date = None
    out.values["is_retired"] = is_retired
    out.values["retired_date"] = retired_date
    return out


# --------------------------------------------------------------------------- #
# Loans
# --------------------------------------------------------------------------- #

def normalise_loan(row, fallback_start=None):
    """Normalise a loan. ``fallback_start`` is the loan's earliest ledger date."""
    out = Normalised()
    ref = (row.get("ref") or "").strip()
    if not ref:
        out.reason = "loan has no reference"
        return out
    out.values["ref"] = ref

    start = normalise_date(row.get("start_date")) or normalise_date(fallback_start)
    if not start:
        out.reason = f"loan {ref} has no usable start date"
        return out
    if start != (row.get("start_date") or ""):
        out.warnings.append(f"loan {ref}: start date {row.get('start_date')!r} -> {start}")
    out.values["start_date"] = start

    next_due = normalise_date(row.get("next_due_date"))
    out.values["next_due_date"] = next_due if next_due else ""

    status = (row.get("status") or "").strip().title()
    if status not in LOAN_STATUSES:
        # Conservative: an unrecognised status keeps the debt outstanding rather
        # than writing it off.
        out.warnings.append(f"loan {ref}: status {row.get('status')!r} -> 'Active'")
        status = "Active"
    out.values["status"] = status

    for key in ("principal", "total_amount", "balance", "installment",
                "monthly_interest", "unearned_interest", "interest_balance"):
        amount = _amount(row.get(key))
        if amount is None:
            out.reason = f"loan {ref}: {key} is not a number ({row.get(key)!r})"
            return out
        out.values[key] = amount
    if out.values["principal"] < 0 or out.values["installment"] < 0:
        out.reason = f"loan {ref}: negative principal or installment"
    return out


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def normalise_ledger(row):
    """Normalise one ledger row, or reject it with a reason."""
    out = Normalised()

    date = normalise_date(row.get("date"))
    if not date:
        out.reason = f"ledger row has an unreadable date ({row.get('date')!r})"
        return out
    if date != (row.get("date") or ""):
        out.warnings.append(f"ledger date {row.get('date')!r} -> {date}")
    out.values["date"] = date

    raw_event = (row.get("event_type") or "").strip()
    event = raw_event if raw_event in LEDGER_EVENT_TYPES else EVENT_SYNONYMS.get(raw_event.lower())
    if not event:
        # A row the recalculator has no branch for cannot be replayed into a
        # balance, so importing it would silently skew the member's position.
        out.reason = f"unknown ledger event type {raw_event!r}"
        return out
    if event != raw_event:
        out.warnings.append(f"event type {raw_event!r} -> {event!r}")
    out.values["event_type"] = event

    loan_ref = (row.get("loan_id") or "").strip()
    if not loan_ref:
        out.reason = f"{event} row on {date} has no loan reference"
        return out
    out.values["loan_id"] = loan_ref

    added, deducted = _amount(row.get("added")), _amount(row.get("deducted"))
    if added is None or deducted is None:
        out.reason = f"{event} row on {date} has a non-numeric amount"
        return out
    if added < 0 or deducted < 0:
        out.reason = f"{event} row on {date} has a negative amount"
        return out
    if added and deducted:
        # Every balance rule in the application assumes a row moves debt one
        # way; a two-sided row would be counted twice.
        out.reason = f"{event} row on {date} both adds ({added}) and deducts ({deducted})"
        return out
    out.values["added"], out.values["deducted"] = added, deducted

    for key in ("balance", "installment_amount", "interest_amount", "principal_balance",
                "interest_balance", "principal_portion", "interest_portion"):
        out.values[key] = _amount(row.get(key)) or 0.0

    # The edit lock is a flag; legacy rows stored the payment amount in it.
    raw_lock = row.get("is_edited") or 0
    out.values["is_edited"] = 1 if raw_lock not in (0, 1) or raw_lock == 1 else 0
    if raw_lock not in (0, 1):
        out.warnings.append(f"edit lock {raw_lock!r} on {date} normalised to 1")

    out.values["notes"] = row.get("notes") or ""
    out.values["batch_id"] = row.get("batch_id")
    out.values["previous_state"] = row.get("previous_state")
    return out


# --------------------------------------------------------------------------- #
# Savings and funds
# --------------------------------------------------------------------------- #

def normalise_fund_row(row, vocabulary=SAVINGS_TYPES, synonyms=SAVINGS_SYNONYMS, label="savings"):
    """Normalise a savings or fund transaction, or reject it."""
    out = Normalised()

    date = normalise_date(row.get("date"))
    if not date:
        out.reason = f"{label} row has an unreadable date ({row.get('date')!r})"
        return out
    out.values["date"] = date

    raw = (row.get("transaction_type") or "").strip()
    kind = raw if raw in vocabulary else (synonyms or {}).get(raw.lower())
    if not kind:
        out.reason = f"unknown {label} transaction type {raw!r}"
        return out
    if kind != raw:
        out.warnings.append(f"{label} type {raw!r} -> {kind!r}")
    out.values["transaction_type"] = kind

    amount = _amount(row.get("amount"))
    if amount is None:
        out.reason = f"{label} row on {date} has a non-numeric amount"
        return out
    if amount < 0:
        # The type carries the direction, so a negative amount would reverse it.
        out.reason = f"{label} row on {date} has a negative amount ({amount})"
        return out
    out.values["amount"] = amount
    out.values["balance"] = _amount(row.get("balance")) or 0.0
    out.values["notes"] = row.get("notes") or ""
    out.values["batch_id"] = row.get("batch_id")
    return out


def normalise_benevolent_row(row):
    return normalise_fund_row(row, FUND_TYPES, {"deposit": "Contribution"}, "benevolent")
