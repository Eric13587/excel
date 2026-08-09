"""Writing a fund transaction, as one pipeline instead of three.

`add_savings_transaction` and `fund_add_transaction` did the same job with
different rules, and the difference was a bug rather than a nuance:

* savings applied ``+amount if type == "Deposit" else -amount``, so an
  ``Interest`` credit *reduced* the balance;
* the funds applied ``-amount if type == "Withdrawal" else +amount``, which is
  also what ``FundRepository.balance`` and ``.recalculate`` do.

So crediting 500 of interest on a 1,000 balance returned 500, stored 500, and
then silently became 1,500 the next time anything recalculated. Two answers to
one question, with the wrong one on the write path.

Validation was equally uneven. A negative amount and a malformed date were
caught — but by ``CHECK`` constraints, surfacing as
``TransactionError: CHECK constraint failed: amount >= 0``, which is a sentence
for a developer and not for a clerk.

THE STAGES
----------
``post()`` runs five, in order, and each is a plain function so it can be read
and tested on its own:

1. **normalise** — trim, round money to cents, canonicalise the type's case,
   accept the date formats the importer accepts;
2. **validate** — a known type, a positive amount, a real date, a member;
3. **guard** — the membership rules in :mod:`src.services.policy`;
4. **write** — one insert;
5. **repair** — re-derive the running totals when the row lands out of order.

Stages 1–3 are pure, so a dialog can run them to check a form before writing
anything. Stages 4–5 share one transaction, so a row is never left with a
running total that disagrees with it.
"""
import logging
from dataclasses import dataclass, replace
from datetime import datetime

from src.exceptions import LoanMasterError
from src.services.policy import Operation, default_policy

logger = logging.getLogger(__name__)

#: The three funds this posts to, and the vocabulary each accepts.
FUND_TYPES = {
    "savings": ("Deposit", "Withdrawal", "Interest"),
    "christmas_savings": ("Deposit", "Withdrawal", "Interest"),
    "benevolent_ledger": ("Contribution", "Withdrawal", "Interest"),
}

#: The one sign rule. Only a withdrawal takes money out; everything else —
#: deposit, contribution, interest — puts money in. This is what
#: FundRepository.balance and .recalculate have always done; the savings write
#: path disagreed, which is the bug this module exists to end.
WITHDRAWAL = "Withdrawal"

#: Which policy operation each fund's credits count as.
_ACCRUING_KIND = {"savings": "savings.deposit",
                  "christmas_savings": "christmas.deposit",
                  "benevolent_ledger": "benevolent.contribute"}


class PostingError(LoanMasterError, ValueError):
    """A fund transaction that cannot be written, explained in a sentence.

    Deriving from LoanMasterError is what marks the message as written *for the
    user*: src/views/error_handling.py shows those as a plain refusal rather
    than a crash. ValueError is kept so existing callers that catch it still do.
    """


@dataclass(frozen=True)
class Posting:
    """One fund transaction, before it is written."""
    table: str
    member_id: int
    date: str
    kind: str
    amount: float
    notes: str = ""
    batch_id: str = None

    @property
    def signed_amount(self):
        return -self.amount if self.kind == WITHDRAWAL else self.amount


# --------------------------------------------------------------------------- #
# 1. normalise
# --------------------------------------------------------------------------- #

def normalise(posting):
    """Tidy the inputs without judging them. Never raises."""
    kind = (posting.kind or "").strip()
    for known in FUND_TYPES.get(posting.table, ()):
        if kind.lower() == known.lower():
            kind = known
            break

    try:
        amount = round(float(posting.amount), 2)
    except (TypeError, ValueError):
        amount = posting.amount            # validate() reports it

    return replace(posting, kind=kind, amount=amount,
                   date=_normalise_date(posting.date),
                   notes=(posting.notes or "").strip())


def _normalise_date(value):
    """Accept the formats the importer accepts; leave anything else alone."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text,
                                     fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


# --------------------------------------------------------------------------- #
# 2. validate
# --------------------------------------------------------------------------- #

def validate(posting):
    """Everything wrong with it, as sentences. Empty means it is writable."""
    problems = []
    known = FUND_TYPES.get(posting.table)
    if known is None:
        problems.append(f"{posting.table!r} is not a fund.")
    elif posting.kind not in known:
        problems.append(f"{posting.kind!r} is not a {posting.table} transaction "
                        f"type ({', '.join(known)}).")

    if not isinstance(posting.amount, (int, float)):
        problems.append(f"{posting.amount!r} is not an amount.")
    elif posting.amount <= 0:
        # The sign lives in the type, not the number: a withdrawal of -500 would
        # otherwise read as money going in.
        problems.append("The amount must be greater than zero — use a "
                        "Withdrawal to take money out.")

    try:
        datetime.strptime(posting.date, "%Y-%m-%d")
    except (TypeError, ValueError):
        problems.append(f"{posting.date!r} is not a date (use YYYY-MM-DD).")

    if posting.member_id is None:
        problems.append("No member was given.")
    return problems


# --------------------------------------------------------------------------- #
# 3-5. the write path
# --------------------------------------------------------------------------- #

def check(db, posting, policy=default_policy):
    """Normalise, validate and guard, writing nothing.

    What a form should call to decide whether its Save button does anything.
    Returns ``(normalised_posting, [problem, ...])``.
    """
    tidy = normalise(posting)
    problems = validate(tidy)
    if problems:
        return tidy, problems

    kind = _ACCRUING_KIND.get(tidy.table)
    if kind and tidy.kind != WITHDRAWAL:
        # Only money going *in* is a new obligation. A member who has left may
        # still withdraw what they saved.
        problems.extend(r.reason for r in policy.check(
            db, Operation(kind, member_id=tidy.member_id, date=tidy.date,
                          amount=tidy.amount)))
    elif tidy.member_id is not None and db.conn.execute(
            "SELECT 1 FROM individuals WHERE id=?", (tidy.member_id,)).fetchone() is None:
        problems.append(f"Member {tidy.member_id} no longer exists.")
    return tidy, problems


def post(db, posting, policy=default_policy):
    """Run the whole pipeline and write. Returns the member's new balance.

    Raises :class:`PostingError` listing everything wrong, rather than letting a
    CHECK constraint surface as a transaction failure.
    """
    tidy, problems = check(db, posting, policy)
    if problems:
        raise PostingError(" ".join(problems))

    with db.transaction():
        db.funds.add(tidy.table, tidy.member_id, tidy.date, tidy.kind,
                     tidy.amount, 0.0, tidy.notes, tidy.batch_id)
        # The running total is a property of the whole ordered series, so it is
        # always derived rather than computed from "the balance right now" —
        # which is only correct when the row happens to be the newest.
        db.funds.recalculate(tidy.table, tidy.member_id)

    return db.funds.balance(tidy.table, tidy.member_id)
