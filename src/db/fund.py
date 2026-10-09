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

* **The running balance now has one implementation** (:meth:`recalculate`) and
  the balance one reader (:meth:`balance`). Finding **D1** — five
  implementations with three semantics — is resolved; see Phase 3 in
  docs/db/phase-3-balance-consolidation.md for what changed and for whom.
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

    def balance(self, table, member_id):
        """A member's current fund balance.

        Summed from the rows rather than read off the most recent one. The
        stored ``balance`` column is a per-row running total for display; it
        goes stale the moment a back-dated row is inserted without a
        recalculation, and reading it was reporting two members' savings
        wrongly on the production journal. A sum cannot go stale.
        """
        t = self._table(table)
        return round(self._scalar(
            f"SELECT COALESCE(SUM(CASE transaction_type WHEN 'Withdrawal' "
            f"THEN -amount ELSE amount END), 0) FROM {t} WHERE individual_id=?",
            (member_id,), 0.0) or 0.0, 2)

    def total(self, table):
        """Portfolio-wide balance across every member for a fund.

        The whole-journal counterpart of :meth:`balance`, summed the same way
        (deposits minus withdrawals) so the dashboard total agrees with the sum
        of the members. One query, not one per member.
        """
        t = self._table(table)
        return round(self._scalar(
            f"SELECT COALESCE(SUM(CASE transaction_type WHEN 'Withdrawal' "
            f"THEN -amount ELSE amount END), 0) FROM {t}", (), 0.0) or 0.0, 2)

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

    def movements_in_range(self, table, start_date, end_date):
        """Every member's rows in a date window, in one query.

        The per-member reader (:meth:`transactions`) is right for one member's
        statement and wrong for a screen showing hundreds at once — that is a
        query per member per fund. This is the whole-journal read: one pass,
        ordered so a caller can group by member and month as it goes.
        """
        t = self._table(table)
        return self._dicts(
            f"SELECT id, individual_id, date, transaction_type, amount, notes, batch_id "
            f"FROM {t} WHERE date >= ? AND date <= ? "
            f"ORDER BY individual_id, date, id", (start_date, end_date))

    def first_movement_dates(self, table):
        """``{member_id: earliest date}`` for a fund, in one query.

        When a member's account in a fund effectively opened. Anything before it
        is a month the account did not exist for, which is not the same as a
        month with no contribution.
        """
        return {row[0]: row[1] for row in self._rows(
            f"SELECT individual_id, MIN(date) FROM {self._table(table)} "
            f"GROUP BY individual_id")}

    def last_dated_row(self, table, member_id, kinds=None):
        """The member's most recent row in a fund, optionally by type.

        Ordered by ``(date, id)`` — the order a statement shows — so "the last
        contribution" means the latest dated one, not the last inserted.
        """
        t = self._table(table)
        sql = f"SELECT id, date, transaction_type, amount FROM {t} WHERE individual_id=?"
        params = [member_id]
        if kinds:
            sql += f" AND transaction_type IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        return self._dict(sql + " ORDER BY date DESC, id DESC LIMIT 1", tuple(params))

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

    def set_type(self, table, row_id, transaction_type):
        """Change what a row *is* — a deposit into a withdrawal, say.

        Separate from :meth:`update` because the type carries the sign of the
        movement, so changing it is a different act from correcting a figure.
        """
        t = self._table(table)
        self._execute(f"UPDATE {t} SET transaction_type=? WHERE id=?",
                      (transaction_type, row_id))

    def snapshot(self, table, row_id):
        """Every column of one row, for putting it back exactly. None if gone."""
        t = self._table(table)
        return self._dict(f"SELECT * FROM {t} WHERE id=?", (row_id,))

    def reinstate(self, table, snapshot):
        """Re-insert a row from :meth:`snapshot`, original id included.

        Rows sharing a date are ordered by id — on a statement and in the
        running balance — so putting a deleted row back under a new id can
        move it past its neighbours: a deposit and a withdrawal on the same day
        trade places, and the balance between them changes.
        """
        t = self._table(table)
        known = {row[1] for row in self.conn.execute(f"PRAGMA table_info({t})")}
        columns = [c for c in snapshot if c in known]
        self._execute(
            f"INSERT INTO {t} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            tuple(snapshot[c] for c in columns))
        if "created_at" in snapshot and snapshot["created_at"] is None:
            # The insert trigger stamps a missing creation time with "now"; a
            # restored row was not created now, and its time was never known.
            self._execute(f"UPDATE {t} SET created_at=NULL WHERE id=?", (snapshot["id"],))

    def history(self, table, member_id):
        """A member's rows as ``(id, date, transaction_type, amount)``, in
        statement order — what a running balance is computed over."""
        t = self._table(table)
        return self._rows(
            f"SELECT id, date, transaction_type, amount FROM {t} "
            f"WHERE individual_id=? ORDER BY date ASC, id ASC", (member_id,))

    def month_rows(self, table, member_id, start_date, end_date):
        """One member's rows within a window, identity and amount only.

        Narrow on purpose: this answers "is this cell still what it was when I
        read it?", so it reads the fields an edit could have moved and no more.
        """
        t = self._table(table)
        return self._rows(
            f"SELECT id, amount, date, transaction_type FROM {t} "
            f"WHERE individual_id=? AND date >= ? AND date <= ?",
            (member_id, start_date, end_date))

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

    # -- running balance: one implementation ------------------------------ #

    def _ordered_rows(self, table, member_id):
        """A member's rows in the order a statement shows them."""
        t = self._table(table)
        return self._rows(
            f"SELECT id, transaction_type, amount FROM {t} "
            f"WHERE individual_id=? ORDER BY date ASC, id ASC", (member_id,))

    def recalculate(self, table, member_id, cursor=None):
        """Rewrite the stored running balance for one member's fund history.

        Ordered by ``(date, id)`` — the order a statement displays — with
        ``Withdrawal`` decreasing and everything else increasing, rounded to
        2dp at each step so float drift cannot accumulate.

        This replaced three implementations that disagreed (finding D1): one
        ordered by ``id`` alone, and one that treated any non-``Deposit`` row as
        a decrease. Ordering by insertion id produces a running balance that
        does not match the order the rows are displayed in, which is what
        corrupted two members' balances on the production journal.

        ``cursor`` lets the import path fold the writes into its own
        transaction.
        """
        running = 0.0
        updates = []
        for row_id, ttype, amount in self._ordered_rows(table, member_id):
            running = round(running - amount if ttype == "Withdrawal" else running + amount, 2)
            updates.append((running, row_id))
        if updates:
            t = self._table(table)
            (cursor or self.conn).executemany(
                f"UPDATE {t} SET balance=? WHERE id=?", updates)

    # -- benevolent enrolment (1:0..1 per member) ------------------------- #

    def get_enrolment(self, member_id):
        return self._dict(
            "SELECT id, individual_id, monthly_amount, start_date, next_due_date, active "
            "FROM benevolent_accounts WHERE individual_id=?", (member_id,))

    def all_enrolments(self):
        """Every enrolment at once, for a screen showing all members."""
        return self._dicts(
            "SELECT individual_id, monthly_amount, start_date, next_due_date, active "
            "FROM benevolent_accounts")

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
