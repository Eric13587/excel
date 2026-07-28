"""The ``ledger`` table — the loan subledger, and the journal's source of truth.

Everything else about a loan is derived from these rows: ``loans.balance``,
``loans.interest_balance``, ``individuals.default_deduction`` and the entire
double-entry general ledger are projections that ``BalanceRecalculator`` and
``GLService`` rebuild by replaying them.

Two column names are historical and load-bearing:

* ``loan_id`` holds the loan **reference string** (``L-001``), not ``loans.id``,
  so it is only meaningful together with ``individual_id``. Every method here
  that takes a ref also takes the member id.
* ``is_edited`` is an edit *lock*: 1 means a human set the amount and the
  recalculator must not overwrite it. :meth:`update_core` still writes the
  payment **amount** into it rather than 1 — the "hysteresis fix" that preserves
  the user's intended figure — and ``create_tables`` normalises anything outside
  0/1 back to 1 on the next open. Preserved verbatim; the domain is closed
  properly in Phase 4.

Repositories do not commit; the caller owns the transaction.
"""
import logging

import pandas as pd

from src.db.base import Repository

logger = logging.getLogger(__name__)

#: Columns accepted by :meth:`add` and :meth:`add_many`, in statement order.
INSERT_COLUMNS = (
    "individual_id", "date", "event_type", "loan_id", "added", "deducted", "balance",
    "notes", "installment_amount", "interest_amount", "batch_id",
    "principal_balance", "interest_balance", "principal_portion", "interest_portion",
    "previous_state",
)

#: Columns restored verbatim by an undo, including the original row id.
RESTORE_COLUMNS = (
    "id", "individual_id", "date", "event_type", "added", "deducted", "balance",
    "loan_id", "notes", "interest_amount", "principal_balance", "interest_balance",
    "principal_portion", "interest_portion", "is_edited",
)


class LedgerRepository(Repository):
    """Every statement that touches ``ledger``."""

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def for_member(self, member_id, start_date=None, end_date=None):
        """A member's full loan history as a DataFrame, ordered by (date, id)."""
        sql = "SELECT * FROM ledger WHERE individual_id = ?"
        params = [member_id]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        return pd.read_sql_query(sql + " ORDER BY date, id", self.conn, params=tuple(params))

    def get(self, row_id):
        return self._dict("SELECT * FROM ledger WHERE id=?", (row_id,))

    def count_events(self, member_id, loan_ref, event_types, start_date=None, end_date=None):
        """Rows of the given event types for one member's loan, optionally dated."""
        placeholders = ",".join("?" * len(event_types))
        sql = (f"SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? "
               f"AND event_type IN ({placeholders})")
        params = [member_id, loan_ref, *event_types]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        return self._scalar(sql, tuple(params), 0)

    def count_edited(self, member_id, loan_ref, event_type, flag=None):
        """Rows carrying an edit lock. ``flag=None`` means "any nonzero", which
        is what the heal probe needs: legacy rows store an amount, not 1."""
        sql = ("SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=? "
               "AND event_type=? AND is_edited")
        sql += "=?" if flag is not None else "<>0"
        params = [member_id, loan_ref, event_type] + ([flag] if flag is not None else [])
        return self._scalar(sql, tuple(params), 0)

    def max_date(self, member_id, loan_ref, event_type):
        return self._scalar(
            "SELECT MAX(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=?",
            (member_id, loan_ref, event_type))

    def min_date(self, member_id, loan_ref, event_type):
        return self._scalar(
            "SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=?",
            (member_id, loan_ref, event_type))

    def first_note(self, member_id, loan_ref, event_type):
        """The earliest row's note — the loan-heal tool parses the issue note
        for principal, interest and duration, which live nowhere else."""
        return self._scalar(
            "SELECT notes FROM ledger WHERE individual_id=? AND loan_id=? AND event_type=? "
            "ORDER BY date, id LIMIT 1", (member_id, loan_ref, event_type))

    def members_in_batch(self, batch_id):
        return [r[0] for r in
                self._rows("SELECT DISTINCT individual_id FROM ledger WHERE batch_id=?", (batch_id,))]

    def loans_in_batch(self, batch_id):
        """(loan_ref, member_id) pairs touched by a mass run."""
        return self._rows(
            "SELECT DISTINCT loan_id, individual_id FROM ledger WHERE batch_id=?", (batch_id,))

    def earliest_date_for_members(self, member_ids):
        """Earliest activity across ledger and savings for one or more members.

        Covers only those two tables, so a member whose entire history is
        Christmas or benevolent contributions has no earliest date and dated
        reports silently start after their data (finding D7). Preserved as-is.
        """
        if not member_ids:
            return None
        placeholders = ",".join("?" * len(member_ids))
        params = list(member_ids) * 2
        return self._scalar(
            f"SELECT MIN(date) FROM ("
            f"  SELECT MIN(date) AS date FROM ledger  WHERE individual_id IN ({placeholders})"
            f"  UNION ALL"
            f"  SELECT MIN(date) AS date FROM savings WHERE individual_id IN ({placeholders}))",
            tuple(params))

    def earliest_date_overall(self):
        """Earliest activity anywhere in the journal, across ledger and savings.

        Same two-table coverage — and therefore the same blind spot — as
        :meth:`earliest_date_for_members` (finding D7).
        """
        return self._scalar(
            "SELECT MIN(date) FROM ("
            "  SELECT date FROM ledger UNION ALL SELECT date FROM savings)")

    def gl_projection_rows(self):
        """Every row the GL projects from, in posting order.

        (id, date, event_type, added, deducted, principal_portion,
        interest_portion, interest_amount) — the columns GLService needs to
        decide which journal an event becomes.
        """
        return self._rows(
            "SELECT id, date, event_type, added, deducted, principal_portion, "
            "interest_portion, interest_amount FROM ledger ORDER BY date, id")

    def gl_projection_row(self, row_id):
        """The same projection columns for one row (the live write-path hook)."""
        return self._row(
            "SELECT date, event_type, added, deducted, principal_portion, "
            "interest_portion, interest_amount FROM ledger WHERE id=?", (row_id,))

    def max_id(self):
        """Highest row id, snapshotted before a bulk insert.

        ``lastrowid`` is unreliable after ``executemany``, so the post-write GL
        hook identifies its new rows by id range instead.
        """
        return self._scalar("SELECT COALESCE(MAX(id), 0) FROM ledger", (), 0)

    def ids_above(self, row_id):
        return [r[0] for r in self._rows("SELECT id FROM ledger WHERE id > ? ORDER BY id", (row_id,))]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    _INSERT_SQL = (
        "INSERT INTO ledger (" + ", ".join(INSERT_COLUMNS) + ") "
        "VALUES (" + ", ".join("?" * len(INSERT_COLUMNS)) + ")"
    )

    def add(self, values):
        """Insert one row from a tuple in :data:`INSERT_COLUMNS` order."""
        return self._execute(self._INSERT_SQL, values).lastrowid

    def add_many(self, rows):
        """Bulk insert. Returns nothing — see :meth:`max_id` for the id range."""
        self._executemany(self._INSERT_SQL, rows)

    def restore(self, values):
        """Re-insert a deleted row with its original id (undo)."""
        self._execute(
            "INSERT INTO ledger (" + ", ".join(RESTORE_COLUMNS) + ") "
            "VALUES (" + ", ".join("?" * len(RESTORE_COLUMNS)) + ")", values)

    def update_core(self, row_id, date, added, deducted, notes,
                    principal_portion=None, interest_portion=None,
                    mark_edited=False, interest_amount=None):
        """Edit a transaction. Optional columns are written only when supplied.

        ``mark_edited`` stores the payment *amount* in ``is_edited`` rather than
        1, to preserve the figure the user intended across recalculation. The
        recalculator treats any nonzero as locked, and create_tables normalises
        the column back to 0/1 on the next open. Preserved verbatim.
        """
        set_clauses = ["date=?", "added=?", "deducted=?", "notes=?"]
        params = [date, added, deducted, notes]
        if principal_portion is not None and interest_portion is not None:
            set_clauses += ["principal_portion=?", "interest_portion=?"]
            params += [principal_portion, interest_portion]
        if mark_edited:
            set_clauses.append("is_edited=?")
            params.append(deducted if deducted > 1 else 1)
        if interest_amount is not None:
            set_clauses.append("interest_amount=?")
            params.append(interest_amount)
        self._execute(f"UPDATE ledger SET {', '.join(set_clauses)} WHERE id=?",
                      (*params, int(row_id)))

    def set_balance(self, row_id, balance):
        self._execute("UPDATE ledger SET balance=? WHERE id=?", (balance, row_id))

    def set_running_balances(self, row_id, balance, principal_balance, interest_balance,
                             gross_balance=0):
        self._execute(
            "UPDATE ledger SET balance=?, principal_balance=?, interest_balance=?, "
            "gross_balance=? WHERE id=?",
            (balance, principal_balance, interest_balance, gross_balance, row_id))

    def set_component_balances(self, row_id, principal_balance, interest_balance):
        self._execute(
            "UPDATE ledger SET principal_balance=?, interest_balance=? WHERE id=?",
            (principal_balance, interest_balance, row_id))

    def set_portions(self, row_id, principal_portion, interest_portion):
        self._execute(
            "UPDATE ledger SET principal_portion=?, interest_portion=? WHERE id=?",
            (principal_portion, interest_portion, row_id))

    def set_accrual(self, row_id, added, interest_amount):
        self._execute("UPDATE ledger SET added=?, interest_amount=? WHERE id=?",
                      (added, interest_amount, row_id))

    def set_deducted(self, row_id, deducted):
        self._execute("UPDATE ledger SET deducted=? WHERE id=?", (deducted, row_id))

    def set_interest_amount(self, row_id, interest_amount):
        self._execute("UPDATE ledger SET interest_amount=? WHERE id=?",
                      (interest_amount, row_id))

    def set_date(self, row_id, date):
        self._execute("UPDATE ledger SET date=? WHERE id=?", (date, row_id))

    def delete(self, row_id):
        self._execute("DELETE FROM ledger WHERE id=?", (row_id,))

    def delete_batch(self, batch_id):
        self._execute("DELETE FROM ledger WHERE batch_id=?", (batch_id,))

    def delete_events(self, member_id, loan_ref, event_types):
        placeholders = ",".join("?" * len(event_types))
        self._execute(
            f"DELETE FROM ledger WHERE individual_id=? AND loan_id=? "
            f"AND event_type IN ({placeholders})",
            (member_id, loan_ref, *event_types))

    # ------------------------------------------------------------------ #
    # The interest edit-lock (finding D4)
    # ------------------------------------------------------------------ #

    def set_accrual_from_date(self, member_id, loan_ref, from_date, amount):
        """Rewrite a loan's accruals from a date onward and lock them.

        Used when an interest rate changes mid-loan: concurrent and future
        accruals move to the new rate immediately, and the lock stops the
        recalculator undoing it. Inclusive of ``from_date``, so a same-day
        accrual is caught.
        """
        self._execute(
            "UPDATE ledger SET added=?, is_edited=1 WHERE individual_id=? AND loan_id=? "
            "AND event_type='Interest Earned' AND date >= ?",
            (amount, member_id, loan_ref, from_date))

    def set_interest_lock(self, member_id, loan_ref, locked, from_date=None,
                          inclusive=False, value=None):
        """Set or clear the edit lock on a loan's ``Interest Earned`` rows.

        The single owner of what used to be three statements with three
        different scopes:

        * ``unlock_future_interest`` cleared rows strictly **after** a date;
        * the loan-heal tool cleared **every** accrual, unbounded;
        * an interest-rate edit **set** the lock from a date **inclusive**.

        Callers still choose their own scope, but there is now one statement to
        reason about. An unbounded clear discards past manual corrections, so
        it is only safe on loans with no legitimate interest edits — which is
        why the heal tool checks that first.
        """
        flag = (value if value is not None else (1 if locked else 0))
        sql = ("UPDATE ledger SET is_edited=? WHERE individual_id=? AND loan_id=? "
               "AND event_type='Interest Earned'")
        params = [flag, member_id, loan_ref]
        if from_date is not None:
            sql += " AND date >= ?" if inclusive else " AND date > ?"
            params.append(from_date)
        self._execute(sql, tuple(params))
