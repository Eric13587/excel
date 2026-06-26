"""Benevolent fund service.

A perpetual welfare contribution: each member is enrolled with a fixed monthly
amount and a due-date schedule (loan-like). Deductions accumulate a running
total per member and the fund is never 'paid off' — there is no balance to
clear, only an ever-growing total contributed. Supports Catch Up and a single
Deduct, mirroring the loan deduction mechanics.
"""
import uuid
from datetime import datetime
from dateutil.relativedelta import relativedelta

_TABLE = "benevolent_ledger"


class BenevolentService:
    """Per-member perpetual benevolent contribution."""

    def __init__(self, db_manager):
        self.db = db_manager

    # ------------------------------------------------------------------ #
    # Enrolment / config
    # ------------------------------------------------------------------ #
    def get_account(self, individual_id):
        return self.db.get_benevolent_account(individual_id)

    def is_enrolled(self, individual_id):
        acc = self.get_account(individual_id)
        return bool(acc and acc.get('active') and (acc.get('monthly_amount') or 0) > 0)

    def enroll(self, individual_id, monthly_amount, start_date):
        """Enrol (or re-configure) a member. First contribution is due on start_date."""
        self.db.upsert_benevolent_account(individual_id, float(monthly_amount), start_date, start_date)
        return True

    def get_total(self, individual_id):
        """Running total contributed so far."""
        return self.db.fund_balance(_TABLE, individual_id)

    def get_transactions(self, individual_id):
        return self.db.fund_transactions(_TABLE, individual_id)

    def recalculate(self, individual_id):
        self.db.fund_recalculate(_TABLE, individual_id)

    def delete_all(self, individual_id):
        """Remove all contributions and reset the schedule back to the start date."""
        self.db.fund_delete_all(_TABLE, individual_id)
        acc = self.get_account(individual_id)
        if acc and acc.get('start_date'):
            self.db.set_benevolent_next_due(individual_id, acc['start_date'])
        return True

    # ------------------------------------------------------------------ #
    # Deductions
    # ------------------------------------------------------------------ #
    def add_payout(self, individual_id, amount, date_str, notes="", batch_id=None):
        """Record a welfare payout (claim) from the fund against a member.

        Stored as a Withdrawal, which reduces the running total and, in the GL,
        posts Dr Benevolent Fund / Cr Cash. Does not touch the contribution
        schedule (next_due) — contributions continue as normal.
        """
        if float(amount) <= 0:
            return False
        self.db.fund_add_transaction(_TABLE, individual_id, date_str, "Withdrawal",
                                     float(amount), notes or "Benevolent Payout", batch_id)
        return True

    def deduct_single(self, individual_id, batch_id=None):
        """Take one contribution at the next due date and advance the schedule."""
        acc = self.get_account(individual_id)
        if not acc or not acc.get('active'):
            return 0
        amount = float(acc.get('monthly_amount') or 0)
        next_due = acc.get('next_due_date')
        if amount <= 0 or not next_due:
            return 0
        self.db.fund_add_transaction(_TABLE, individual_id, next_due, "Contribution", amount,
                                     "Benevolent Contribution", batch_id)
        new_due = (datetime.strptime(next_due, "%Y-%m-%d") + relativedelta(months=1)).strftime("%Y-%m-%d")
        self.db.set_benevolent_next_due(individual_id, new_due)
        return 1

    def catch_up(self, individual_id, batch_id=None, target_date=None):
        """Take a contribution for every due month from next_due through the target.

        Returns the number of contributions added.
        """
        acc = self.get_account(individual_id)
        if not acc or not acc.get('active'):
            return 0
        amount = float(acc.get('monthly_amount') or 0)
        next_due = acc.get('next_due_date')
        if amount <= 0 or not next_due:
            return 0

        if target_date is None:
            limit = datetime.now().strftime("%Y-%m-%d")
        elif isinstance(target_date, datetime):
            limit = target_date.strftime("%Y-%m-%d")
        else:
            limit = target_date

        count = 0
        while next_due <= limit:
            self.db.fund_add_transaction(_TABLE, individual_id, next_due, "Contribution", amount,
                                         "Benevolent Contribution (Auto)", batch_id)
            next_due = (datetime.strptime(next_due, "%Y-%m-%d") + relativedelta(months=1)).strftime("%Y-%m-%d")
            count += 1
        if count:
            self.db.set_benevolent_next_due(individual_id, next_due)
        return count

    def mass_catch_up(self, individual_ids, progress_callback=None, target_date=None):
        """Catch up many members in one transaction. Returns (processed, total, batch_id, errors)."""
        batch_id = str(uuid.uuid4())
        processed = total = 0
        errors = []
        with self.db.transaction():
            for i, ind in enumerate(individual_ids):
                try:
                    n = self.catch_up(ind, batch_id=batch_id, target_date=target_date)
                    if n:
                        processed += 1
                        total += n
                except Exception as e:
                    errors.append((ind, str(e)))
                if progress_callback:
                    progress_callback(i, ind)
        return processed, total, batch_id, errors

    def revert_batch(self, batch_id):
        """Undo a catch-up batch: delete its contributions, restore each member's
        next-due to the batch's earliest date, and recalc the running total."""
        if not batch_id:
            return False
        cur = self.db.conn.cursor()
        # earliest contribution per member == that member's next_due before the run
        cur.execute(f"SELECT individual_id, MIN(date) FROM {_TABLE} WHERE batch_id=? "
                    f"GROUP BY individual_id", (batch_id,))
        rows = cur.fetchall()
        self.db.fund_delete_batch(_TABLE, batch_id)
        for ind, earliest in rows:
            self.db.set_benevolent_next_due(ind, earliest)
            self.db.fund_recalculate(_TABLE, ind)
        return True
