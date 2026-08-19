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
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

from src.services.fund_grid import (Cell, CellState, ChangeKind, month_bounds,
                                    month_key, month_label, month_range)
from src.services.policy import Operation, default_policy

logger = logging.getLogger(__name__)

_DATE = "%Y-%m-%d"

#: Ledger events that count as money paid against a loan in a month.
PAYMENTS = ("Repayment", "Loan Buyoff")

#: A deduction the grid will take by running the engine forward one month.
DEDUCT = "deduct"

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
    #: visible window — the only one that can be removed without stranding it.
    latest_payment_id: int = None
    cells: dict = field(default_factory=dict)

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

    def cell(self, month):
        return self.cells.get(month) or Cell()

    def period_total(self, months):
        return round(sum(self.cell(m).net for m in months), 2)


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


def _in_suspension(month, windows):
    """A month is suspended when any day of it falls inside a window."""
    first = month + "-01"
    return any(first >= start and (end is None or first < end)
               for start, end in windows)


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

    # The most recent deduction on each loan, over its whole history. Removing
    # any earlier one leaves a gap the derivation cannot replay across, so this
    # is what the delete guard is measured against — and it must look past the
    # visible window, or scrolling back a year would make old rows deletable.
    latest = {}
    for member_id, ref, row_id in db.conn.execute(
            "SELECT individual_id, loan_id, id FROM ledger WHERE event_type='Repayment' "
            "ORDER BY individual_id, loan_id, date, id"):
        latest[(member_id, ref)] = row_id

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

        grid.rows.append(LoanRow(
            member_id=member_id, loan_id=loan_id, ref=ref, name=name or "",
            pf_no=pf_no or "", installment=round(float(installment or 0), 2),
            balance=round(float(balance or 0), 2), next_due=next_due,
            status=status, is_suspended=bool(suspended), suspend_until=suspend_until,
            member_active=member_active, excluded=member_id in excluded,
            exclusion_note=excluded.get(member_id, ""),
            issued_month=issued.get((member_id, ref)),
            latest_payment_id=latest.get((member_id, ref)), cells=cells))
        if suspended or db.get_loan_suspensions_for_loan(loan_id):
            grid.suspensions[loan_id] = _suspension_windows(db, loan_id)
    return grid


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
    if _in_suspension(month, windows):
        until = row.suspend_until or "further notice"
        return (f"{row.ref} is suspended for this month (until {until}). "
                f"Resume it before deducting.")
    if row.issued_month and month < row.issued_month:
        return f"{row.ref} was not issued until {month_label(row.issued_month)}."
    if not row.next_due_month:
        return f"{row.ref} has no due date recorded."
    if month < row.next_due_month:
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
    text = (text or "").strip().replace(",", "").replace("—", "").replace("–", "")

    if text in ("", "-"):
        if cell.state == CellState.EMPTY:
            return None, None
        if cell.row_id != row.latest_payment_id:
            return None, ("Only the loan's most recent deduction can be removed "
                          "here. Removing an earlier one leaves a gap the loan "
                          "cannot be re-derived across, and its balance would "
                          "stop following from its own ledger.")
        return Change(row.loan_id, row.member_id, row.ref, month, ChangeKind.DELETE,
                      row_id=cell.row_id, before=cell.fingerprint()), None

    try:
        amount = round(float(text), 2)
    except ValueError:
        return None, f"{text!r} is not a number."
    if amount <= 0:
        return None, ("A deduction must be more than zero — clear the cell to "
                      "remove it.")

    if cell.state == CellState.EMPTY:
        return Change(row.loan_id, row.member_id, row.ref, month, DEDUCT,
                      amount=amount, before=cell.fingerprint()), None

    if round(cell.rows[0]["amount"], 2) == amount:
        return None, None
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

    def summary(self):
        bits = []
        if self.deducted:
            bits.append(f"{self.deducted} deducted")
        if self.updated:
            bits.append(f"{self.updated} corrected")
        if self.deleted:
            bits.append(f"{self.deleted} removed")
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

    ordered = sorted(changes, key=lambda c: (c.loan_id, c.month))
    with db.transaction():
        for change in ordered:
            reason = _refusal_for(db, change, policy)
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


def _refusal_for(db, change, policy):
    """The membership rules, for the one change kind that creates an obligation."""
    if change.kind != DEDUCT:
        return None             # correcting or removing creates nothing new
    refusals = policy.check(db, Operation("loan.deduct", member_id=change.member_id,
                                          loan_ref=change.ref))
    return refusals[0].reason if refusals else None


def _write(db, engine, change):
    """Perform one change. Never fabricates a posting."""
    from src.services.loan_projection import clear_derived

    if change.kind == DEDUCT:
        return _deduct(db, engine, change)

    row = db.conn.execute(
        "SELECT id, date, event_type, deducted, notes FROM ledger WHERE id=?",
        (change.row_id,)).fetchone()
    if row is None:
        raise ValueError("That deduction no longer exists.")
    restore = {"id": row[0], "date": row[1], "event_type": row[2],
               "deducted": row[3], "notes": row[4]}

    if change.kind == ChangeKind.DELETE:
        # The month's accrual goes with the deduction. Leaving it behind is what
        # makes a loan un-projectable: the derivation counts an accrual the
        # payments no longer justify.
        month = month_key(row[1])
        accruals = db.conn.execute(
            "SELECT id, date, added, notes FROM ledger WHERE individual_id=? "
            "AND loan_id=? AND event_type='Interest Earned' AND date LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (change.member_id, change.ref, f"{month}%")).fetchone()
        restore["accrual"] = ({"id": accruals[0], "date": accruals[1],
                               "added": accruals[2], "notes": accruals[3]}
                              if accruals else None)
        db.conn.execute("DELETE FROM ledger WHERE id=?", (change.row_id,))
        if accruals:
            db.conn.execute("DELETE FROM ledger WHERE id=?", (accruals[0],))
        return Applied(change, row_id=None, restore=restore)

    # UPDATE: the amount is an assertion, the split is not. Clearing the split
    # is what makes the projection recompute it — without it the new figure
    # changes nothing at all.
    db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                    (change.magnitude, change.row_id))
    clear_derived(db, change.row_id)
    return Applied(change, row_id=change.row_id, restore=restore)


def _deduct(db, engine, change):
    """Take one scheduled deduction, then correct it to the figure asked for.

    The deduction itself is :meth:`LoanService.deduct_single_loan` — the same
    call the Deduct button makes, so the accrual, the split and the due date are
    the engine's work and not a copy of it. Only when the operator asked for a
    figure other than the instalment is the resulting row then corrected, which
    is the same two steps somebody would do by hand.
    """
    from src.services.loan_projection import clear_derived

    engine.loan_service.deduct_single_loan(change.member_id, change.ref)
    row = db.conn.execute(
        "SELECT id, deducted FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Repayment' ORDER BY date DESC, id DESC LIMIT 1",
        (change.member_id, change.ref)).fetchone()
    if row is None:
        raise ValueError(f"{change.ref} did not take a deduction.")

    row_id, taken = row[0], round(float(row[1] or 0), 2)
    if taken != change.magnitude:
        db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                        (change.magnitude, row_id))
        clear_derived(db, row_id)
    return Applied(change, row_id=row_id, restore=None)


def revert(db, engine, result):
    """Put a batch back. The grid's own undo, as for the funds."""
    touched = set()
    with db.transaction():
        for applied in reversed(result.applied):
            change = applied.change
            touched.add(change.member_id)
            if change.kind == DEDUCT:
                _undo_deduct(db, change, applied)
            elif change.kind == ChangeKind.DELETE:
                row = applied.restore or {}
                # The accrual goes back FIRST. Both rows carry the same date, so
                # their order is decided by insertion id — and project_loan pairs
                # stored rows with derived postings by position within a date,
                # checking the date but not the kind. Restored the other way
                # round, the accrual's derived figures land on the repayment row.
                accrual = row.get("accrual")
                if accrual:
                    db.conn.execute(
                        "INSERT INTO ledger (individual_id, date, event_type, loan_id, "
                        "added, deducted, balance, notes) VALUES (?, ?, ?, ?, ?, 0, 0, ?)",
                        (change.member_id, accrual["date"], "Interest Earned",
                         change.ref, accrual["added"], accrual["notes"] or ""))
                db.conn.execute(
                    "INSERT INTO ledger (individual_id, date, event_type, loan_id, "
                    "added, deducted, balance, notes) VALUES (?, ?, ?, ?, 0, ?, 0, ?)",
                    (change.member_id, row.get("date"), row.get("event_type"),
                     change.ref, row.get("deducted"), row.get("notes") or ""))
            else:
                row = applied.restore or {}
                db.conn.execute("UPDATE ledger SET deducted=? WHERE id=?",
                                (row.get("deducted"), change.row_id))
                from src.services.loan_projection import clear_derived
                clear_derived(db, change.row_id)
        for member_id in touched:
            engine.balance_recalculator.recalculate_balances(member_id)
            engine.balance_recalculator.recalculate_default_deduction(member_id)
    return touched


def _undo_deduct(db, change, applied):
    """Remove a deduction the grid took, and the accrual that came with it."""
    if applied.row_id is None:
        return
    row = db.conn.execute("SELECT date FROM ledger WHERE id=?",
                          (applied.row_id,)).fetchone()
    db.conn.execute("DELETE FROM ledger WHERE id=?", (applied.row_id,))
    if row:
        accrual = db.conn.execute(
            "SELECT id FROM ledger WHERE individual_id=? AND loan_id=? "
            "AND event_type='Interest Earned' AND date=? ORDER BY id DESC LIMIT 1",
            (change.member_id, change.ref, row[0])).fetchone()
        if accrual:
            db.conn.execute("DELETE FROM ledger WHERE id=?", (accrual[0],))


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
        if row.is_suspended:
            plan.skipped.append((row, f"{row.ref} is suspended."))
            continue
        if row.installment <= 0:
            plan.skipped.append((row, f"{row.ref} has no instalment recorded."))
            continue
        due = row.next_due_month
        if not due:
            plan.skipped.append((row, f"{row.ref} has no due date recorded."))
            continue

        windows = grid.suspensions.get(row.loan_id) or []
        proposed, cursor = [], due
        for month in months:
            if month < cursor:
                continue
            if month > this_month:
                plan.in_future += 1
                continue
            if _in_suspension(month, windows):
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

    def load(self, db, engine, months, include_inactive=True, include_excluded=False):
        return load(db, months, include_inactive=include_inactive,
                    include_paid=self.include_paid,
                    include_excluded=include_excluded)

    def empty(self):
        return LoanGrid(months=[])

    def stage(self, grid, row, month, text):
        return stage(grid, row, month, text)

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
