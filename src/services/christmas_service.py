"""Christmas fund service.

A second savings pot dedicated to Christmas. It mirrors the Savings/Shares
mechanics — deposits, a monthly auto-contribution with Catch Up, and a running
balance — but withdrawals are locked outside the configured unlock month
(default December, setting ``christmas_unlock_month``).
"""
import uuid
from datetime import datetime
from dateutil.relativedelta import relativedelta

from src.exceptions import ChristmasLockedError

_TABLE = "christmas_savings"


class ChristmasService:
    """Per-member Christmas savings."""

    def __init__(self, db_manager):
        self.db = db_manager

    # ------------------------------------------------------------------ #
    # Balance & config
    # ------------------------------------------------------------------ #
    def get_balance(self, individual_id):
        return self.db.fund_balance(_TABLE, individual_id)

    def get_transactions(self, individual_id):
        return self.db.fund_transactions(_TABLE, individual_id)

    def get_unlock_month(self):
        try:
            return int(self.db.get_setting("christmas_unlock_month", "12"))
        except (TypeError, ValueError):
            return 12

    def withdrawals_allowed(self, date_str):
        """True if a withdrawal dated date_str falls in the unlock month."""
        try:
            month = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").month
        except ValueError:
            return False
        return month == self.get_unlock_month()

    def get_suggested_increment(self, individual_id):
        df = self.get_transactions(individual_id)
        deposits = df[df['transaction_type'] == 'Deposit'] if not df.empty else df
        if deposits is None or deposits.empty:
            return float(self.db.get_setting("default_christmas_increment", "0") or 0)
        try:
            mode = deposits['amount'].mode()
            if not mode.empty:
                return float(mode.iloc[0])
        except Exception:
            pass
        return float(deposits.iloc[-1]['amount'])

    # ------------------------------------------------------------------ #
    # Transactions
    # ------------------------------------------------------------------ #
    def add_deposit(self, individual_id, amount, date_str, notes="", batch_id=None):
        self.db.fund_add_transaction(_TABLE, individual_id, date_str, "Deposit", amount, notes, batch_id)
        return True

    def add_withdrawal(self, individual_id, amount, date_str, notes="", batch_id=None,
                       allow_override=False):
        """Withdraw from the Christmas pot.

        Refused outside the unlock month unless allow_override is set (an admin
        deliberately overriding the lock).
        """
        if not allow_override and not self.withdrawals_allowed(date_str):
            raise ChristmasLockedError(self.get_unlock_month())
        self.db.fund_add_transaction(_TABLE, individual_id, date_str, "Withdrawal", amount, notes, batch_id)
        return True

    def recalculate(self, individual_id):
        self.db.fund_recalculate(_TABLE, individual_id)

    # ------------------------------------------------------------------ #
    # Monthly auto-contribution catch-up (mirrors savings)
    # ------------------------------------------------------------------ #
    def catch_up(self, individual_id, monthly_amount=None, batch_id=None, target_date=None):
        """Add the monthly contribution from the last deposit up to the target.

        Like savings: starts the month after the last deposit and adds one
        'Deposit' per month through the target month (default: current month).
        Returns the number of contributions added.
        """
        df = self.get_transactions(individual_id)
        if df.empty:
            return 0
        deposits = df[df['transaction_type'] == 'Deposit']
        last = deposits.iloc[-1] if not deposits.empty else df.iloc[-1]
        try:
            last_date = datetime.strptime(str(last['date']).split()[0], "%Y-%m-%d")
        except ValueError:
            return 0

        if not monthly_amount or monthly_amount <= 0:
            monthly_amount = self.get_suggested_increment(individual_id)
        if monthly_amount <= 0:
            return 0

        if target_date:
            if isinstance(target_date, str):
                target_date = datetime.strptime(target_date, "%Y-%m-%d")
            limit = (target_date + relativedelta(months=1)).replace(day=1)
        else:
            limit = (datetime.now() + relativedelta(months=1)).replace(day=1)

        nxt = (last_date + relativedelta(months=1)).replace(day=1)
        count = 0
        while nxt < limit:
            self.db.fund_add_transaction(_TABLE, individual_id, nxt.strftime("%Y-%m-%d"),
                                         "Deposit", monthly_amount,
                                         "Monthly Christmas Contribution (Auto)", batch_id)
            nxt += relativedelta(months=1)
            count += 1
        return count

    def mass_catch_up(self, individual_ids, progress_callback=None, target_date=None):
        """Catch up many members in one transaction. Returns (processed, total, batch_id, errors)."""
        batch_id = str(uuid.uuid4())
        processed = total = 0
        errors = []
        with self.db.transaction():
            for i, ind in enumerate(individual_ids):
                try:
                    n = self.catch_up(ind, None, batch_id=batch_id, target_date=target_date)
                    if n:
                        processed += 1
                        total += n
                except Exception as e:
                    errors.append((ind, str(e)))
                if progress_callback:
                    progress_callback(i, ind)
        return processed, total, batch_id, errors
