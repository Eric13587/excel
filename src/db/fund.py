"""Member fund subledgers: ``savings``, ``christmas_savings``, ``benevolent_ledger``
plus the ``benevolent_accounts`` enrolment table.

The three transaction tables are structurally identical, and ``DatabaseManager``
already drove two of them through one generic API over a whitelisted table name.
This repository extends that treatment to ``savings`` as well, so all three are
reached the same way. The table name is interpolated, so it always goes through
:meth:`Repository._checked_identifier` against :data:`TABLES`; every value is
parameterised.

Two behaviours are preserved verbatim even though they are known defects,
because Phase 2 is a refactor:

* **Three different running-balance semantics** survive as three separate
  methods (:meth:`recalc_savings_by_date`, :meth:`recalc_savings_by_id`,
  :meth:`recalc_fund`). They disagree about what an ``Interest`` row does and
  about ordering — finding **D1**, five implementations with three semantics.
  Collapsing them changes numbers, so it is Phase 3's job; keeping them side by
  side here at least makes the disagreement visible in one file.
* **Only ``savings`` writes fire the general-ledger post hook.** Christmas and
  Benevolent activity reaches the GL only on a full sync, which is why 12
  Christmas deposits were missing from the books until one was run. Wiring the
  hook here would change when journals are posted; that is Phase 5.
"""
import logging

import pandas as pd

from src.db.base import Repository

logger = logging.getLogger(__name__)

SAVINGS = "savings"
CHRISTMAS = "christmas_savings"
BENEVOLENT = "benevolent_ledger"

#: The only table names this repository will interpolate.
TABLES = frozenset({SAVINGS, CHRISTMAS, BENEVOLENT})


class FundRepository(Repository):
    """Every statement that touches a member fund subledger lives here."""

    def _table(self, table):
        return self._checked_identifier(table, TABLES)

    # -- reads ------------------------------------------------------------ #

    def latest_balance(self, table, member_id):
        """The running balance carried on the member's most recent row.

        A stored snapshot, not a recomputed sum: if a mid-history row is edited
        without a recalculation it goes stale (finding D9).
        """
        t = self._table(table)
        return self._scalar(
            f"SELECT balance FROM {t} WHERE individual_id=? ORDER BY date DESC, id DESC LIMIT 1",
            (member_id,), 0.0) or 0.0

    def transactions(self, table, member_id, start_date=None, end_date=None, order="date, id"):
        """A member's fund history as a DataFrame."""
        t = self._table(table)
        sql = f"SELECT * FROM {t} WHERE individual_id = ?"
        params = [member_id]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        return pd.read_sql_query(sql + f" ORDER BY {order}", self.conn, params=tuple(params))

    def get(self, table, row_id):
        t = self._table(table)
        return self._dict(
            f"SELECT id, individual_id, date, transaction_type, amount, notes FROM {t} WHERE id=?",
            (row_id,))

    def members_in_batch(self, table, batch_id):
        """Members touched by a mass run, for post-revert recalculation."""
        t = self._table(table)
        return [r[0] for r in
                self._rows(f"SELECT DISTINCT individual_id FROM {t} WHERE batch_id=?", (batch_id,))]

    def earliest_date_per_member_in_batch(self, table, batch_id):
        """(member_id, earliest date) pairs — a member's next-due before the run."""
        t = self._table(table)
        return self._rows(
            f"SELECT individual_id, MIN(date) FROM {t} WHERE batch_id=? GROUP BY individual_id",
            (batch_id,))

    def gl_projection_rows(self, table):
        """(id, date, transaction_type, amount) in posting order, for the GL."""
        t = self._table(table)
        return self._rows(
            f"SELECT id, date, transaction_type, amount FROM {t} ORDER BY date, id")

    def gl_projection_row(self, table, row_id):
        """The same projection columns for one row (the live write-path hook)."""
        t = self._table(table)
        return self._row(
            f"SELECT date, transaction_type, amount FROM {t} WHERE id=?", (row_id,))

    # -- writes ----------------------------------------------------------- #

    def add(self, table, member_id, date, transaction_type, amount, balance,
            notes="", batch_id=None):
        """Append a fund row with its running balance already computed."""
        t = self._table(table)
        cur = self._execute(
            f"INSERT INTO {t} (individual_id, date, transaction_type, amount, balance, "
            f"notes, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, date, transaction_type, amount, balance, notes, batch_id))
        return cur.lastrowid

    def add_imported(self, table, member_id, date, transaction_type, amount, balance,
                     notes, batch_id):
        """Copy a fund row from a foreign journal, balance included.

        The running balance is recalculated afterwards, so the copied value is
        only a starting point.
        """
        t = self._table(table)
        self._execute(
            f"INSERT INTO {t} (individual_id, date, transaction_type, amount, balance, "
            f"notes, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, date, transaction_type, amount, balance, notes, batch_id))

    def add_imported_savings(self, member_id, date, transaction_type, amount, balance,
                             notes, import_id):
        """Copy a savings row from a foreign journal, tagged with its run.

        Note this carries ``import_id`` and no ``batch_id``, unlike the fund
        tables — savings is the only subledger the importer marks for undo.
        """
        self._execute(
            "INSERT INTO savings (individual_id, date, transaction_type, amount, balance, "
            "notes, import_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (member_id, date, transaction_type, amount, balance, notes, import_id))

    def replace_enrolment(self, member_id, monthly_amount, start_date, next_due_date, active):
        """INSERT OR REPLACE an enrolment copied from a foreign journal."""
        self._execute(
            "INSERT OR REPLACE INTO benevolent_accounts "
            "(individual_id, monthly_amount, start_date, next_due_date, active) "
            "VALUES (?, ?, ?, ?, ?)",
            (member_id, monthly_amount, start_date, next_due_date, active))

    def update(self, table, row_id, date, amount, notes):
        t = self._table(table)
        self._execute(f"UPDATE {t} SET date=?, amount=?, notes=? WHERE id=?",
                      (date, amount, notes, row_id))

    def set_balance(self, table, row_id, balance):
        t = self._table(table)
        self._execute(f"UPDATE {t} SET balance=? WHERE id=?", (balance, row_id))

    def delete(self, table, row_id):
        t = self._table(table)
        self._execute(f"DELETE FROM {t} WHERE id=?", (row_id,))

    def delete_batch(self, table, batch_id):
        t = self._table(table)
        self._execute(f"DELETE FROM {t} WHERE batch_id=?", (batch_id,))

    def delete_all_for_member(self, table, member_id):
        t = self._table(table)
        self._execute(f"DELETE FROM {t} WHERE individual_id=?", (member_id,))

    # -- running-balance recalculation (three semantics, see module docstring) - #

    def _ordered_rows(self, table, member_id, order):
        t = self._table(table)
        return self._rows(
            f"SELECT id, transaction_type, amount FROM {t} "
            f"WHERE individual_id=? ORDER BY {order}", (member_id,))

    def recalc_savings_by_date(self, member_id, cursor=None):
        """Ordered by (date, id); Interest **increases** the balance; no rounding.

        The variant used by the import path, which passes its own cursor so the
        work joins the caller's transaction.
        """
        rows = self._ordered_rows(SAVINGS, member_id, "date ASC, id ASC")
        running = 0.0
        updates = []
        for row_id, ttype, amount in rows:
            if ttype in ("Deposit", "Interest"):
                running += amount
            elif ttype == "Withdrawal":
                running -= amount
            updates.append((running, row_id))
        (cursor or self.conn).executemany(
            "UPDATE savings SET balance=? WHERE id=?", updates)

    def recalc_savings_by_id(self, member_id):
        """Ordered by id alone; anything that is not a Deposit **decreases** the
        balance — so Interest subtracts here and adds in the sibling method.

        Preserved as-is; reconciled in Phase 3.
        """
        running = 0.0
        for row_id, ttype, amount in self._ordered_rows(SAVINGS, member_id, "id"):
            running += amount if ttype == "Deposit" else -amount
            self.set_balance(SAVINGS, row_id, running)

    def recalc_fund(self, table, member_id):
        """Ordered by (date, id); only Withdrawal decreases; rounded to 2dp."""
        t = self._table(table)
        running = 0.0
        for row_id, ttype, amount in self._ordered_rows(t, member_id, "date ASC, id ASC"):
            running = round(running - amount if ttype == "Withdrawal" else running + amount, 2)
            self.set_balance(t, row_id, running)

    # -- benevolent enrolment (1:0..1 per member) ------------------------- #

    def get_enrolment(self, member_id):
        return self._dict(
            "SELECT id, individual_id, monthly_amount, start_date, next_due_date, active "
            "FROM benevolent_accounts WHERE individual_id=?", (member_id,))

    def upsert_enrolment(self, member_id, monthly_amount, start_date, next_due_date):
        if self.get_enrolment(member_id):
            self._execute(
                "UPDATE benevolent_accounts SET monthly_amount=?, start_date=?, "
                "next_due_date=?, active=1 WHERE individual_id=?",
                (monthly_amount, start_date, next_due_date, member_id))
        else:
            self._execute(
                "INSERT INTO benevolent_accounts (individual_id, monthly_amount, start_date, "
                "next_due_date, active) VALUES (?, ?, ?, ?, 1)",
                (member_id, monthly_amount, start_date, next_due_date))

    def set_next_due(self, member_id, next_due_date):
        self._execute("UPDATE benevolent_accounts SET next_due_date=? WHERE individual_id=?",
                      (next_due_date, member_id))
