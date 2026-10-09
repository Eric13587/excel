"""The ``loans`` and ``loan_suspensions`` tables.

Segregated principal/interest model: ``balance`` is outstanding **principal
only**, ``interest_balance`` is interest accrued but unpaid, and
``unearned_interest`` is interest scheduled but not yet accrued. All three are
caches that ``BalanceRecalculator`` re-derives by replaying the ledger.

``ref`` (``L-001``) is generated per member, so it is **unique per member only**
— every read and write that takes a ref also takes the member id, in the
signature, so it cannot be forgotten (the rule in CLAUDE.md).

``loans.is_suspended``/``suspend_until`` hold only the *current* state; the full
history of spans lives in ``loan_suspensions``, which is what lets statements
annotate a past suspension after the loan has resumed.

Three different update keys survive this refactor — ``id``,
``(id, individual_id)`` and ``(ref, individual_id)`` — because they are what the
callers do today (finding **D5**). They are named here so the divergence is
visible rather than scattered; consolidating on the primary key changes which
rows a statement can reach, so it belongs to a later phase.

Repositories do not commit; the caller owns the transaction.
"""
import logging
from datetime import datetime

from src.db.base import Repository

logger = logging.getLogger(__name__)

ACTIVE = "Active"
PAID = "Paid"


class LoanRepository(Repository):
    """Every statement that touches ``loans`` or ``loan_suspensions``."""

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def by_ref(self, member_id, ref):
        """One loan, scoped by member because refs repeat across members."""
        return self._dict("SELECT * FROM loans WHERE individual_id=? AND ref=?",
                          (member_id, ref))

    def for_member(self, member_id):
        """Every loan for a member, Active and Paid."""
        return self._dicts("SELECT * FROM loans WHERE individual_id=?", (member_id,))

    def active_for_member(self, member_id):
        return self._dicts("SELECT * FROM loans WHERE individual_id=? AND status='Active'",
                           (member_id,))

    def all_active(self):
        return self._dicts("SELECT * FROM loans WHERE status='Active'")

    def active_refs(self):
        """(member_id, ref) for every active loan, for whole-portfolio sweeps."""
        return self._rows(
            "SELECT individual_id, ref FROM loans WHERE status='Active' ORDER BY individual_id, ref")

    def overdue_count(self, as_of):
        return self._scalar(
            "SELECT COUNT(*) FROM loans WHERE status='Active' AND next_due_date < ?",
            (as_of,), 0)

    def overdue(self, as_of):
        """Every active loan past its due date, with the member behind it.

        The same population :meth:`overdue_count` counts — deliberately, so the
        dashboard's arrears figure and the list you get by clicking it can never
        disagree. Suspended loans are included and flagged rather than filtered,
        because whether a deliberate hold counts as arrears is the reader's call,
        not this query's. Oldest due date first: that is the order the arrears
        are worked in.
        """
        return self._dicts(
            "SELECT l.id AS loan_id, l.individual_id, l.ref, l.balance, "
            "       l.installment, l.interest_balance, l.next_due_date, "
            "       l.start_date, l.is_suspended, l.suspend_until, "
            "       i.name, i.phone, i.pf_no, i.employment_status, i.is_retired "
            "FROM loans l JOIN individuals i ON i.id = l.individual_id "
            "WHERE l.status='Active' AND l.next_due_date < ? "
            "ORDER BY l.next_due_date, i.name, l.ref", (as_of,))

    def has_active(self, member_id):
        return self._scalar(
            "SELECT COUNT(*) FROM loans WHERE individual_id=? AND status='Active'",
            (member_id,), 0) > 0

    def terms(self, member_id, ref):
        """(id, total_amount, installment, monthly_interest) — the loan-heal probe."""
        return self._row(
            "SELECT id, total_amount, installment, monthly_interest FROM loans "
            "WHERE individual_id=? AND ref=?", (member_id, ref))

    def contract_terms(self, member_id, ref):
        """(duration_months, interest_rate, principal), or None values where the
        loan predates migration 003 and its issue note did not parse."""
        return self._row(
            "SELECT duration_months, interest_rate, principal FROM loans "
            "WHERE individual_id=? AND ref=?", (member_id, ref))

    def without_contract_terms(self):
        """Loans still missing a duration — the backfill's leftovers.

        Reported rather than guessed: inventing a duration changes what a loan
        is contracted to repay.
        """
        return self._dicts(
            "SELECT id, individual_id, ref, status, principal FROM loans "
            "WHERE duration_months IS NULL OR duration_months <= 0 "
            "ORDER BY individual_id, ref")

    def identity(self, loan_id):
        """(ref, individual_id) for a loan, or (None, None)."""
        row = self._row("SELECT ref, individual_id FROM loans WHERE id=?", (loan_id,))
        return (row[0], row[1]) if row else (None, None)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def add(self, member_id, ref, principal, total, balance, installment,
            monthly_interest, start_date, next_due_date, unearned_interest=0,
            duration_months=None, interest_rate=None):
        """Issue a loan. interest_balance starts at 0 and status at 'Active'.

        ``duration_months`` and ``interest_rate`` are the loan's own terms.
        Until migration 003 they existed only inside the issue note's prose, so
        a note edit changed the loan's mathematics.
        """
        cur = self._execute(
            "INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
            "installment, monthly_interest, start_date, next_due_date, unearned_interest, "
            "interest_balance, status, duration_months, interest_rate) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'Active', ?, ?)",
            (member_id, ref, principal, total, balance, installment, monthly_interest,
             start_date, next_due_date, unearned_interest, duration_months, interest_rate))
        return cur.lastrowid

    def adjust_unearned(self, loan_id, delta):
        """Move interest between charged and still-to-charge.

        Removing a month's interest charge must hand it back here, and putting
        the charge back must take it out again: for every loan, interest charged
        plus unearned interest is what the loan was written for.
        """
        self._execute(
            "UPDATE loans SET unearned_interest = ROUND(COALESCE(unearned_interest, 0) + ?, 2) "
            "WHERE id=?", (delta, loan_id))

    def set_terms(self, loan_id, duration_months, interest_rate=None):
        """Record a new duration (and optionally rate) after a top-up or
        restructure, so the stored terms keep describing the live loan."""
        if interest_rate is None:
            self._execute("UPDATE loans SET duration_months=? WHERE id=?",
                          (duration_months, loan_id))
        else:
            self._execute("UPDATE loans SET duration_months=?, interest_rate=? WHERE id=?",
                          (duration_months, interest_rate, loan_id))

    def add_imported(self, member_id, ref, principal, total_amount, balance, installment,
                     start_date, next_due_date, status, monthly_interest,
                     unearned_interest, interest_balance, import_id):
        """Insert a loan copied from a foreign journal, tagged with its run."""
        cur = self._execute(
            "INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
            "installment, start_date, next_due_date, status, monthly_interest, "
            "unearned_interest, interest_balance, import_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (member_id, ref, principal, total_amount, balance, installment, start_date,
             next_due_date, status, monthly_interest, unearned_interest,
             interest_balance, import_id))
        return cur.lastrowid

    def all_refs(self):
        """Every loan ref in the journal, for import collision detection.

        Refs are unique per member, so this over-collects deliberately: the
        importer renames on any collision rather than reasoning per member.
        """
        return {r[0] for r in self._rows("SELECT ref FROM loans")}

    def id_for_ref(self, member_id, ref):
        return self._scalar("SELECT id FROM loans WHERE individual_id=? AND ref=?",
                            (member_id, ref))

    def set_status(self, loan_id, balance, next_due_date, status,
                   interest_balance=None, unearned_interest=None):
        """Post-deduction state. The two optional columns are only written when
        supplied, so a caller that does not know them cannot zero them."""
        sql = "UPDATE loans SET balance=?, next_due_date=?, status=?"
        params = [balance, next_due_date, status]
        if interest_balance is not None:
            sql += ", interest_balance=?"
            params.append(interest_balance)
        if unearned_interest is not None:
            sql += ", unearned_interest=?"
            params.append(unearned_interest)
        self._execute(sql + " WHERE id=?", (*params, loan_id))

    def set_details(self, loan_id, total_amount, balance, installment, monthly_interest,
                    next_due_date, unearned_interest=None, principal=None,
                    interest_balance=None):
        """Restructure/top-up terms, with the same optional-column contract."""
        sql = ("UPDATE loans SET total_amount=?, balance=?, installment=?, "
               "monthly_interest=?, next_due_date=?")
        params = [total_amount, balance, installment, monthly_interest, next_due_date]
        if unearned_interest is not None:
            sql += ", unearned_interest=?"
            params.append(unearned_interest)
        if principal is not None:
            sql += ", principal=?"
            params.append(principal)
        if interest_balance is not None:
            sql += ", interest_balance=?"
            params.append(interest_balance)
        self._execute(sql + " WHERE id=?", (*params, loan_id))

    def set_recalc_state(self, loan_id, monthly_interest, unearned_interest):
        """Terms re-derived from a history replay."""
        self._execute("UPDATE loans SET monthly_interest=?, unearned_interest=? WHERE id=?",
                      (monthly_interest, unearned_interest, loan_id))

    def set_next_due(self, loan_id, next_due_date, member_id=None):
        """Advance the schedule. ``member_id`` adds the redundant scope that
        ``catch_up_loan`` uses; the loan id alone already identifies the row."""
        if member_id is None:
            self._execute("UPDATE loans SET next_due_date=? WHERE id=?",
                          (next_due_date, loan_id))
        else:
            self._execute("UPDATE loans SET next_due_date=? WHERE id=? AND individual_id=?",
                          (next_due_date, loan_id, member_id))

    def set_catch_up_state(self, loan_id, member_id, balance, interest_balance,
                           unearned_interest, next_due_date, status):
        """Final state after a catch-up run (keyed on id AND member — see D5)."""
        self._execute(
            "UPDATE loans SET balance=?, interest_balance=?, unearned_interest=?, "
            "next_due_date=?, status=? WHERE id=? AND individual_id=?",
            (balance, interest_balance, unearned_interest, next_due_date, status,
             loan_id, member_id))

    def reset_to_issued(self, loan_id, principal, unearned_interest, next_due_date):
        """Return a loan to its issued state before a schedule rebuild."""
        self._execute(
            "UPDATE loans SET balance=?, interest_balance=0, unearned_interest=?, "
            "next_due_date=?, status='Active' WHERE id=?",
            (principal, unearned_interest, next_due_date, loan_id))

    def set_healed_terms(self, loan_id, total_amount, installment, monthly_interest,
                         unearned_interest):
        """Terms re-derived from the issue note by the loan-heal tool."""
        self._execute(
            "UPDATE loans SET total_amount=?, installment=?, monthly_interest=?, "
            "unearned_interest=? WHERE id=?",
            (total_amount, installment, monthly_interest, unearned_interest, loan_id))

    def set_schedule_by_ref(self, member_id, ref, installment, monthly_interest):
        """Refresh the repayment schedule, keyed on (ref, member) — see D5.

        Correct only because it also filters on the member: an earlier version
        keyed on ``ref`` alone and updated other members' loans, which is the
        origin of the scoping rule in CLAUDE.md.
        """
        self._execute(
            "UPDATE loans SET installment=?, monthly_interest=? WHERE ref=? AND individual_id=?",
            (installment, monthly_interest, ref, member_id))

    def restore(self, loan_id, principal, total_amount, balance, installment,
                interest_balance, unearned_interest, monthly_interest,
                next_due_date, status):
        """Restore a full loan snapshot (undo)."""
        self._execute(
            "UPDATE loans SET principal=?, total_amount=?, balance=?, installment=?, "
            "interest_balance=?, unearned_interest=?, monthly_interest=?, "
            "next_due_date=?, status=? WHERE id=?",
            (principal, total_amount, balance, installment, interest_balance,
             unearned_interest, monthly_interest, next_due_date, status, loan_id))

    def delete_with_ledger(self, member_id, ref):
        """Delete a loan and its ledger rows. Caller supplies the transaction."""
        self._execute("DELETE FROM ledger WHERE individual_id=? AND loan_id=?",
                      (member_id, ref))
        self._execute("DELETE FROM loans WHERE individual_id=? AND ref=?", (member_id, ref))

    # ------------------------------------------------------------------ #
    # Suspensions
    # ------------------------------------------------------------------ #

    def suspensions_for_member(self, member_id):
        return self._dicts(
            "SELECT id, loan_id, individual_id, loan_ref, start_date, suspend_until, "
            "resumed_date, status FROM loan_suspensions WHERE individual_id=? "
            "ORDER BY start_date, id", (member_id,))

    def suspensions_for_loan(self, loan_id):
        """Every recorded span for one loan, earliest first.

        Scoped by the loan's primary key rather than its ref, because a ref is
        unique per member only.
        """
        return self._dicts(
            "SELECT id, loan_id, individual_id, loan_ref, start_date, suspend_until, "
            "resumed_date, status, created_at FROM loan_suspensions WHERE loan_id=? "
            "ORDER BY start_date, id", (loan_id,))

    def suspension(self, suspension_id):
        """One span, or None."""
        return self._dict(
            "SELECT id, loan_id, individual_id, loan_ref, start_date, suspend_until, "
            "resumed_date, status, created_at FROM loan_suspensions WHERE id=?",
            (suspension_id,))

    def delete_suspension(self, suspension_id):
        """Remove a recorded span entirely.

        Distinct from closing one: closing says "the suspension ended", which
        leaves the skipped months skipped. Deleting says "this never applied",
        so catch-up will deduct those months again.
        """
        self._execute("DELETE FROM loan_suspensions WHERE id=?", (suspension_id,))

    def open_suspension_id(self, loan_id):
        """The one open span for a loan, if any.

        Migration 001 added a partial unique index enforcing at most one, but
        the ORDER BY is kept so behaviour is identical on an unmigrated journal.
        """
        return self._scalar(
            "SELECT id FROM loan_suspensions WHERE loan_id=? AND resumed_date IS NULL "
            "AND status='active' ORDER BY id DESC LIMIT 1", (loan_id,))

    def open_suspension(self, loan_id):
        """The open span itself rather than just its id, or None.

        Same row :meth:`open_suspension_id` picks; the UI needs its dates to
        say what is actually in force before offering to end it.
        """
        return self._dict(
            "SELECT id, loan_id, individual_id, loan_ref, start_date, suspend_until, "
            "resumed_date, status, created_at FROM loan_suspensions WHERE loan_id=? "
            "AND resumed_date IS NULL AND status='active' ORDER BY id DESC LIMIT 1",
            (loan_id,))

    def set_suspended(self, loan_id, until_date):
        self._execute("UPDATE loans SET is_suspended=1, suspend_until=? WHERE id=?",
                      (until_date, loan_id))

    def clear_suspended(self, loan_id):
        self._execute("UPDATE loans SET is_suspended=0, suspend_until=NULL WHERE id=?",
                      (loan_id,))

    def extend_suspension(self, suspension_id, until_date):
        """Move only the intended resume date; the original start is preserved
        so the recorded span still covers the whole skipped period."""
        self._execute("UPDATE loan_suspensions SET suspend_until=? WHERE id=?",
                      (until_date, suspension_id))

    def add_suspension(self, loan_id, member_id, loan_ref, start_date, suspend_until,
                       resumed_date=None, status="active", created_at=None):
        """``created_at`` is supplied only by the importer, which preserves the
        source journal's stamp rather than restamping the copy."""
        self._execute(
            "INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, "
            "suspend_until, resumed_date, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (loan_id, member_id, loan_ref, start_date, suspend_until, resumed_date, status,
             created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def close_open_suspension(self, loan_id, resumed_date):
        self._execute(
            "UPDATE loan_suspensions SET resumed_date=?, status='resumed' "
            "WHERE id = (SELECT id FROM loan_suspensions WHERE loan_id=? "
            "AND resumed_date IS NULL ORDER BY id DESC LIMIT 1)",
            (resumed_date, loan_id))
