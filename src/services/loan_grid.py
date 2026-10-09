"""A loans × months grid over the deduction schedule.

The sibling of :mod:`src.services.fund_grid`, and deliberately a narrower thing.
A fund subledger is a list of independent movements, so a cell can be any number
you like. A loan is not: each deduction accrues that month's interest, splits
itself between interest and principal against the balance *at that moment*, and
moves the due date on. The rows in the ledger are the consequence of a schedule,
not entries in a list.

WHY THIS DOES NOT SIMPLY WRITE ROWS
-----------------------------------
Measured on a 100,000 loan at 5,750 a month, three months in:

* writing a bare ``Repayment`` row and re-projecting gives principal 79,250.
  The engine gives 80,000. Nothing errors — the month simply accrued no
  interest, so the whole 5,750 hit principal and the member was silently
  over-credited by 750;
* editing an amount without :func:`~src.services.loan_projection.clear_derived`
  leaves the old split in place, and the new figure changes nothing at all;
* deleting a deduction from the middle of a loan's history leaves stored rows
  the derivation cannot match, so :func:`project_loan` declines to project and
  the loan keeps a cached balance its own ledger no longer supports.

So this module never fabricates loan postings. Every deduction it creates goes
through :meth:`LoanService.deduct_single_loan` — the same call the Deduct button
makes — and every edit clears the derived split and re-projects. What the grid
adds is the *table*: hundreds of loans and a dozen months on one screen, staged
and reviewable, instead of a dialog per member.

WHAT A CELL WILL AND WILL NOT ACCEPT
------------------------------------
* **the loan's next due month, empty** — takes a deduction. Type a figure to
  deduct something other than the instalment.
* **a month with exactly one deduction** — the figure can be corrected, or
  cleared if it is the loan's most recent (clearing an earlier one would strand
  the loan, per above).
* **a month before the next due date that is empty** — refused. It is a hole in
  a sequence, and filling it means placing an accrual whose size depends on the
  balance at that date; that is the derivation's job and not a cell's.
* **a month after the next due date, with earlier months unfilled** — refused,
  because a loan is paid in order. The cell says which month is owed first, and
  Fill Forward will work through them.
* **a suspended month** — refused, with the suspension named.

OPERATIONS
----------
The due month will also take one operation instead of a deduction — the same
things the member's ledger offers as buttons, run through the same calls
(:mod:`src.services.grid_ops` has the syntax):

* ``@T=20,000/12`` — a top-up of 20,000, re-termed over 12 months
  (:meth:`LoanService.top_up_loan`, as the Top-Up button);
* ``@B`` — buy the loan off: settle principal and interest in full
  (:meth:`LoanService.buyoff_loan`, as the Buyoff button);
* ``@S=3`` — suspend deductions for three months from this one
  (:meth:`DatabaseManager.record_suspension`, as the Suspend dialog).

Only in the due month, and only one per month, for the reason everything else
here is constrained: each of these moves the loan's schedule, and a second
thing staged against the same month would be relying on a schedule that no
longer exists by the time it is written.

DELETING
--------
docs/grid/deleting-months.md §4 is the specification. In short, removing a
deduction is one of two things, decided by what follows it on the loan:

* **un-run** — it is the loan's last entry: the deduction and the month's
  interest charge go, the interest returns to unearned, and the month falls due
  again (the exact inverse of Deduct);
* **missed** — later deductions exist: only the deduction goes, the interest
  stays charged, and every later deduction is re-split interest first.

A buy-off after it, or a top-up between it and the next deduction, refuses it.
A loan's deletions are judged newest first and all-or-nothing.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

from dateutil.relativedelta import relativedelta

from collections import defaultdict

from src.services.fund_grid import (Cell, CellState, ChangeKind, DeletePlan,
                                    month_bounds, month_key, month_label,
                                    month_range)
from src.services.grid_ops import MONTHS, Entry, Op, OpError, OpSet, figure
from src.services.policy import Operation, default_policy

logger = logging.getLogger(__name__)

_DATE = "%Y-%m-%d"

#: Ledger events that count as money paid against a loan in a month.
PAYMENTS = ("Repayment", "Loan Buyoff")

#: A deduction the grid will take by running the engine forward one month.
DEDUCT = "deduct"
#: Operations other than a deduction — see OPERATIONS in the module docstring.
TOP_UP = "top_up"
BUYOFF = "buyoff"
SUSPEND = "suspend"
#: A deduction recorded in a missed month (docs §4.6).
LATE = "late"

#: The two kinds of deletion (docs §4.2).
UNRUN = "unrun"
MISSED = "missed"

#: Loan events that are not deductions, and that a deletion never removes.
_STRUCTURAL = ("Loan Issued", "Loan Top-Up", "Loan Restructure")

_REPAY = Op("R", "Repayment", ("r", "repayment", "repay", "deduction", "deduct", "d"),
            "The month's deduction — the default, so a bare figure means this. "
            "Type what was actually taken if it was not the instalment.")
_TOP_UP = Op("T", "Top-up", ("t", "topup"),
             "Lend more on this loan, re-termed over the months after the slash: "
             "@T=20,000/12.", arg=MONTHS)
_BUYOFF = Op("B", "Buy-off", ("b", "buyoff", "settle"),
             "Settle the loan in full this month — principal plus all interest. "
             "Takes no figure; the amount owed is worked out.", value=None)
_SUSPEND = Op("S", "Suspend", ("s", "suspend", "pause"),
              "Pause deductions for this many months, starting this month: @S=3. "
              "The term extends by the same.", value=MONTHS)

#: What a loan cell understands. No negative figure: a loan is not paid back
#: by a withdrawal.
OPS = OpSet((_REPAY, _TOP_UP, _BUYOFF, _SUSPEND), _REPAY)
_KIND_OF = {_TOP_UP: TOP_UP, _BUYOFF: BUYOFF, _SUSPEND: SUSPEND}
_OP_OF = {kind: op for op, kind in _KIND_OF.items()}

LABEL = "Loans"
NOUN = "deduction"


@dataclass
class LoanRow:
    """One active loan — the unit of a row.

    Keyed by the loan, not the member: somebody holding two loans is two rows,
    because two loans are two schedules and a single figure for the month would
    have to be split between them by guesswork.
    """

    member_id: int
    loan_id: int
    ref: str
    name: str
    pf_no: str = ""
    installment: float = 0.0
    balance: float = 0.0
    next_due: str = None
    status: str = "Active"
    is_suspended: bool = False
    suspend_until: str = None
    member_active: bool = True
    excluded: bool = False
    exclusion_note: str = ""
    issued_month: str = None
    #: The loan's most recent Repayment across its whole history, not just the
    #: visible window.
    latest_payment_id: int = None
    cells: dict = field(default_factory=dict)
    #: Every non-interest row on the loan, whole history: ``(date, id, kind)``.
    #: What a deletion is judged against — what comes after it decides it.
    timeline: list = field(default_factory=list)
    #: Interest charged in the visible months: month -> {id, date, amount}.
    accruals: dict = field(default_factory=dict)

    @property
    def key(self):
        """What staged edits are keyed by. The loan, not the member."""
        return self.loan_id

    @property
    def label(self):
        return f"{self.name} · {self.ref}"

    @property
    def next_due_month(self):
        return month_key(self.next_due) if self.next_due else None

    #: next_due_date as stored, before stepping over suspensions — where Fill
    #: Forward starts counting, so the months it skips are reported.
    stored_due: str = None

    @property
    def due_day(self):
        """The day of the month this loan falls due on."""
        try:
            return int(str(self.next_due)[8:10])
        except (TypeError, ValueError):
            return 1

    def cell(self, month):
        return self.cells.get(month) or Cell()

    def period_total(self, months):
        return round(sum(self.cell(m).net for m in months), 2)

    def missed(self, month):
        """Interest charged this month, nothing paid, before the due date.

        A missed deduction (docs §4.2) — what deleting a month the loan has
        since moved past leaves behind, and the one kind of earlier month a
        figure can still be typed into (§4.6).
        """
        return (month in self.accruals and not self.cell(month).rows
                and bool(self.next_due_month) and month < self.next_due_month)


@dataclass
class LoanGrid:
    """One period's worth of loan deductions."""

    months: list
    rows: list = field(default_factory=list)
    suspensions: dict = field(default_factory=dict)   # loan_id -> [(start, end)]

    #: Named so the view can treat this and a fund grid the same way.
    fund = "loans"

    @property
    def members(self):
        """The grid's rows. Named for symmetry with the fund grid's view."""
        return self.rows

    def grand_total(self):
        return round(sum(r.period_total(self.months) for r in self.rows), 2)


def _suspension_windows(db, loan_id):
    windows = []
    for span in db.get_loan_suspensions_for_loan(loan_id):
        start = span.get("start_date")
        if not start:
            continue
        end = span.get("resumed_date") or span.get("suspend_until")
        windows.append((str(start)[:10], str(end)[:10] if end else None))
    return windows


def _due_in(month, day):
    """The loan's due date within ``month``, clamped to the month's length."""
    first = datetime.strptime(month + "-01", _DATE)
    last = (first + relativedelta(months=1) - relativedelta(days=1)).day
    return first.replace(day=min(day, last)).strftime(_DATE)


def _window_for(month, windows, day=1):
    """The suspension covering the loan's due date in ``month``, or None.

    The due *date* decides, not the month: a loan due on the 28th suspended
    from 28 Aug to 28 Oct skips August and September and pays in October. That
    is the half-open ``[start, end)`` test :meth:`LoanService._date_in_suspension`
    applies when it deducts. Testing the 1st instead got both ends wrong —
    August looked payable and October looked suspended.
    """
    due = _due_in(month, day)
    return next(((start, end) for start, end in windows
                 if due >= start and (end is None or due < end)), None)


def _in_suspension(month, windows, day=1):
    return _window_for(month, windows, day) is not None


def _skip_suspended(due, windows):
    """Step a due date past fixed-term suspensions, as a deduction would.

    The stored ``next_due_date`` is not moved when a suspension is recorded —
    deduct_single_loan steps over the window when it next deducts. Showing the
    stored date would call the loan due for a month it will never be charged.
    An open-ended suspension is left where it is: the loan is on hold, not
    due later.
    """
    if not due or not windows:
        return due
    cursor = datetime.strptime(str(due)[:10], _DATE)
    for _ in range(600):
        date = cursor.strftime(_DATE)
        window = next(((s, e) for s, e in windows if date >= s and (e is None or date < e)),
                      None)
        if window is None:
            return date
        if window[1] is None:
            return str(due)[:10]
        cursor += relativedelta(months=1)
    return str(due)[:10]


def load(db, months, include_inactive=True, include_paid=False,
         include_excluded=False):
    """Read the loan grid for a list of month keys.

    One query for the loans, one for the payments in range, and one suspension
    read per loan — the last only for loans that have any, which on the
    production journal is a handful.
    """
    start, end = month_bounds(months)
    if start is None:
        return LoanGrid(months=[])

    payments = {}
    for row in db.conn.execute(
            "SELECT id, individual_id, loan_id AS ref, date, event_type, deducted, notes "
            "FROM ledger WHERE date >= ? AND date <= ? AND event_type IN (?, ?) "
            "ORDER BY individual_id, date, id", (start, end, *PAYMENTS)):
        payments.setdefault((row[1], row[2]), {}).setdefault(month_key(row[3]), []).append({
            "id": row[0], "date": row[3], "transaction_type": row[4],
            "amount": round(float(row[5] or 0), 2), "notes": row[6]})

    issued = {(r[0], r[1]): month_key(r[2]) for r in db.conn.execute(
        "SELECT individual_id, loan_id, MIN(date) FROM ledger "
        "WHERE event_type='Loan Issued' GROUP BY individual_id, loan_id")}

    # Every loan's non-interest history, whole: a deletion is judged by what
    # follows it, which can lie far outside the visible window.
    timelines = defaultdict(list)
    latest = {}
    for member_id, ref, date, row_id, kind in db.conn.execute(
            "SELECT individual_id, loan_id, date, id, event_type FROM ledger "
            "WHERE event_type != 'Interest Earned' ORDER BY individual_id, loan_id, date, id"):
        timelines[(member_id, ref)].append((str(date)[:10], row_id, kind))
        if kind == "Repayment":
            latest[(member_id, ref)] = row_id

    accruals = defaultdict(dict)
    for row_id, member_id, ref, date, added in db.conn.execute(
            "SELECT id, individual_id, loan_id, date, added FROM ledger "
            "WHERE event_type='Interest Earned' AND date >= ? AND date <= ? ORDER BY id",
            (start, end)):
        accruals[(member_id, ref)][month_key(date)] = {
            "id": row_id, "date": str(date)[:10], "amount": round(float(added or 0), 2)}

    statuses = ("Active",) if not include_paid else ("Active", "Paid")
    sql = ("SELECT l.id, l.individual_id, l.ref, l.installment, l.balance, "
           "       l.next_due_date, l.status, l.is_suspended, l.suspend_until, "
           "       i.name, i.pf_no, i.employment_status, i.is_retired "
           "FROM loans l JOIN individuals i ON i.id = l.individual_id "
           f"WHERE l.status IN ({','.join('?' * len(statuses))}) "
           "ORDER BY i.name COLLATE NOCASE, l.ref")

    excluded = db.members.excluded_from("loans")

    grid = LoanGrid(months=months)
    for row in db.conn.execute(sql, statuses):
        (loan_id, member_id, ref, installment, balance, next_due, status,
         suspended, suspend_until, name, pf_no, employment, retired) = row
        member_active = not retired and (employment or "Active") == "Active"
        if not include_inactive and not member_active:
            continue
        if member_id in excluded and not include_excluded:
            continue

        cells = {}
        for month, rows in payments.get((member_id, ref), {}).items():
            cell = Cell(rows=rows)
            cell.net = round(sum(r["amount"] for r in rows), 2)
            cells[month] = cell

        windows = (_suspension_windows(db, loan_id)
                   if suspended or db.get_loan_suspensions_for_loan(loan_id) else [])
        if windows:
            grid.suspensions[loan_id] = windows

        grid.rows.append(LoanRow(
            member_id=member_id, loan_id=loan_id, ref=ref, name=name or "",
            pf_no=pf_no or "", installment=round(float(installment or 0), 2),
            balance=round(float(balance or 0), 2),
            next_due=_skip_suspended(next_due, windows), stored_due=next_due,
            status=status, is_suspended=bool(suspended), suspend_until=suspend_until,
            member_active=member_active, excluded=member_id in excluded,
            exclusion_note=excluded.get(member_id, ""),
            issued_month=issued.get((member_id, ref)),
            latest_payment_id=latest.get((member_id, ref)), cells=cells,
            timeline=timelines.get((member_id, ref), []),
            accruals=dict(accruals.get((member_id, ref), {}))))
    return grid


# --------------------------------------------------------------------------- #
# What deleting a deduction means — docs/grid/deleting-months.md §4.2–4.4
# --------------------------------------------------------------------------- #

def _timeline(db, member_id, ref):
    """A loan's non-interest rows as ``(date, id, kind)``, oldest first."""
    return [(str(d)[:10], i, k) for d, i, k in db.conn.execute(
        "SELECT date, id, event_type FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type != 'Interest Earned' ORDER BY date, id", (member_id, ref))]


def _after(timeline, date, row_id, gone=()):
    key = (str(date)[:10], row_id)
    return [(d, i, k) for d, i, k in timeline if (d, i) > key and i not in gone]


def _buyoff_after(timeline, date, row_id):
    """The date of a buy-off after this row, or None."""
    return next((d for d, _i, k in _after(timeline, date, row_id) if k == "Loan Buyoff"),
                None)


def _behind_buyoff(when):
    return (f"The loan was bought off on {when}, for a figure worked out from a history "
            f"that includes this month. Reverse the buy-off in the member's ledger "
            f"first (Delete Last restores the loan).")


def deletion_kind(timeline, row_id, date, gone=()):
    """What removing one deduction does: ``(UNRUN | MISSED, None)`` or
    ``(None, reason)``.

    ``gone`` holds deductions removed before this one in the same batch —
    newer ones, since a loan's deletions run newest first (§4.4).
    """
    later = _after(timeline, date, row_id, gone)
    buyoff = next((d for d, _i, k in later if k == "Loan Buyoff"), None)
    if buyoff:
        return None, _behind_buyoff(buyoff)
    if any(k == "Repayment" for _d, _i, k in later):
        return MISSED, None
    structural = next(((d, k) for d, _i, k in later if k in _STRUCTURAL), None)
    if structural:
        when, kind = structural
        what = kind.replace("Loan ", "").lower()
        return None, (f"A {what} on {when} follows this, the loan's last deduction, and "
                      f"was worked out from the balance it left. Remove the {what} in "
                      f"the member's ledger first, or keep this month.")
    return UNRUN, None


def judge_deletions(timeline, payments):
    """Judge one loan's deletions together, newest first, all or nothing.

    ``payments`` are ``(row_id, date, kind)``. Returns ``(modes, None)`` with a
    mode per payment in the order given, or ``(None, reason)``.
    """
    ordered = sorted(payments, key=lambda p: (str(p[1])[:10], p[0]), reverse=True)
    modes, gone = {}, set()
    for row_id, date, kind in ordered:
        if kind == "Loan Buyoff":
            return None, ("A buy-off settles the loan; reverse it in the member's "
                          "ledger rather than deleting it here.")
        mode, why = deletion_kind(timeline, row_id, date, gone)
        if why:
            return None, why
        modes[row_id] = mode
        gone.add(row_id)
    return [modes[row_id] for row_id, _d, _k in payments], None


# --------------------------------------------------------------------------- #
# What a cell will accept
# --------------------------------------------------------------------------- #

@dataclass
class Change:
    """One staged loan-cell edit."""

    loan_id: int
    member_id: int
    ref: str
    month: str
    kind: str                   # ChangeKind.UPDATE / DELETE, or DEDUCT
    amount: float = 0.0
    row_id: int = None
    before: tuple = ()
    months: int = None          # TOP_UP: the new term; SUSPEND: how long
    mode: str = None            # DELETE: UNRUN or MISSED, as judged when staged
    date: str = None            # LATE: the date the missed month was charged on

    @property
    def effect(self):
        """What this adds to the month's payments once saved — staged totals.

        Only a deduction is a payment. A top-up lends money, a suspension moves
        none, and a buy-off's figure is not known until it is worked out.
        """
        return self.amount if self.kind in (DEDUCT, ChangeKind.UPDATE, LATE) else 0.0

    @property
    def entry(self):
        """The change as a cell entry, for display."""
        op = _OP_OF.get(self.kind, _REPAY)
        if self.kind == SUSPEND:
            return Entry(op, self.months or 0)
        return Entry(op, self.amount, self.months)

    @property
    def key(self):
        """The row this change belongs to — see :attr:`LoanRow.key`.

        The loan, not the member: a member with two loans has two rows, and
        keying staged edits by member would collapse them onto each other.
        """
        return self.loan_id

    @property
    def magnitude(self):
        return round(abs(self.amount), 2)


def cell_refusal(grid, row, month):
    """Why this cell cannot be typed into, or None.

    Every branch names the month that *can* be worked on, because "no" without
    "here instead" is what makes a locked cell feel arbitrary.
    """
    cell = row.cell(month)
    if cell.state == CellState.MIXED:
        return ("This month holds more than one payment, so one figure cannot "
                "stand for it. Open the member's ledger to work on them.")
    if cell.state == CellState.SINGLE:
        if cell.rows[0]["transaction_type"] == "Loan Buyoff":
            return "This is a buy-off, which settles the loan. Edit it in the ledger."
        return None

    # Empty from here on.
    if row.status != "Active":
        return f"{row.ref} is {row.status.lower()}; it takes no more deductions."
    windows = grid.suspensions.get(row.loan_id) or []
    window = _window_for(month, windows, row.due_day)
    if window:
        if window[1] is None:
            return (f"{row.ref} is suspended until further notice. Resume it in "
                    f"the member's ledger before deducting.")
        return (f"{row.ref} is suspended for this month; deductions resume from "
                f"{window[1]}.")
    if row.issued_month and month < row.issued_month:
        return f"{row.ref} was not issued until {month_label(row.issued_month)}."
    if not row.next_due_month:
        return f"{row.ref} has no due date recorded."
    if month < row.next_due_month:
        if row.missed(month):
            return None         # a missed deduction can be recorded (§4.6)
        return ("This month was passed over and cannot be filled in here — the "
                "interest for it depends on the balance at the time. Use the "
                "member's ledger.")
    if month > row.next_due_month:
        return (f"{row.ref} is due for {month_label(row.next_due_month)} first. "
                f"A loan is paid in order — fill that month, or use Fill Forward.")
    return None


def stage(grid, row, month, text):
    """Turn typed text into a :class:`Change`, or return a refusal.

    Returns ``(change, problem)``, exactly one of which is None.
    """
    refusal = cell_refusal(grid, row, month)
    if refusal:
        return None, refusal

    cell = row.cell(month)
    try:
        entries, _append = OPS.parse(text)
    except OpError as e:
        return None, str(e)
    # A zero figure is no figure: "0" is refused below, "@S=0" asks for nothing.
    entries = [e for e in entries if e.amount or e.op.value is None]

    if len(entries) > 1:
        return None, ("A loan month takes one thing — a deduction, or one "
                      "operation. Save one, then type the next.")

    if not entries:
        if (text or "").strip() not in ("", "-", "—", "–"):
            return None, ("A deduction must be more than zero — clear the cell "
                          "to remove it.")
        if cell.state == CellState.EMPTY:
            return None, None
        mode, why = deletion_kind(row.timeline, cell.row_id, cell.rows[0]["date"])
        if why:
            return None, why
        return Change(row.loan_id, row.member_id, row.ref, month, ChangeKind.DELETE,
                      row_id=cell.row_id, before=cell.fingerprint(), mode=mode), None

    entry = entries[0]
    if row.missed(month):
        if entry.op is not _REPAY:
            due = month_label(row.next_due_month)
            return None, (f"This month was missed; it can take its deduction, but "
                          f"@{entry.op.code} goes in the month {row.ref} is due next — "
                          f"{due}.")
        accrual = row.accruals[month]
        buyoff = _buyoff_after(row.timeline, accrual["date"], accrual["id"])
        if buyoff:
            return None, _behind_buyoff(buyoff)
        return Change(row.loan_id, row.member_id, row.ref, month, LATE,
                      amount=entry.amount, before=cell.fingerprint(),
                      date=accrual["date"]), None
    if entry.op is not _REPAY:
        if cell.state != CellState.EMPTY:
            due = month_label(row.next_due_month) if row.next_due_month else "its due month"
            return None, (f"This month already has its deduction. @{entry.op.code} "
                          f"({entry.op.name.lower()}) goes in the month {row.ref} is "
                          f"due next — {due}.")
        kind = _KIND_OF[entry.op]
        if kind == SUSPEND:
            if entry.amount > 60:
                return None, "A suspension can run for at most 60 months here."
            return Change(row.loan_id, row.member_id, row.ref, month, kind,
                          months=int(entry.amount), before=cell.fingerprint()), None
        return Change(row.loan_id, row.member_id, row.ref, month, kind,
                      amount=entry.amount, months=entry.arg,
                      before=cell.fingerprint()), None

    amount = entry.amount
    if cell.state == CellState.EMPTY:
        return Change(row.loan_id, row.member_id, row.ref, month, DEDUCT,
                      amount=amount, before=cell.fingerprint()), None

    if round(cell.rows[0]["amount"], 2) == amount:
        return None, None
    buyoff = _buyoff_after(row.timeline, cell.rows[0]["date"], cell.row_id)
    if buyoff:
        return None, _behind_buyoff(buyoff)
    return Change(row.loan_id, row.member_id, row.ref, month, ChangeKind.UPDATE,
                  amount=amount, row_id=cell.row_id, before=cell.fingerprint()), None


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #

@dataclass
class Refused:
    change: Change
    reason: str


@dataclass
class Applied:
    change: Change
    row_id: int = None
    restore: dict = None


@dataclass
class BatchResult:
    fund: str = "loans"
    applied: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    members_touched: set = field(default_factory=set)

    @property
    def deducted(self):
        return sum(1 for a in self.applied if a.change.kind == DEDUCT)

    @property
    def updated(self):
        return sum(1 for a in self.applied if a.change.kind == ChangeKind.UPDATE)

    @property
    def deleted(self):
        return sum(1 for a in self.applied if a.change.kind == ChangeKind.DELETE)

    def _count(self, kind):
        return sum(1 for a in self.applied if a.change.kind == kind)

    def summary(self):
        bits = []
        if self.deducted:
            bits.append(f"{self.deducted} deducted")
        for kind, verb in ((TOP_UP, "topped up"), (BUYOFF, "bought off"),
                           (SUSPEND, "suspended")):
            if self._count(kind):
                bits.append(f"{self._count(kind)} {verb}")
        if self._count(LATE):
            bits.append(f"{self._count(LATE)} missed month(s) recorded")
        if self.updated:
            bits.append(f"{self.updated} corrected")
        modes = [a.change.mode for a in self.applied if a.change.kind == ChangeKind.DELETE]
        if modes.count(UNRUN):
            bits.append(f"{modes.count(UNRUN)} un-run")
        if modes.count(MISSED):
            bits.append(f"{modes.count(MISSED)} marked missed")
        text = ", ".join(bits) or "nothing changed"
        if self.refused:
            text += f" — {len(self.refused)} refused"
        return text


def apply_changes(db, engine, changes, policy=default_policy):
    """Write a batch of staged loan-cell edits.

    Ordered by (loan, month) before anything is written, because a loan is paid
    in sequence: two deductions staged for the same loan must be taken in the
    order the months fall, and each one moves the due date the next one relies
    on.

    One transaction for the batch, per-change refusals inside it, and one
    re-derivation per member at the end.
    """
    result = BatchResult()
    if not changes:
        return result

    ordered = sorted(changes, key=_batch_order)
    with db.transaction():
        modes, refused_loans = _judge_batch_deletions(
            db, [c for c in ordered if c.kind == ChangeKind.DELETE])
        for change in ordered:
            if change.kind == ChangeKind.DELETE:
                reason = refused_loans.get(change.loan_id)
                if reason is None:
                    change.mode = modes[id(change)]
            else:
                reason = (_refusal_for(db, change, policy)
                          or _out_of_turn(db, engine, change))
                if not reason and change.kind in (ChangeKind.UPDATE, LATE):
                    reason = _unreconciled(db, change.member_id, change.ref)
            if reason:
                result.refused.append(Refused(change, reason))
                continue
            try:
                applied = _write(db, engine, change)
            except Exception as e:              # noqa: BLE001 - report per cell
                logger.exception("Loan grid change failed for %s %s",
                                 change.ref, change.month)
                result.refused.append(Refused(change, str(e)))
                continue
            if applied is None:
                continue
            result.applied.append(applied)
            result.members_touched.add(change.member_id)

        for member_id in result.members_touched:
            engine.balance_recalculator.recalculate_balances(member_id)
            engine.balance_recalculator.recalculate_default_deduction(member_id)
    return result


def _unreconciled(db, member_id, ref):
    """Why this loan's history cannot be re-derived, as a refusal, or None.

    Deleting, recording or correcting a deduction is worked out by re-deriving
    the loan (docs §4.2). A loan whose rows already disagree with its own
    schedule cannot be re-derived, so the consequences of the change could not
    be written — its cached balance would stop following its ledger. Refusing
    is the honest answer; such a loan is corrected by hand.
    """
    from src.services.loan_projection import reconciles
    ok, why = reconciles(db, member_id, ref)
    if ok:
        return None
    return (f"{ref} does not reconcile with its own ledger ({why}), so what this "
            f"change does to it cannot be worked out. Correct the loan's history in "
            f"the member's ledger first.")


def _batch_order(change):
    """Per loan: deletions newest first, then corrections and missed months,
    then new deductions and operations in month order (docs §4.4)."""
    month = int(change.month.replace("-", ""))
    if change.kind == ChangeKind.DELETE:
        return (change.loan_id, 0, -month)
    if change.kind in (ChangeKind.UPDATE, LATE):
        return (change.loan_id, 1, month)
    return (change.loan_id, 2, month)


def _judge_batch_deletions(db, deletions):
    """Decide every staged deletion against the journal as it is now.

    Returns ``({id(change): mode}, {loan_id: reason})``. A loan with any
    deletion refused has all of them refused: half of a loan's months deleted
    is not what anybody asked for.
    """
    modes, refused = {}, {}
    by_loan = defaultdict(list)
    for change in deletions:
        by_loan[change.loan_id].append(change)
    for loan_id, group in by_loan.items():
        unreconciled = _unreconciled(db, group[0].member_id, group[0].ref)
        if unreconciled:
            refused[loan_id] = unreconciled
            continue
        timeline = _timeline(db, group[0].member_id, group[0].ref)
        dated = {row_id: (date, kind) for date, row_id, kind in timeline}
        missing = [c for c in group if c.row_id not in dated]
        if missing:
            refused[loan_id] = (f"The deduction for {month_label(missing[0].month)} no "
                                f"longer exists — reload the grid.")
            continue
        found, why = judge_deletions(
            timeline, [(c.row_id, *dated[c.row_id]) for c in group])
        if why:
            refused[loan_id] = why
            continue
        modes.update({id(c): mode for c, mode in zip(group, found)})
    return modes, refused


#: The policy operation each obligation-creating change answers to. A buy-off
#: or a suspension asks nothing new of the member, so neither is gated.
_POLICY_KIND = {DEDUCT: "loan.deduct", TOP_UP: "loan.top_up"}


def _refusal_for(db, change, policy):
    """The membership rules, for the change kinds that create an obligation."""
    kind = _POLICY_KIND.get(change.kind)
    if kind is None:
        return None             # correcting or removing creates nothing new
    refusals = policy.check(db, Operation(kind, member_id=change.member_id,
                                          loan_ref=change.ref))
    return refusals[0].reason if refusals else None


def _due_date(engine, member_id, loan):
    """The date the loan is next due, stepping past recorded suspensions.

    The same walk :meth:`LoanService.deduct_single_loan` does before it
    deducts, so a check made here agrees with where the deduction would land.
    """
    due = loan.get("next_due_date")
    if not due:
        return None
    service = engine.loan_service
    windows = service._suspension_windows(member_id, loan["id"])
    cursor = datetime.strptime(str(due)[:10], _DATE)
    for _ in range(600):                        # an open-ended window is capped
        if not service._date_in_suspension(cursor.strftime(_DATE), windows):
            break
        cursor += relativedelta(months=1)
    return cursor.strftime(_DATE)


def _out_of_turn(db, engine, change):
    """Why a schedule-moving change no longer fits the loan, or None.

    Staged changes are checked against the loan as it was read. By the time a
    later one is written, an earlier one in the same batch — a buy-off, a
    suspension — may have moved the loan on, and writing it anyway would land
    a deduction in a month nobody typed it into.
    """
    if change.kind not in (DEDUCT, TOP_UP, BUYOFF, SUSPEND):
        return None
    loan = db.get_loan_by_ref(change.member_id, change.ref)
    if not loan:
        return f"{change.ref} no longer exists."
    if loan["status"] != "Active":
        return f"{change.ref} is {str(loan['status']).lower()} now."
    due = _due_date(engine, change.member_id, loan)
    if due is None or month_key(due) != change.month:
        when = month_label(month_key(due)) if due else "no recorded month"
        return f"{change.ref} is now due for {when}, so this month was not taken."
    if change.kind == SUSPEND and loan.get("is_suspended"):
        return f"{change.ref} is already suspended."
    if change.kind == TOP_UP:
        last = db.conn.execute(
            "SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=?",
            (change.member_id, change.ref)).fetchone()[0]
        # The Top-Up dialog's rule: a top-up lands in a month after the loan's
        # last entry, never alongside or before it.
        if last and month_key(last) >= change.month:
            return (f"{change.ref} already has an entry in {month_label(month_key(last))}; "
                    f"a top-up must fall in a later month.")
    return None


def _write(db, engine, change):
    """Perform one change. Never fabricates a posting."""
    from src.services.loan_projection import clear_derived

    if change.kind == DEDUCT:
        return _deduct(db, engine, change)
    if change.kind == TOP_UP:
        return _top_up(db, engine, change)
    if change.kind == BUYOFF:
        return _buyoff(db, engine, change)
    if change.kind == SUSPEND:
        return _suspend(db, engine, change)
    if change.kind == ChangeKind.DELETE:
        return _delete(db, change)
    if change.kind == LATE:
        return _record_late(db, change)

    row = db.conn.execute(
        "SELECT id, date, deducted, principal_portion, interest_portion "
        "FROM ledger WHERE id=?", (change.row_id,)).fetchone()
    if row is None:
        raise ValueError("That deduction no longer exists.")

    # UPDATE: the amount is an assertion, the split is not. Clearing the split
    # is what makes the projection recompute it — without it the new figure
    # changes nothing at all — and every later split follows from it too.
    restore = {"deducted": row[2], "split": (row[3], row[4]),
               "later": _resplit_after(db, change.member_id, change.ref, row[1], row[0])}
    db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                    (change.magnitude, change.row_id))
    clear_derived(db, change.row_id)
    _project(db, change)
    return Applied(change, row_id=change.row_id, restore=restore)


def _project(db, change):
    """Re-derive one loan inside the batch, so the next change sees it current."""
    from src.services.loan_projection import project_loan
    project_loan(db, change.member_id, change.ref, commit=False)


def _resplit_after(db, member_id, ref, date, row_id):
    """Clear the split on every deduction after ``(date, row_id)``.

    A split is derived from everything before it, so changing an earlier
    deduction — removing it, recording it, correcting it — changes every one
    after it. Returns the splits as they were, for an exact undo.
    """
    from src.services.loan_projection import clear_derived
    later = db.conn.execute(
        "SELECT id, principal_portion, interest_portion FROM ledger "
        "WHERE individual_id=? AND loan_id=? AND event_type='Repayment' "
        "AND (date > ? OR (date = ? AND id > ?)) ORDER BY date, id",
        (member_id, ref, str(date)[:10], str(date)[:10], row_id)).fetchall()
    for later_id, _principal, _interest in later:
        clear_derived(db, later_id)
    return [tuple(r) for r in later]


def _restore_splits(db, splits):
    for row_id, principal, interest in splits or ():
        db.ledger.set_portions(row_id, principal, interest)


def _delete(db, change):
    """Remove one deduction — un-run or missed, as judged for the batch (§4.2)."""
    payment = db.ledger.snapshot(change.row_id)
    if payment is None:
        raise ValueError("That deduction no longer exists.")
    restore = {"mode": change.mode, "rows": [payment]}

    if change.mode == UNRUN:
        # The month is undone: its interest charge goes too, and the interest
        # returns to what the loan has still to charge (§4.5).
        accrual = db.conn.execute(
            "SELECT id FROM ledger WHERE individual_id=? AND loan_id=? "
            "AND event_type='Interest Earned' AND date=? AND id < ? "
            "ORDER BY id DESC LIMIT 1",
            (change.member_id, change.ref, payment["date"], payment["id"])).fetchone()
        if accrual:
            charge = db.ledger.snapshot(accrual[0])
            restore["rows"].insert(0, charge)       # put back first: it sorts first
            restore["unearned"] = float(charge["added"] or 0)
            db.ledger.delete(charge["id"])
            db.loans.adjust_unearned(change.loan_id, restore["unearned"])
    else:
        # Missed: the month stays charged; what follows settles it first.
        restore["later"] = _resplit_after(db, change.member_id, change.ref,
                                          payment["date"], payment["id"])
    db.ledger.delete(payment["id"])
    _project(db, change)
    return Applied(change, row_id=None, restore=restore)


def _record_late(db, change):
    """Record the deduction for a missed month, where its interest was charged."""
    accrual = db.conn.execute(
        "SELECT id, date FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Interest Earned' AND date=? ORDER BY id DESC LIMIT 1",
        (change.member_id, change.ref, change.date)).fetchone()
    if accrual is None:
        raise ValueError("No interest is charged in that month any more — reload.")
    paid = db.conn.execute(
        "SELECT 1 FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type IN ('Repayment', 'Loan Buyoff') AND date LIKE ?",
        (change.member_id, change.ref, change.month + "%")).fetchone()
    if paid:
        raise ValueError("That month has a deduction now — reload.")
    later = _resplit_after(db, change.member_id, change.ref, accrual[1], accrual[0])
    row_id = db.add_transaction(change.member_id, accrual[1], "Repayment", change.ref,
                                0, change.magnitude, 0,
                                "Monthly Deduction (grid entry, recorded late)")
    _project(db, change)
    return Applied(change, row_id=row_id, restore={"later": later})


def _deduct(db, engine, change):
    """Take one scheduled deduction, then correct it to the figure asked for.

    The deduction itself is :meth:`LoanService.deduct_single_loan` — the same
    call the Deduct button makes, so the accrual, the split and the due date are
    the engine's work and not a copy of it. Only when the operator asked for a
    figure other than the instalment is the resulting row then corrected, which
    is the same two steps somebody would do by hand.
    """
    from src.services.loan_projection import clear_derived

    # Past the end of a fixed-term suspension the loan resumes, recorded at the
    # boundary rather than today — what catch-up does when it reaches it.
    loan = db.get_loan_by_ref(change.member_id, change.ref)
    until = loan.get("suspend_until")
    if loan.get("is_suspended") and until and (
            _due_date(engine, change.member_id, loan) or "") >= str(until)[:10]:
        db.resume_loan(loan["id"], resumed_date=str(until)[:10])

    engine.loan_service.deduct_single_loan(change.member_id, change.ref)
    row = db.conn.execute(
        "SELECT id, deducted FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Repayment' ORDER BY date DESC, id DESC LIMIT 1",
        (change.member_id, change.ref)).fetchone()
    if row is None:
        raise ValueError(f"{change.ref} did not take a deduction.")

    row_id, taken = row[0], round(float(row[1] or 0), 2)
    # A final month owes less than the instalment, and the engine takes only
    # what is owed. Forcing the staged figure back on top over-collects from a
    # loan that is already closed — Fill Forward stages the full instalment
    # for every month, so this is the ordinary case, not an edge.
    settled = (db.get_loan_by_ref(change.member_id, change.ref) or {}).get("status") == "Paid"
    if settled and change.magnitude > taken:
        return Applied(change, row_id=row_id, restore=None)
    if taken != change.magnitude:
        db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                        (change.magnitude, row_id))
        clear_derived(db, row_id)
    return Applied(change, row_id=row_id, restore=None)


def _latest(db, change, event_type):
    row = db.conn.execute(
        "SELECT id FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=? "
        "ORDER BY id DESC LIMIT 1", (change.member_id, change.ref, event_type)).fetchone()
    return row[0] if row else None


def _top_up(db, engine, change):
    """The Top-Up button, dated on the loan's due day in this month."""
    loan = db.get_loan_by_ref(change.member_id, change.ref)
    date = _due_date(engine, change.member_id, loan)
    engine.loan_service.top_up_loan(change.member_id, change.ref, change.magnitude,
                                    change.months, date)
    row_id = _latest(db, change, "Loan Top-Up")
    if row_id is None:
        raise ValueError(f"{change.ref} did not take the top-up.")
    # The ledger row's snapshot restores the loan's figures on undo, but not
    # its term, which top_up_loan rewrites separately.
    return Applied(change, row_id=row_id,
                   restore={"loan_id": loan["id"],
                            "duration": loan.get("duration_months")})


def _buyoff(db, engine, change):
    """The Buyoff button, dated on the loan's due day — where it would land anyway."""
    loan = db.get_loan_by_ref(change.member_id, change.ref)
    paid = engine.loan_service.buyoff_loan(change.member_id, change.ref,
                                           _due_date(engine, change.member_id, loan))
    row_id = _latest(db, change, "Loan Buyoff")
    if row_id is None:
        raise ValueError(f"{change.ref} has nothing left to settle.")
    return Applied(change, row_id=row_id, restore={"paid": paid})


def _suspend(db, engine, change):
    """The Suspend dialog's fixed-period choice, starting at the due date."""
    loan = db.get_loan_by_ref(change.member_id, change.ref)
    start = _due_date(engine, change.member_id, loan)
    end = (datetime.strptime(start, _DATE)
           + relativedelta(months=change.months)).strftime(_DATE)
    db.record_suspension(loan["id"], start, end)
    span = db.conn.execute(
        "SELECT id FROM loan_suspensions WHERE loan_id=? ORDER BY id DESC LIMIT 1",
        (loan["id"],)).fetchone()
    return Applied(change, row_id=span[0] if span else None,
                   restore={"start": start, "end": end})


def revert(db, engine, result):
    """Put a batch back. The grid's own undo, as for the funds."""
    touched = set()
    with db.transaction():
        for applied in reversed(result.applied):
            change = applied.change
            touched.add(change.member_id)
            if change.kind == DEDUCT:
                _undo_deduct(db, change, applied)
            elif change.kind in (TOP_UP, BUYOFF):
                # Delete Last's own path: removes the row (and a buy-off's
                # realised interest) and restores the loan from its snapshot.
                if applied.row_id is not None:
                    engine.transaction_manager.delete_transaction(change.member_id,
                                                                  applied.row_id)
                restore = applied.restore or {}
                if change.kind == TOP_UP and restore.get("duration"):
                    db.loans.set_terms(restore["loan_id"], restore["duration"])
            elif change.kind == SUSPEND:
                if applied.row_id is not None:
                    db.delete_suspension(applied.row_id)
            elif change.kind == ChangeKind.DELETE:
                restore = applied.restore or {}
                # Original ids: within a date, rows are ordered by id, and the
                # projection pairs stored rows with derived postings by that
                # order. Putting them back under new ids would reorder them.
                for snapshot in restore.get("rows", []):
                    db.ledger.reinstate(snapshot)
                if restore.get("unearned"):
                    db.loans.adjust_unearned(change.loan_id, -restore["unearned"])
                _restore_splits(db, restore.get("later"))
            elif change.kind == LATE:
                if applied.row_id is not None:
                    db.ledger.delete(applied.row_id)
                _restore_splits(db, (applied.restore or {}).get("later"))
            else:
                restore = applied.restore or {}
                db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                                (restore.get("deducted"), change.row_id))
                db.ledger.set_portions(change.row_id, *restore.get("split", (0, 0)))
                _restore_splits(db, restore.get("later"))
        for member_id in touched:
            engine.balance_recalculator.recalculate_balances(member_id)
            engine.balance_recalculator.recalculate_default_deduction(member_id)
    return touched


def _undo_deduct(db, change, applied):
    """Remove a deduction the grid took, and the accrual that came with it.

    The accrual's interest goes back to unearned. Without that, every undo of a
    deduction forgave a month's interest for good (docs §4.5).
    """
    if applied.row_id is None:
        return
    row = db.conn.execute("SELECT date FROM ledger WHERE id=?",
                          (applied.row_id,)).fetchone()
    db.conn.execute("DELETE FROM ledger WHERE id=?", (applied.row_id,))
    if row:
        accrual = db.conn.execute(
            "SELECT id, added FROM ledger WHERE individual_id=? AND loan_id=? "
            "AND event_type='Interest Earned' AND date=? ORDER BY id DESC LIMIT 1",
            (change.member_id, change.ref, row[0])).fetchone()
        if accrual:
            db.conn.execute("DELETE FROM ledger WHERE id=?", (accrual[0],))
            db.loans.adjust_unearned(change.loan_id, float(accrual[1] or 0))


# --------------------------------------------------------------------------- #
# Fill forward
# --------------------------------------------------------------------------- #

@dataclass
class FillPlan:
    changes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    in_future: int = 0
    suspended: int = 0

    @property
    def members(self):
        return len({c.loan_id for c in self.changes})

    def notes(self):
        out = []
        if self.in_future:
            out.append(f"{self.in_future} month(s) in the future were left empty.")
        if self.suspended:
            out.append(f"{self.suspended} suspended month(s) were skipped.")
        return out


def plan_fill(db, grid, rows, months, policy=default_policy, today=None):
    """Propose the scheduled deduction for each loan's outstanding months.

    This *is* catch-up, expressed as a table: a loan is walked from its next due
    month through the end of the range, one instalment per month, skipping
    suspended months exactly as :meth:`LoanService.catch_up_loan` does. Nothing
    before the due date is proposed, because a loan cannot pay March before
    February, and nothing after this month, because it has not happened.
    """
    today = today or datetime.now().strftime(_DATE)
    this_month = month_key(today)

    plan = FillPlan()
    for row in rows:
        if row.status != "Active":
            plan.skipped.append((row, f"{row.ref} is {row.status.lower()}."))
            continue
        if row.excluded:
            plan.skipped.append((row, f"{row.name} is excluded from this ledger."))
            continue
        if not row.member_active:
            plan.skipped.append((row, f"{row.name} has left the SACCO."))
            continue
        if row.is_suspended and not row.suspend_until:
            # Open-ended: on hold until somebody resumes it. A fixed-term
            # suspension is filled around — its months are skipped below and
            # the deduction after it resumes the loan, as catch-up does.
            plan.skipped.append((row, f"{row.ref} is suspended until further notice."))
            continue
        if row.installment <= 0:
            plan.skipped.append((row, f"{row.ref} has no instalment recorded."))
            continue
        due = row.next_due_month
        if not due:
            plan.skipped.append((row, f"{row.ref} has no due date recorded."))
            continue

        windows = grid.suspensions.get(row.loan_id) or []
        proposed, cursor = [], month_key(row.stored_due) if row.stored_due else due
        for month in months:
            if month < cursor:
                continue
            if month > this_month:
                plan.in_future += 1
                continue
            if _in_suspension(month, windows, row.due_day):
                plan.suspended += 1
                continue
            if row.cell(month).rows:
                continue
            proposed.append(month)

        if not proposed:
            plan.skipped.append((row, f"{row.ref} is up to date."))
            continue
        for month in proposed:
            plan.changes.append(Change(row.loan_id, row.member_id, row.ref, month,
                                       DEDUCT, amount=row.installment,
                                       before=row.cell(month).fingerprint()))
    return plan


# --------------------------------------------------------------------------- #
# Deleting months — docs/grid/deleting-months.md §4
# --------------------------------------------------------------------------- #

def plan_delete_months(db, grid, rows, months):
    """Plan deleting ``months`` for the given (on-screen) loans.

    Each loan's deductions in those months are judged together, newest first
    (§4.4): un-run, missed, or — if any is refused — none of them.
    """
    months = sorted(months)
    plan = DeletePlan(months=months)
    shown = {row.loan_id for row in rows if not row.excluded}
    structural = reopened = 0

    for row in rows:
        if row.excluded:
            continue
        structural += sum(1 for d, _i, k in row.timeline
                          if k in _STRUCTURAL and month_key(d) in months)
        cells = [(m, row.cell(m)) for m in months if row.cell(m).rows]
        if not cells:
            continue
        if any(cell.state == CellState.MIXED for _m, cell in cells):
            plan.refused.append((row, "A month here holds two deductions; open the "
                                      "member's ledger to work on them."))
            continue
        payments = [(cell.row_id, cell.rows[0]["date"], cell.rows[0]["transaction_type"])
                    for _m, cell in cells]
        modes, why = judge_deletions(row.timeline, payments)
        if not why:
            why = _unreconciled(db, row.member_id, row.ref)
        if why:
            plan.refused.append((row, why))
            continue
        for (month, cell), mode in zip(cells, modes):
            plan.changes.append(Change(row.loan_id, row.member_id, row.ref, month,
                                       ChangeKind.DELETE, row_id=cell.row_id,
                                       before=cell.fingerprint(), mode=mode))
            plan.tally("un-run" if mode == UNRUN else "missed", cell.net)
        if row.status == "Paid":
            reopened += 1

    # Month by month, not first-to-last: March and July are not April to June.
    others = sum(1 for (loan_id,) in db.conn.execute(
        "SELECT l.id FROM ledger g JOIN loans l ON l.individual_id = g.individual_id "
        "AND l.ref = g.loan_id WHERE g.event_type IN ('Repayment', 'Loan Buyoff') "
        f"AND substr(g.date, 1, 7) IN ({','.join('?' * len(months))})", months)
        if loan_id not in shown)
    if others:
        plan.untouched.append(
            f"{others} deduction(s) on loans not on screen (paid off, filtered out, "
            f"past members or excluded) are not touched.")
    if structural:
        plan.untouched.append(
            f"{structural} loan issue(s), top-up(s) or restructure(s) in these months "
            f"are not deleted — they are not deductions.")
    if plan.refused:
        plan.untouched.append(f"{len(plan.refused)} loan(s) are refused — listed below "
                              f"— and keep all their deductions.")

    unrun = plan.tallies.get("un-run", (0, 0))[0]
    missed = plan.tallies.get("missed", (0, 0))[0]
    if unrun:
        plan.consequences.append(
            f"Un-run ({unrun}): the deduction is the loan's last entry. It goes with "
            f"that month's interest charge, the interest returns to the loan's unearned "
            f"interest, and the loan falls due for that month again.")
    if missed:
        plan.consequences.append(
            f"Missed ({missed}): later deductions exist. Only the deduction goes — the "
            f"month's interest stays charged — and every later deduction is re-split, "
            f"interest first. The shortfall stays in the balance; the loan runs one "
            f"instalment longer and its due date does not move.")
    if reopened:
        plan.consequences.append(f"{reopened} paid-off loan(s) reopen.")
    if missed:
        plan.consequences.append(
            "If a month was a payment holiday rather than a missed payment, suspend it "
            "instead (@S): no interest for it, and the term extends.")
    return plan


def blocked_members(db, rows, policy=default_policy):
    """Loans whose member may take no new deduction, as ``{loan_id: reason}``."""
    out = {}
    for row in rows:
        refusals = policy.check(db, Operation("loan.deduct", member_id=row.member_id,
                                              loan_ref=row.ref))
        if refusals:
            out[row.loan_id] = refusals[0].reason
    return out


class LoanBackend:
    """The loan schedule behind the same interface a fund grid presents.

    Deliberately the same surface as :class:`~src.services.fund_grid.FundBackend`
    so both grids are one screen. The differences are all here: a row is a loan,
    the trailing columns are the instalment and what is left owing, and a change
    is written by the engine rather than as a row.
    """

    lead_columns = [("Member", "name"), ("Loan", "ref"), ("Due", "next_due")]
    tail_columns = [("Paid in period", "period"), ("Outstanding", "balance")]
    per_row_noun = "loan"

    fund = "loans"
    label = LABEL
    noun = NOUN

    def __init__(self):
        self.spec = None
        self.include_paid = False
        self._rows = {}

    def load(self, db, engine, months, include_inactive=True, include_excluded=False):
        grid = load(db, months, include_inactive=include_inactive,
                    include_paid=self.include_paid,
                    include_excluded=include_excluded)
        self._rows = {row.loan_id: row for row in grid.rows}
        return grid

    def empty(self):
        return LoanGrid(months=[])

    ops = OPS

    def stage(self, grid, row, month, text):
        return stage(grid, row, month, text)

    def cell_refusal(self, grid, row, month):
        return cell_refusal(grid, row, month)

    def edit_text(self, row, month):
        cell = row.cell(month)
        return figure(cell.rows[0]["amount"]) if cell.state == CellState.SINGLE else ""

    def display_text(self, row, month):
        return None

    def staged_text(self, change):
        if change.kind in (TOP_UP, BUYOFF, SUSPEND):
            return OPS.format([change.entry])
        return None

    def staged_edit_text(self, change):
        return "" if change.kind == ChangeKind.DELETE else OPS.format([change.entry])

    def plan_delete_months(self, db, grid, rows, months):
        return plan_delete_months(db, grid, rows, months)

    def cell_note(self, row, month):
        """The tooltip for a missed month (§4.6), or None."""
        if not row.missed(month):
            return None
        charge = row.accruals[month]
        return (f"Missed: interest of {charge['amount']:,.0f} was charged on "
                f"{charge['date']} and nothing was paid. Type a figure to record the "
                f"deduction for this month.")

    def describe(self, change, staged=None):
        """A staged change in words, for its tooltip.

        ``staged`` is everything else staged, so a deletion is described as it
        will actually run — a month is un-run, not missed, when the deductions
        after it are being deleted in the same save.
        """
        if change.kind == ChangeKind.DELETE:
            return self._describe_deletion(change, staged or {})
        if change.kind == LATE:
            return (f"Records the missed deduction of {change.amount:,.0f} for "
                    f"{month_label(change.month)}; later deductions are re-split.")
        if change.kind == TOP_UP:
            return (f"Top up {change.ref} by {change.amount:,.0f}, re-termed over "
                    f"{change.months} month(s).")
        if change.kind == BUYOFF:
            return f"Buy off {change.ref}: settle everything owed on it this month."
        if change.kind == SUSPEND:
            return (f"Suspend {change.ref} for {change.months} month(s) from "
                    f"{month_label(change.month)}.")
        return f"Deduct {change.amount:,.0f} from {change.ref}."

    def _describe_deletion(self, change, staged):
        row = self._rows.get(change.loan_id)
        mode = change.mode
        if row is not None:
            payments = [(c.row_id, row.cell(c.month).rows[0]["date"], "Repayment")
                        for c in staged.values()
                        if c.kind == ChangeKind.DELETE and c.loan_id == change.loan_id
                        and row.cell(c.month).rows]
            if not any(p[0] == change.row_id for p in payments):
                payments.append((change.row_id, row.cell(change.month).rows[0]["date"],
                                 "Repayment"))
            modes, _why = judge_deletions(row.timeline, payments)
            if modes:
                mode = modes[[p[0] for p in payments].index(change.row_id)]
        if mode == UNRUN:
            return ("Un-runs this month: the deduction and its interest charge go, the "
                    "interest returns to unearned, and the month falls due again.")
        return ("Marks this month missed: the deduction goes, its interest stays "
                "charged, and later deductions are re-split interest first.")

    def apply(self, db, engine, changes):
        return apply_changes(db, engine, changes)

    def revert(self, db, engine, result):
        return revert(db, engine, result)

    def plan_fill(self, db, grid, rows, months):
        return plan_fill(db, grid, rows, months)

    def blocked(self, db, rows):
        return blocked_members(db, rows)

    def lead_values(self, row):
        return [row.name, row.ref,
                month_label(row.next_due_month) if row.next_due_month else "—"]

    def tail_values(self, row, months):
        return [row.period_total(months), row.balance]

    def row_note(self, row, blocked):
        if row.excluded:
            return (f"{row.name} is excluded from this ledger."
                    + (f" {row.exclusion_note}" if row.exclusion_note else ""))
        if row.is_suspended:
            until = row.suspend_until or "resumed"
            return f"{row.ref} is suspended until {until}."
        if not row.member_active:
            return blocked.get(row.key, f"{row.name} has left the SACCO.")
        if row.status != "Active":
            return f"{row.ref} is {row.status.lower()}."
        return None


#: Re-exported so a view can treat this and a fund grid the same way.
__all__ = ["LABEL", "NOUN", "DEDUCT", "LoanGrid", "LoanRow", "Change", "FillPlan",
           "load", "stage", "cell_refusal", "apply_changes", "revert", "plan_fill",
           "blocked_members", "month_range", "month_label", "CellState", "ChangeKind"]
