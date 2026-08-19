"""A members × months grid over the three fund subledgers.

Editing a fund today means opening one member, finding the tab, and working a
row at a time. For the monthly job — "everybody paid their 2,000 in March
except these four" — that is the wrong shape entirely: the data is a table and
the interface is a stack of index cards.

This module is the model behind a spreadsheet-shaped view of the same data.
Rows are members, columns are months, a cell is what moved in that member-month.
It is deliberately **separate from the per-member screens and changes none of
them**: every write goes through :mod:`src.services.posting` and
:class:`~src.db.fund.FundRepository`, exactly the paths the ledger view uses, so
balances, validation, membership policy and the audit trail behave identically
whichever screen the edit came from.

WHAT A CELL CAN AND CANNOT BE
-----------------------------
A member-month may hold no rows, one row, or several — a deposit and a
correction, a contribution and a payout. One editable number can only ever
represent the first two honestly. So:

* **empty** — typing a number inserts one credit row (:data:`FundSpec.credit`),
  dated by :func:`default_day`;
* **one row** — typing changes that row's amount; clearing it deletes the row;
* **several rows** — the cell shows the net and is **read only**, flagged
  :data:`CellState.MIXED`. Collapsing three transactions into one number would
  either destroy two of them or invent an allocation between them. The view
  offers to open that month instead.

A negative number means money out: it is written as a ``Withdrawal``, which is
the sign convention the whole application already uses (the sign lives in the
transaction type, never in the stored amount — see :mod:`src.services.posting`).

WHAT IS REFUSED, AND WHY IT IS REPORTED PER CELL
------------------------------------------------
A batch is not all-or-nothing at the cell level: one member having left the
SACCO must not stop the other ninety edits from saving. :func:`apply_changes`
returns what it wrote *and* what it would not write, each with the sentence
explaining it, so the view can leave those cells dirty and say why. The whole
batch still shares one database transaction, so a genuine failure rolls back
everything rather than leaving the funds half-written.

Refusals come from the existing rules, not from new ones invented here:

* a member who has left may not receive new contributions
  (:mod:`src.services.policy`) — but may still have a withdrawal recorded;
* a Christmas withdrawal outside the unlock month
  (:class:`~src.exceptions.ChristmasLockedError`);
* anything :func:`src.services.posting.validate` rejects.

THE BENEVOLENT SCHEDULE
-----------------------
Benevolent contributions are driven by ``benevolent_accounts.next_due_date``,
which its catch-up walks forward. A row written straight into the ledger — by
this grid or by anything else — leaves that pointer behind, and the next
catch-up then re-posts every month the grid already filled. So a batch that
touches the benevolent fund re-derives the pointer from the contributions that
now exist (:func:`resync_benevolent_schedule`). This is the one piece of
book-keeping the grid must do that the per-member screens never had to.
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from dateutil.relativedelta import relativedelta

from src.db.fund import BENEVOLENT, CHRISTMAS, SAVINGS
from src.services.policy import Operation, default_policy
from src.services.posting import Posting, check as check_posting

logger = logging.getLogger(__name__)

_DATE = "%Y-%m-%d"


# --------------------------------------------------------------------------- #
# What differs between the three funds
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FundSpec:
    """The handful of things that are not the same across the three funds."""

    table: str
    label: str
    credit: str                 # the transaction type money-in is recorded as
    noun: str                   # what one credit is called, for messages
    policy_kind: str            # the policy Operation for a new credit
    scheduled: bool = False     # driven by benevolent_accounts.next_due_date
    locks_withdrawals: bool = False   # Christmas: only in the unlock month


FUNDS = {
    SAVINGS: FundSpec(SAVINGS, "Savings / Shares", "Deposit", "deposit",
                      "savings.deposit"),
    CHRISTMAS: FundSpec(CHRISTMAS, "Christmas", "Deposit", "deposit",
                        "christmas.deposit", locks_withdrawals=True),
    BENEVOLENT: FundSpec(BENEVOLENT, "Benevolent", "Contribution", "contribution",
                         "benevolent.contribute", scheduled=True),
}

#: Order the funds are offered in — the order they appear on the member screen.
FUND_ORDER = (SAVINGS, CHRISTMAS, BENEVOLENT)

WITHDRAWAL = "Withdrawal"


def spec(fund):
    """The :class:`FundSpec` for a table name, or raise for anything else."""
    try:
        return FUNDS[fund]
    except KeyError:
        raise ValueError(f"{fund!r} is not a fund grid table; "
                         f"expected one of {sorted(FUNDS)}") from None


# --------------------------------------------------------------------------- #
# Months
# --------------------------------------------------------------------------- #

def month_key(date_str):
    """``'2026-03-14'`` → ``'2026-03'``. The column a row belongs to."""
    return str(date_str)[:7]


def month_range(start, end):
    """Every month key from ``start`` to ``end`` inclusive, earliest first.

    Both bounds are ``YYYY-MM`` or ``YYYY-MM-DD``. An end before the start is
    an empty range rather than an error — a date picker can produce one while
    the user is still choosing.
    """
    first = datetime.strptime(str(start)[:7] + "-01", _DATE)
    last = datetime.strptime(str(end)[:7] + "-01", _DATE)
    out = []
    while first <= last:
        out.append(first.strftime("%Y-%m"))
        first += relativedelta(months=1)
    return out


def month_bounds(months):
    """The (first day, last day) covering a list of month keys."""
    if not months:
        return None, None
    start = months[0] + "-01"
    last = datetime.strptime(months[-1] + "-01", _DATE) + relativedelta(months=1)
    return start, (last - relativedelta(days=1)).strftime(_DATE)


def month_label(key):
    """``'2026-03'`` → ``'Mar 2026'``."""
    return datetime.strptime(key + "-01", _DATE).strftime("%b %Y")


# --------------------------------------------------------------------------- #
# The loaded grid
# --------------------------------------------------------------------------- #

class CellState:
    """Why a cell is the way it is. The view turns these into colour and tooltip."""

    EMPTY = "empty"         # nothing this month; typing inserts
    SINGLE = "single"       # exactly one row; typing edits it
    MIXED = "mixed"         # several rows; read-only, open the month instead


@dataclass
class Cell:
    """One member-month."""

    net: float = 0.0
    rows: list = field(default_factory=list)   # [{id, date, transaction_type, amount, notes}]

    @property
    def state(self):
        if not self.rows:
            return CellState.EMPTY
        return CellState.SINGLE if len(self.rows) == 1 else CellState.MIXED

    @property
    def editable(self):
        return self.state != CellState.MIXED

    @property
    def row_id(self):
        """The single row's id, or None when empty or mixed."""
        return self.rows[0]["id"] if self.state == CellState.SINGLE else None

    def fingerprint(self):
        """What this cell looked like when it was read.

        Compared again at save time so an edit made from another screen while
        the grid sat open is refused rather than silently overwritten.
        """
        return tuple(sorted((r["id"], round(float(r["amount"] or 0), 2),
                             str(r["date"])[:10], r["transaction_type"])
                            for r in self.rows))


@dataclass
class MemberRow:
    """A member as the grid needs them: identity, standing and their cells."""

    member_id: int
    name: str
    pf_no: str = ""
    status: str = "Active"
    retired: bool = False
    enrolled: bool = True        # benevolent: has an active enrolment
    monthly_amount: float = 0.0  # benevolent: the enrolled amount
    cells: dict = field(default_factory=dict)      # month key -> Cell
    opening_balance: float = 0.0                   # before the first column
    total_balance: float = 0.0                     # the whole fund, all time
    #: Left out of this ledger's grid by a deliberate decision. Kept as a row
    #: rather than filtered away, so "excluded" is visible and reversible.
    excluded: bool = False
    exclusion_note: str = ""
    #: The month this member's account in this fund began, or None if unknown.
    #: Months before it are months the account did not exist for — which is not
    #: the same as months with no contribution, and must not be filled.
    account_start: str = None

    @property
    def key(self):
        """What staged edits are keyed by. A fund row is one member."""
        return self.member_id

    @property
    def label(self):
        return self.name

    @property
    def active(self):
        return not self.retired and (self.status or "Active") == "Active"

    def cell(self, month):
        return self.cells.get(month) or Cell()

    def period_total(self, months):
        return round(sum(self.cell(m).net for m in months), 2)


@dataclass
class Grid:
    """Everything one fund's grid needs to draw itself."""

    fund: str
    months: list
    members: list                                  # [MemberRow]

    @property
    def spec(self):
        return spec(self.fund)

    def column_total(self, month):
        return round(sum(m.cell(month).net for m in self.members), 2)

    def grand_total(self):
        return round(sum(m.period_total(self.months) for m in self.members), 2)


def load(db, fund, months, include_inactive=True, include_excluded=False):
    """Read one fund's grid for a list of month keys.

    Three queries in total regardless of how many members there are: the member
    directory, the movements in the window, and (for benevolent) the enrolments.
    Balances are read per member from the existing summed reader, which is the
    one place a fund balance is defined.
    """
    fund_spec = spec(fund)
    start, end = month_bounds(months)
    if start is None:
        return Grid(fund=fund, months=[], members=[])

    movements = defaultdict(lambda: defaultdict(Cell))
    for row in db.funds.movements_in_range(fund, start, end):
        cell = movements[row["individual_id"]][month_key(row["date"])]
        amount = round(float(row["amount"] or 0), 2)
        cell.net = round(cell.net + (-amount if row["transaction_type"] == WITHDRAWAL
                                     else amount), 2)
        cell.rows.append(row)

    first_seen = db.funds.first_movement_dates(fund)

    enrolments = {}
    enrolled_from = {}
    if fund_spec.scheduled:
        for row in db.funds.all_enrolments():
            enrolments[row["individual_id"]] = (float(row["monthly_amount"] or 0),
                                                bool(row["active"]))
            if row.get("start_date"):
                enrolled_from[row["individual_id"]] = month_key(row["start_date"])

    excluded = db.members.excluded_from(fund)

    members = []
    for person in db.members.summaries():
        member_id = person["id"]
        if member_id in excluded and not include_excluded:
            continue
        retired = bool(person.get("is_retired"))
        status = person.get("employment_status") or "Active"
        if not include_inactive and (retired or status != "Active"):
            continue
        amount, enrolled = enrolments.get(member_id, (0.0, not fund_spec.scheduled))
        cells = dict(movements.get(member_id, {}))
        total = db.funds.balance(fund, member_id)
        # When this account began. An enrolment date is an explicit statement of
        # it; a first movement is evidence of it. Where both exist the earlier
        # wins, since the account cannot have started after money moved in it.
        candidates = [month_key(d) for d in
                      (first_seen.get(member_id), enrolled_from.get(member_id)) if d]
        members.append(MemberRow(
            excluded=member_id in excluded,
            exclusion_note=excluded.get(member_id, ""),
            account_start=min(candidates) if candidates else None,
            member_id=member_id, name=person.get("name") or "",
            pf_no=person.get("pf_no") or "", status=status, retired=retired,
            enrolled=enrolled, monthly_amount=amount, cells=cells,
            total_balance=total,
            # What the member held before the first visible column: the whole
            # balance less everything shown, so the columns and the balance add up.
            opening_balance=round(total - sum(c.net for c in cells.values()), 2),
        ))
    return Grid(fund=fund, months=months, members=members)


# --------------------------------------------------------------------------- #
# Staged changes
# --------------------------------------------------------------------------- #

class ChangeKind:
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Change:
    """One staged cell edit, in terms of what it does to the ledger."""

    fund: str
    member_id: int
    month: str
    kind: str                       # ChangeKind
    amount: float = 0.0             # signed: negative means money out
    row_id: int = None              # UPDATE/DELETE: the row it acts on
    before: tuple = ()              # the cell's fingerprint when it was read
    notes: str = ""

    @property
    def key(self):
        """The row this change belongs to — see :attr:`MemberRow.key`."""
        return self.member_id

    @property
    def transaction_type(self):
        return WITHDRAWAL if self.amount < 0 else spec(self.fund).credit

    @property
    def magnitude(self):
        """The unsigned amount, which is how every fund table stores it."""
        return round(abs(self.amount), 2)


def stage(grid, member_row, month, text):
    """Turn what somebody typed into a :class:`Change`, or return a refusal.

    Returns ``(change, problem)`` — exactly one of which is None. Blank (or a
    dash, which is how the grid shows an empty cell) means "no movement": that
    deletes the row if there is one and is a no-op if there is not.
    """
    cell = member_row.cell(month)
    if not cell.editable:
        return None, ("This month has several transactions, so one number "
                      "cannot stand for it. Open the month to edit them.")

    text = (text or "").strip().replace(",", "").replace("—", "").replace("–", "")
    if text in ("", "-", "0", "0.0", "0.00"):
        if cell.state == CellState.SINGLE and text in ("", "-"):
            return Change(grid.fund, member_row.member_id, month, ChangeKind.DELETE,
                          row_id=cell.row_id, before=cell.fingerprint()), None
        if cell.state == CellState.EMPTY:
            return None, None                   # nothing there, nothing asked for
        # An explicit zero on an existing row is a deletion too: a fund row of
        # zero is not a fact anybody wants recorded, and posting refuses it.
        return Change(grid.fund, member_row.member_id, month, ChangeKind.DELETE,
                      row_id=cell.row_id, before=cell.fingerprint()), None

    try:
        amount = round(float(text), 2)
    except ValueError:
        return None, f"{text!r} is not a number."
    if amount == 0:
        return None, "Zero is not a movement — clear the cell to remove it."

    if cell.state == CellState.EMPTY:
        return Change(grid.fund, member_row.member_id, month, ChangeKind.INSERT,
                      amount=amount, before=cell.fingerprint()), None

    existing = cell.rows[0]
    was = round(float(existing["amount"] or 0), 2)
    signed = -was if existing["transaction_type"] == WITHDRAWAL else was
    if signed == amount:
        return None, None                       # typed what was already there
    return Change(grid.fund, member_row.member_id, month, ChangeKind.UPDATE,
                  amount=amount, row_id=cell.row_id, before=cell.fingerprint()), None


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def default_day(db, fund, member_id, month):
    """The date a newly inserted cell should carry.

    Follows whatever the member's own history in that fund already does, so a
    grid insert lands where their other rows land — the 1st for the funds whose
    catch-up posts monthly on the 1st, or the enrolment's day for a benevolent
    schedule that runs on the 15th. Falls back to the 1st, which is what every
    catch-up in the application uses.
    """
    day = 1
    last = db.funds.last_dated_row(fund, member_id)
    if last and last.get("date"):
        try:
            day = datetime.strptime(str(last["date"])[:10], _DATE).day
        except ValueError:
            day = 1
    elif spec(fund).scheduled:
        account = db.funds.get_enrolment(member_id)
        if account and account.get("start_date"):
            try:
                day = datetime.strptime(str(account["start_date"])[:10], _DATE).day
            except ValueError:
                day = 1

    first = datetime.strptime(month + "-01", _DATE)
    # Clamp into the month: day 31 in February is the 28th/29th, not March.
    last_day = (first + relativedelta(months=1) - relativedelta(days=1)).day
    return first.replace(day=min(day, last_day)).strftime(_DATE)


# --------------------------------------------------------------------------- #
# Applying a batch
# --------------------------------------------------------------------------- #

@dataclass
class Refused:
    """A change that was not written, and the sentence explaining it."""

    change: Change
    reason: str


@dataclass
class Applied:
    """A change that was written, with what it takes to undo it."""

    change: Change
    row_id: int = None          # the row now holding it
    restore: dict = None        # the row as it was (UPDATE/DELETE), for reversal


@dataclass
class BatchResult:
    """What a save did."""

    fund: str
    applied: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    members_touched: set = field(default_factory=set)

    @property
    def inserted(self):
        return sum(1 for a in self.applied if a.change.kind == ChangeKind.INSERT)

    @property
    def updated(self):
        return sum(1 for a in self.applied if a.change.kind == ChangeKind.UPDATE)

    @property
    def deleted(self):
        return sum(1 for a in self.applied if a.change.kind == ChangeKind.DELETE)

    def summary(self):
        bits = []
        if self.inserted:
            bits.append(f"{self.inserted} added")
        if self.updated:
            bits.append(f"{self.updated} changed")
        if self.deleted:
            bits.append(f"{self.deleted} removed")
        text = ", ".join(bits) or "nothing changed"
        if self.refused:
            text += f" — {len(self.refused)} refused"
        return text


def _refusal_for(db, fund, change, date_str, policy):
    """Why this change may not be written, or None.

    Reuses the existing gates rather than restating them: the posting pipeline's
    validation and the membership policy for money going in, plus the Christmas
    unlock month for money coming out.
    """
    fund_spec = spec(fund)
    if change.kind == ChangeKind.DELETE:
        return None                     # removing a row creates no obligation

    if change.amount < 0:
        if fund_spec.locks_withdrawals:
            month = datetime.strptime(date_str, _DATE).month
            unlock = db.settings.christmas_unlock_month
            if month != unlock:
                name = datetime(2000, unlock, 1).strftime("%B")
                return (f"Christmas withdrawals are only allowed in {name}. "
                        f"Use the member's Christmas tab to override.")
        # A member who has left may still take their money out.
        return None

    _tidy, problems = check_posting(
        db, Posting(table=fund, member_id=change.member_id, date=date_str,
                    kind=change.transaction_type, amount=change.magnitude),
        policy=policy)
    return " ".join(problems) if problems else None


def _current_fingerprint(db, fund, member_id, month):
    """The cell as the database holds it *now*, for the staleness check."""
    start = month + "-01"
    end = (datetime.strptime(start, _DATE) + relativedelta(months=1)
           - relativedelta(days=1)).strftime(_DATE)
    return tuple(sorted((r[0], round(float(r[1] or 0), 2), str(r[2])[:10], r[3])
                        for r in db.funds.month_rows(fund, member_id, start, end)))


def apply_changes(db, fund, changes, policy=default_policy):
    """Write a batch of staged cell edits.

    One database transaction for the whole batch — so a failure leaves the funds
    exactly as they were — but per-change refusals, so one ineligible member
    does not cost the operator the other ninety edits.

    Running balances are re-derived once per member at the end rather than once
    per row: the stored ``balance`` is a property of a member's whole ordered
    series, so recalculating it forty times while writing forty rows produces
    the same answer forty times more slowly.
    """
    spec(fund)                                  # rejects an unknown table
    result = BatchResult(fund=fund)
    if not changes:
        return result

    with db.transaction():
        for change in changes:
            date_str = default_day(db, fund, change.member_id, change.month)

            # Someone editing the same member on another screen while this grid
            # sat open. Refusing is the only honest answer: the staged number
            # was typed against figures that have since moved.
            if change.before != _current_fingerprint(db, fund, change.member_id,
                                                     change.month):
                result.refused.append(Refused(
                    change, "This month changed elsewhere since the grid was "
                            "loaded. Reload to see the current figures."))
                continue

            reason = _refusal_for(db, fund, change, date_str, policy)
            if reason:
                result.refused.append(Refused(change, reason))
                continue

            try:
                applied = _write(db, fund, change, date_str)
            except Exception as e:              # noqa: BLE001 - report per cell
                logger.exception("Fund grid change failed for member %s %s",
                                 change.member_id, change.month)
                result.refused.append(Refused(change, str(e)))
                continue

            result.applied.append(applied)
            result.members_touched.add(change.member_id)

        for member_id in result.members_touched:
            db.funds.recalculate(fund, member_id)
        if spec(fund).scheduled and result.members_touched:
            for member_id in result.members_touched:
                resync_benevolent_schedule(db, member_id)

    return result


def _write(db, fund, change, date_str):
    """Perform one change inside the batch's transaction."""
    fund_spec = spec(fund)
    if change.kind == ChangeKind.INSERT:
        row_id = db.funds.add(fund, change.member_id, date_str,
                              change.transaction_type, change.magnitude, 0.0,
                              change.notes or f"{fund_spec.label} (grid entry)")
        return Applied(change, row_id=row_id)

    before = db.funds.get(fund, change.row_id)
    if before is None:
        raise ValueError("That entry no longer exists.")
    restore = dict(before)

    if change.kind == ChangeKind.DELETE:
        db.funds.delete(fund, change.row_id)
        return Applied(change, row_id=None, restore=restore)

    # An UPDATE may flip a deposit into a withdrawal, which the generic
    # fund update does not cover — it only moves date/amount/notes.
    db.funds.update(fund, change.row_id, str(before["date"])[:10],
                    change.magnitude, before.get("notes") or "")
    if before["transaction_type"] != change.transaction_type:
        db.funds.set_type(fund, change.row_id, change.transaction_type)
    return Applied(change, row_id=change.row_id, restore=restore)


def revert(db, fund, result):
    """Put a batch back exactly as it was.

    The grid's own undo. It does not touch :class:`~src.services.undo_manager`
    — that stack belongs to the loan and mass-operation screens, and a fund
    grid save has no business appearing in it.
    """
    spec(fund)
    touched = set()
    with db.transaction():
        for applied in reversed(result.applied):
            change = applied.change
            touched.add(change.member_id)
            if change.kind == ChangeKind.INSERT:
                if applied.row_id is not None:
                    db.funds.delete(fund, applied.row_id)
            elif change.kind == ChangeKind.DELETE:
                row = applied.restore or {}
                db.funds.add(fund, change.member_id, str(row.get("date"))[:10],
                             row.get("transaction_type"), row.get("amount"), 0.0,
                             row.get("notes") or "")
            else:
                row = applied.restore or {}
                db.funds.update(fund, change.row_id, str(row.get("date"))[:10],
                                row.get("amount"), row.get("notes") or "")
                db.funds.set_type(fund, change.row_id, row.get("transaction_type"))
        for member_id in touched:
            db.funds.recalculate(fund, member_id)
        if spec(fund).scheduled:
            for member_id in touched:
                resync_benevolent_schedule(db, member_id)
    return touched


def resync_benevolent_schedule(db, member_id):
    """Point ``next_due_date`` at the month after the last contribution.

    Without this, a contribution written straight into the ledger leaves the
    schedule where it was, and the next catch-up re-posts every month the grid
    just filled. Falls back to the enrolment's start date when every
    contribution has been removed, which is what ``delete_all`` does.
    """
    account = db.funds.get_enrolment(member_id)
    if not account:
        return None
    last = db.funds.last_dated_row(BENEVOLENT, member_id, kinds=("Contribution",))
    if last and last.get("date"):
        try:
            due = (datetime.strptime(str(last["date"])[:10], _DATE)
                   + relativedelta(months=1)).strftime(_DATE)
        except ValueError:
            return None
    else:
        due = account.get("start_date")
    if due:
        db.funds.set_next_due(member_id, due)
    return due


# --------------------------------------------------------------------------- #
# Fill forward — the grid's answer to catch-up
# --------------------------------------------------------------------------- #

def suggested_amount(db, fund, member_row):
    """What this member's monthly contribution to this fund appears to be.

    The same figure the fund's own catch-up would use, read from the same
    places: the enrolled amount for benevolent, otherwise the member's most
    common credit, otherwise the configured default. Returns 0 when there is
    nothing to go on — a fill must not invent a number.
    """
    fund_spec = spec(fund)
    if fund_spec.scheduled:
        return round(member_row.monthly_amount or 0, 2)

    amounts = [round(float(r["amount"] or 0), 2)
               for cell in member_row.cells.values() for r in cell.rows
               if r["transaction_type"] == fund_spec.credit]
    if not amounts:
        row = db.funds.last_dated_row(fund, member_row.member_id,
                                      kinds=(fund_spec.credit,))
        if row:
            return round(float(row["amount"] or 0), 2)
        return round(float(
            db.settings.default_savings_increment if fund == SAVINGS
            else db.settings.default_christmas_increment) or 0, 2)
    return max(set(amounts), key=amounts.count)


@dataclass
class FillPlan:
    """What a fill proposes, and what it deliberately left alone."""

    changes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)     # [(member_row, reason)]
    before_start: int = 0       # cells skipped: the account did not exist yet
    in_future: int = 0          # cells skipped: the month has not happened

    @property
    def members(self):
        return len({c.member_id for c in self.changes})

    def notes(self):
        """Sentences for the parts a count alone would not explain."""
        out = []
        if self.before_start:
            out.append(f"{self.before_start} month(s) before the member's account "
                       f"began were left empty.")
        if self.in_future:
            out.append(f"{self.in_future} month(s) in the future were left empty.")
        return out


def plan_fill(db, grid, member_rows, months, policy=default_policy, today=None):
    """Propose a credit for every **fillable** empty cell in a selection.

    The grid's equivalent of Catch Up, and deliberately a *proposal*: it stages
    changes for review rather than writing, which is the difference between a
    spreadsheet and a batch job.

    "Fillable" is narrower than "empty", and the difference is the whole point.
    An empty cell means one of three quite different things, and only one of
    them is a missed contribution:

    * **before the account began** — a member enrolled in April has no March
      contribution to miss. Filling it would invent money that could not have
      been collected, and on a scheduled fund it would then drag the benevolent
      due date backwards. Bounded by :attr:`MemberRow.account_start`.
    * **after this month** — September has not happened. Filling it records a
      contribution nobody has paid, inflating every balance and report that
      reads the fund. Bounded by ``today``.
    * **a genuine gap** — the account existed, the month has passed, and nothing
      was recorded. This is the only case a fill is for.

    Occupied cells are never touched: filling is for gaps, and replacing a
    recorded figure with an assumed one is not on offer.

    A member with no account start at all is skipped rather than guessed at —
    the same answer ``catch_up_savings`` gives when there is no history to
    extrapolate from.
    """
    today = today or datetime.now().strftime(_DATE)
    this_month = month_key(today)

    plan = FillPlan()
    for member_row in member_rows:
        if not member_row.active:
            plan.skipped.append((member_row, f"{member_row.name} has left the SACCO."))
            continue
        if member_row.excluded:
            plan.skipped.append((
                member_row, f"{member_row.name} is excluded from this ledger."))
            continue
        if grid.spec.scheduled and not member_row.enrolled:
            plan.skipped.append((member_row, f"{member_row.name} is not enrolled."))
            continue
        if not member_row.account_start:
            plan.skipped.append((
                member_row,
                f"Nothing on record says when {member_row.name}'s "
                f"{grid.spec.label} account began — record their first "
                f"{grid.spec.noun}, then fill."))
            continue
        amount = suggested_amount(db, grid.fund, member_row)
        if amount <= 0:
            plan.skipped.append((
                member_row, f"No {grid.spec.noun} history to infer an amount from."))
            continue

        fillable = []
        for month in months:
            if member_row.cell(month).state != CellState.EMPTY:
                continue
            if month < member_row.account_start:
                plan.before_start += 1
            elif month > this_month:
                plan.in_future += 1
            else:
                fillable.append(month)

        if not fillable:
            plan.skipped.append((
                member_row, f"No missed months in range for {member_row.name}."))
            continue
        for month in fillable:
            plan.changes.append(Change(grid.fund, member_row.member_id, month,
                                       ChangeKind.INSERT, amount=amount,
                                       before=member_row.cell(month).fingerprint()))
    return plan


class FundBackend:
    """One fund, behind the interface the grid screen drives.

    The screen — staging, clipboard, totals, save, undo — is the same work
    whichever ledger is underneath. What differs is what a row is, what a cell
    will accept and how a change is written, and that is all this exposes. The
    loan grid supplies the same surface (:class:`~src.services.loan_grid.LoanBackend`),
    so there is one screen rather than two that drift apart.
    """

    #: The read-only columns either side of the months.
    lead_columns = [("Member", "label"), ("PF No", "pf_no"), ("Opening", "opening")]
    tail_columns = [("Period", "period"), ("Balance", "balance")]
    per_row_noun = "member"

    def __init__(self, fund):
        self.fund = fund
        self.spec = spec(fund)
        self.label = self.spec.label
        self.noun = self.spec.noun

    def load(self, db, engine, months, include_inactive=True, include_excluded=False):
        return load(db, self.fund, months, include_inactive=include_inactive,
                    include_excluded=include_excluded)

    def empty(self):
        return Grid(fund=self.fund, months=[], members=[])

    def cell_refusal(self, grid, row, month):
        """Why this cell cannot be typed into, or None."""
        cell = row.cell(month)
        if cell.editable:
            return None
        return (f"{len(cell.rows)} transactions this month, netting "
                f"{cell.net:,.0f}. One number cannot stand for them — "
                f"double-click to open {row.name}'s ledger.")

    def stage(self, grid, row, month, text):
        return stage(grid, row, month, text)

    def apply(self, db, engine, changes):
        return apply_changes(db, self.fund, changes)

    def revert(self, db, engine, result):
        return revert(db, self.fund, result)

    def plan_fill(self, db, grid, rows, months):
        return plan_fill(db, grid, rows, months)

    def blocked(self, db, rows):
        return blocked_members(db, self.fund, [r.member_id for r in rows])

    def lead_values(self, row):
        return [row.label, row.pf_no, row.opening_balance]

    def tail_values(self, row, months):
        return [row.period_total(months), row.total_balance]

    def row_note(self, row, blocked):
        """The tooltip explaining a greyed row, or None."""
        if row.excluded:
            return (f"{row.name} is excluded from this ledger."
                    + (f" {row.exclusion_note}" if row.exclusion_note else ""))
        if not row.active:
            return blocked.get(row.key, row.status)
        if self.spec.scheduled and not row.enrolled:
            return "Not enrolled in the benevolent fund."
        return None


def blocked_members(db, fund, member_ids, policy=default_policy):
    """Members who may not receive new credits, as ``{id: reason}``.

    Lets the view grey those rows out *before* anything is typed, rather than
    letting somebody fill a row and then explaining at save time.
    """
    kind = spec(fund).policy_kind
    out = {}
    for member_id in member_ids:
        refusals = policy.check(db, Operation(kind, member_id=member_id))
        if refusals:
            out[member_id] = refusals[0].reason
    return out
