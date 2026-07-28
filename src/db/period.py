"""The fiscal-period register (``fiscal_periods``, migration 008).

Periods are declared rather than derived. ``fy_start_month`` decides where the
*suggested* boundaries fall, but once a period is closed its dates are a fact
about which figures were published, and changing a setting must not redraw
them retroactively.

Repositories do not commit; the caller owns the transaction.
"""
import logging

from src.db.base import Repository

logger = logging.getLogger(__name__)

OPEN, CLOSED = "open", "closed"


class PeriodRepository(Repository):
    """Every statement that touches ``fiscal_periods``."""

    _COLUMNS = ("id, label, start_date, end_date, status, closed_at, closed_by, note")

    def all(self):
        """Every period, earliest first."""
        return self._dicts(f"SELECT {self._COLUMNS} FROM fiscal_periods "
                           f"ORDER BY start_date")

    def get(self, period_id):
        return self._dict(f"SELECT {self._COLUMNS} FROM fiscal_periods WHERE id=?",
                          (period_id,))

    def by_label(self, label):
        return self._dict(f"SELECT {self._COLUMNS} FROM fiscal_periods WHERE label=?",
                          (label,))

    def covering(self, date):
        """The period a date falls in, or None.

        Non-overlap is enforced by trigger, so at most one row can match.
        """
        return self._dict(
            f"SELECT {self._COLUMNS} FROM fiscal_periods "
            f"WHERE ? BETWEEN start_date AND end_date", (date,))

    def closed_covering(self, date):
        """The *closed* period a date falls in, or None — the lock question."""
        row = self.covering(date)
        return row if row and row["status"] == CLOSED else None

    def add(self, label, start_date, end_date, note=None):
        cur = self._execute(
            "INSERT INTO fiscal_periods (label, start_date, end_date, status, note) "
            "VALUES (?, ?, ?, 'open', ?)", (label, start_date, end_date, note))
        return cur.lastrowid

    def set_status(self, period_id, status, closed_at=None, closed_by=None):
        self._execute(
            "UPDATE fiscal_periods SET status=?, closed_at=?, closed_by=? WHERE id=?",
            (status, closed_at, closed_by, period_id))

    def set_note(self, period_id, note):
        self._execute("UPDATE fiscal_periods SET note=? WHERE id=?", (note, period_id))

    def delete(self, period_id):
        self._execute("DELETE FROM fiscal_periods WHERE id=?", (period_id,))

    def latest_closed_end(self):
        """The end date of the most recent closed period, or None.

        Everything on or before this is settled, which is what a report footer
        or a cut-off warning needs to know.
        """
        return self._scalar(
            "SELECT MAX(end_date) FROM fiscal_periods WHERE status='closed'")

    def counts(self):
        """``{'open': n, 'closed': n}`` — a cheap summary for the integrity report."""
        rows = dict(self._rows(
            "SELECT status, COUNT(*) FROM fiscal_periods GROUP BY status"))
        return {OPEN: rows.get(OPEN, 0), CLOSED: rows.get(CLOSED, 0)}


class SnapshotRepository(Repository):
    """Period-end account snapshots (``account_snapshots``, migration 010).

    A snapshot holds *cumulative* totals from the beginning of the journal to a
    closed period's end date. Only closed periods are snapshotted, so nothing
    here ever needs invalidating — see the migration for why that distinction
    is what makes this a record rather than a cache.
    """

    def write(self, period_id, as_of, rows):
        """Replace this period's snapshot.

        ``rows`` is an iterable of ``(account_code, debit_total, credit_total)``.
        Replacing rather than inserting means re-snapshotting a period is
        idempotent.
        """
        self._execute("DELETE FROM account_snapshots WHERE period_id=?", (period_id,))
        self._executemany(
            "INSERT INTO account_snapshots (period_id, account_code, as_of, "
            "debit_total, credit_total, closing_balance) VALUES (?, ?, ?, ?, ?, ?)",
            [(period_id, code, as_of, round(d, 2), round(c, 2), round(d - c, 2))
             for code, d, c in rows])

    def latest_at_or_before(self, as_of):
        """The most recent snapshot date at or before ``as_of``, or None.

        This is the baseline a reader starts from; everything after it is
        summed from journal_lines.
        """
        return self._scalar(
            "SELECT MAX(as_of) FROM account_snapshots WHERE as_of <= ?", (as_of,))

    def balances_at(self, as_of):
        """``{account_code: (debit_total, credit_total)}`` for a snapshot date."""
        return {code: (d, c) for code, d, c in self._rows(
            "SELECT account_code, debit_total, credit_total FROM account_snapshots "
            "WHERE as_of = ?", (as_of,))}

    def balance_at(self, account_code, as_of):
        """One account's cumulative ``(debit, credit)`` at a snapshot date."""
        return self._row(
            "SELECT debit_total, credit_total FROM account_snapshots "
            "WHERE as_of = ? AND account_code = ?", (as_of, account_code))

    def discard_from(self, as_of):
        """Drop every snapshot dated on or after ``as_of``. Returns the count.

        Reopening a period invalidates its own snapshot *and every later one*,
        because the totals are cumulative: a change inside the reopened period
        moves every figure that includes it.
        """
        return self._execute(
            "DELETE FROM account_snapshots WHERE as_of >= ?", (as_of,)).rowcount

    def dates(self):
        """Every snapshot date held, earliest first."""
        return [r[0] for r in self._rows(
            "SELECT DISTINCT as_of FROM account_snapshots ORDER BY as_of")]

    def count(self):
        return self._scalar("SELECT COUNT(*) FROM account_snapshots", (), 0)
