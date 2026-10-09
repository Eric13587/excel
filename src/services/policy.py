"""Rules that run *before* an operation, as a pipeline.

The gap this closes: `individuals.is_retired` and `employment_status` existed and
were consulted only by reports, so nothing stopped a monthly catch-up from
generating a year of contributions for somebody who left the SACCO in March.
The status was decoration.

WHY A PIPELINE RATHER THAN `if` STATEMENTS
------------------------------------------
The same question — *may this happen?* — is asked from eight catch-up entry
points, the mass-operation dialogs, and every single-transaction path. Written
inline it would be eight copies drifting apart, each with its own message, and
adding a rule would mean finding all of them. As a pipeline there is one list,
one place to add a rule, and one place to read to know what the system refuses.

It also separates *deciding* from *doing*. A rule is a pure function of
(journal, operation) with no side effects, so the mass dialogs can ask
:meth:`Policy.check` to grey out ineligible members **before** running anything,
and the services can call :meth:`Policy.enforce` at the point of writing. Same
rules, same wording, two uses.

WHAT IS DELIBERATELY NOT BLOCKED
--------------------------------
Leaving the SACCO does not erase what you owe or what you are owed. A member who
has retired must still be able to repay an outstanding loan and withdraw their
savings, and the books must still be correctable. So the line is drawn at
*creating new obligations*, not at touching the account:

* **refused** — catch-up, mass deduction, a new loan, a new scheduled
  contribution: all of these invent activity going forward;
* **allowed** — repayments, withdrawals, payouts, edits and corrections.

Blocking everything would trap a retired member's own money, which is a worse
failure than the one being fixed.
"""
import logging
from dataclasses import dataclass

from src.exceptions import LoanMasterError

logger = logging.getLogger(__name__)

# Membership states. Only ACTIVE may accrue new obligations; the rest are all
# forms of "has left", distinguished because the SACCO reports on them
# differently, not because the software treats them differently.
ACTIVE = "Active"
RETIRED = "Retired"
RESIGNED = "Resigned"
TERMINATED = "Terminated"
DECEASED = "Deceased"
SUSPENDED = "Suspended"

MEMBERSHIP_STATUSES = (ACTIVE, RETIRED, RESIGNED, TERMINATED, DECEASED, SUSPENDED)
INACTIVE_STATUSES = tuple(s for s in MEMBERSHIP_STATUSES if s != ACTIVE)

# How each reads in a sentence: "… has retired", "… is deceased".
_READS = {RETIRED: "has retired", RESIGNED: "has resigned",
          TERMINATED: "has been terminated", DECEASED: "is deceased",
          SUSPENDED: "is suspended"}


@dataclass(frozen=True)
class Operation:
    """What is about to happen, described well enough to judge it.

    ``kind`` is a dotted name — ``savings.catch_up``, ``loan.issue`` — so a rule
    can match a family (``kind.startswith("savings.")``) without knowing every
    member of it.
    """
    kind: str
    member_id: int = None
    date: str = None
    amount: float = None
    loan_ref: str = None

    @property
    def family(self):
        return self.kind.split(".", 1)[0]


@dataclass(frozen=True)
class Refusal:
    """Why an operation may not proceed. Carries text a user can act on."""
    rule: str
    reason: str
    member_id: int = None

    def __str__(self):
        return self.reason


class OperationNotAllowed(LoanMasterError):
    """Raised by :meth:`Policy.enforce` when a rule refuses.

    A LoanMasterError because its message is written for the person reading it;
    see src/views/error_handling.py.
    """

    def __init__(self, refusals):
        self.refusals = list(refusals)
        super().__init__("; ".join(r.reason for r in self.refusals))


# --------------------------------------------------------------------------- #
# Rules. Each is (journal, operation) -> Refusal | None, and must not write.
# --------------------------------------------------------------------------- #

#: Operations that create new obligations, and so require an active member.
ACCRUING_OPERATIONS = (
    "loan.issue", "loan.top_up", "loan.catch_up", "loan.deduct",
    "savings.catch_up", "savings.deposit",
    "christmas.catch_up", "christmas.deposit",
    "benevolent.catch_up", "benevolent.contribute", "benevolent.enrol",
)


def member_must_exist(db, op):
    if op.member_id is None:
        return None
    row = db.conn.execute("SELECT 1 FROM individuals WHERE id=?", (op.member_id,)).fetchone()
    if row is None:
        return Refusal("member_must_exist",
                       f"Member {op.member_id} no longer exists.", op.member_id)
    return None


def member_must_be_active(db, op):
    """New obligations require a member who is still in the SACCO.

    Repayments, withdrawals and corrections are not listed in
    ACCRUING_OPERATIONS and so pass — a member who has left still owes what they
    owe and is still owed what they saved.
    """
    if op.kind not in ACCRUING_OPERATIONS or op.member_id is None:
        return None
    row = db.conn.execute(
        "SELECT name, employment_status, is_retired, retired_date "
        "FROM individuals WHERE id=?", (op.member_id,)).fetchone()
    if row is None:
        return None                     # member_must_exist reports this
    name, status, is_retired, retired_date = row
    status = status or (RETIRED if is_retired else ACTIVE)
    if status == ACTIVE and not is_retired:
        return None
    reads = _READS.get(status, f"is marked {status}")
    since = f" ({retired_date})" if retired_date else ""
    return Refusal(
        "member_must_be_active",
        f"{name} {reads}{since}, so no new {op.family} activity can be recorded. "
        f"Repayments and withdrawals are still allowed.",
        op.member_id)


def date_must_not_precede_departure(db, op):
    """A contribution dated after someone left is almost always a mistake.

    Deliberately only refuses *after* the departure date, so genuine
    back-dated corrections covering their working period still go through.
    """
    if op.kind not in ACCRUING_OPERATIONS or not op.date or op.member_id is None:
        return None
    row = db.conn.execute(
        "SELECT name, retired_date FROM individuals WHERE id=? AND retired_date IS NOT NULL "
        "AND retired_date <> ''", (op.member_id,)).fetchone()
    if row and op.date > row[1]:
        return Refusal(
            "date_must_not_precede_departure",
            f"{op.date} is after {row[0]} left on {row[1]}.", op.member_id)
    return None


#: The order rules run in. Existence first, so later rules can assume the row.
DEFAULT_RULES = (member_must_exist, member_must_be_active,
                 date_must_not_precede_departure)


class Policy:
    """Runs the rules. Stateless; construct freely."""

    def __init__(self, rules=DEFAULT_RULES):
        self.rules = tuple(rules)

    def check(self, db, op):
        """Every refusal, without raising — for greying out a UI in advance."""
        out = []
        for rule in self.rules:
            try:
                refusal = rule(db, op)
            except Exception:               # noqa: BLE001 - a broken rule must not block work
                logger.exception("Policy rule %s failed on %s", rule.__name__, op.kind)
                continue
            if refusal is not None:
                out.append(refusal)
        return out

    def allows(self, db, op):
        return not self.check(db, op)

    def enforce(self, db, op):
        """Raise :class:`OperationNotAllowed` if anything refuses."""
        refusals = self.check(db, op)
        if refusals:
            raise OperationNotAllowed(refusals)

    def partition(self, db, kind, member_ids, date=None):
        """Split members into (allowed, [(member_id, refusal), ...]).

        What a mass operation needs: run for those it may, and report the rest
        rather than failing partway through a batch.
        """
        allowed, blocked = [], []
        for member_id in member_ids:
            refusals = self.check(db, Operation(kind, member_id=member_id, date=date))
            if refusals:
                blocked.append((member_id, refusals[0]))
            else:
                allowed.append(member_id)
        return allowed, blocked


#: The shared instance. A different rule set is only needed in tests.
default_policy = Policy()
