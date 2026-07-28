"""The double-entry general ledger: ``chart_of_accounts``, ``journal_entries``,
``journal_lines``, and the frozen legacy ``general_ledger``.

``GLService`` keeps the accounting logic — posting rules, the balance
assertion, statement assembly, the cash-flow categorisation — and this holds
the SQL. The split is clean because the two were already separable: every
posting helper builds a list of ``{account, debit|credit}`` dicts and hands it
to one insert.

The GL is a *projection*. Entries whose ``source`` is one of the auto set are
deleted and re-derived from the subledgers on every sync, which is why
``(source, source_ref)`` is an idempotency key — and, since migration 001, a
unique index.

Repositories do not commit; the caller owns the transaction. ``GLService``
batches thousands of posts into one transaction, so this matters.
"""
import logging

from src.db.base import Repository

logger = logging.getLogger(__name__)


class JournalRepository(Repository):
    """Every statement that touches the general-ledger tables."""

    # ------------------------------------------------------------------ #
    # Chart of accounts
    # ------------------------------------------------------------------ #

    def account_codes(self):
        """Every account code, for validating a posting."""
        return {r[0] for r in self._rows("SELECT code FROM chart_of_accounts")}

    def accounts(self, active_only=True):
        sql = ("SELECT code, name, type, normal_balance, is_active, description "
               "FROM chart_of_accounts")
        if active_only:
            sql += " WHERE is_active = 1"
        return self._dicts(sql + " ORDER BY code")

    def normal_balance(self, account_code):
        """'debit' or 'credit' — the side that increases the account."""
        return self._scalar("SELECT normal_balance FROM chart_of_accounts WHERE code=?",
                            (account_code,))

    # ------------------------------------------------------------------ #
    # Posting
    # ------------------------------------------------------------------ #

    def add_entry(self, entry_date, memo, source, source_ref, created_at, created_by):
        cur = self._execute(
            "INSERT INTO journal_entries (entry_date, memo, source, source_ref, status, "
            "created_at, created_by) VALUES (?, ?, ?, ?, 'posted', ?, ?)",
            (entry_date, memo, source, source_ref, created_at, created_by))
        return cur.lastrowid

    def add_lines(self, rows):
        """rows: iterable of (entry_id, account_code, debit, credit, line_memo)."""
        self._executemany(
            "INSERT INTO journal_lines (entry_id, account_code, debit, credit, line_memo) "
            "VALUES (?, ?, ?, ?, ?)", rows)

    def find_by_source_ref(self, source, source_ref):
        """The idempotency lookup: has this subledger row already been posted?"""
        return self._scalar(
            "SELECT id FROM journal_entries WHERE source=? AND source_ref=? LIMIT 1",
            (source, source_ref))

    def entry_count(self):
        return self._scalar("SELECT COUNT(*) FROM journal_entries", (), 0)

    def entry_header(self, entry_id):
        """(entry_date, memo) or None."""
        return self._row("SELECT entry_date, memo FROM journal_entries WHERE id=?", (entry_id,))

    def entry_lines(self, entry_id):
        """(account_code, debit, credit) tuples, for building a reversal."""
        return self._rows(
            "SELECT account_code, debit, credit FROM journal_lines WHERE entry_id=?", (entry_id,))

    def mark_reversed(self, entry_id):
        self._execute("UPDATE journal_entries SET status='reversed' WHERE id=?", (entry_id,))

    def mark_reversal(self, reversal_id, reverses_entry_id):
        self._execute("UPDATE journal_entries SET status='reversal', reversal_of=? WHERE id=?",
                      (reverses_entry_id, reversal_id))

    def delete_by_sources(self, sources):
        """Drop the projected slice so it can be re-derived. Lines first."""
        placeholders = ",".join("?" * len(sources))
        self._execute(
            f"DELETE FROM journal_lines WHERE entry_id IN "
            f"(SELECT id FROM journal_entries WHERE source IN ({placeholders}))", tuple(sources))
        self._execute(
            f"DELETE FROM journal_entries WHERE source IN ({placeholders})", tuple(sources))

    # ------------------------------------------------------------------ #
    # Balances and statements
    # ------------------------------------------------------------------ #

    def account_net(self, account_code, as_of=None):
        """SUM(debit) - SUM(credit) for one account, optionally as at a date.

        The date is a WHERE predicate, so it genuinely filters — unlike
        :meth:`trial_balance_rows`; see the note there.
        """
        sql = ("SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) "
               "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
               "WHERE l.account_code = ?")
        params = [account_code]
        if as_of:
            sql += " AND e.entry_date <= ?"
            params.append(as_of)
        return self._scalar(sql, tuple(params), 0.0) or 0.0

    def trial_balance_rows(self, as_of=None):
        """(code, name, type, net) for every account.

        .. warning::

           ``as_of`` does **not** filter. The predicate lands on the second
           ``LEFT JOIN``'s ``ON`` clause, so a line whose entry falls after the
           date simply fails to match ``e`` while the line itself survives the
           outer join and still contributes to the sums. A dated trial balance
           is therefore an all-time trial balance.

           Preserved verbatim here because Phase 2 must not change behaviour.
           The fix is to move the predicate into a ``WHERE`` with a NULL guard
           (``AND (e.entry_date IS NULL OR e.entry_date <= ?)`` will not do it
           either — the line must be excluded, so the join has to become an
           inner join or the filter has to sit on a subquery). Tracked in
           docs/db/phase-2-repository-layer.md; ``views/treasury.py`` passes a
           date, so the Treasury trial balance is affected.
        """
        sql = ("SELECT a.code, a.name, a.type, "
               "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
               "FROM chart_of_accounts a "
               "LEFT JOIN journal_lines l ON l.account_code = a.code "
               "LEFT JOIN journal_entries e ON l.entry_id = e.id")
        params = []
        if as_of:
            sql += " AND e.entry_date <= ?"
            params.append(as_of)
        return self._rows(sql + " GROUP BY a.code, a.name, a.type ORDER BY a.code", tuple(params))

    def statement_rows(self, account_type, start_date=None, end_date=None):
        """(code, name, net) for one account type over a date range.

        Here the dates are WHERE predicates, so they do filter — which is why
        the income statement and balance sheet respect their period while the
        trial balance does not.
        """
        sql = ("SELECT a.code, a.name, "
               "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
               "FROM chart_of_accounts a "
               "LEFT JOIN journal_lines l ON l.account_code = a.code "
               "LEFT JOIN journal_entries e ON l.entry_id = e.id "
               "WHERE a.type = ?")
        params = [account_type]
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        return self._rows(sql + " GROUP BY a.code, a.name ORDER BY a.code", tuple(params))

    def cash_before(self, cash_code, start_date):
        """Opening cash: net movement on the cash account before a date."""
        return self._scalar(
            "SELECT COALESCE(SUM(l.debit - l.credit),0) FROM journal_lines l "
            "JOIN journal_entries e ON l.entry_id = e.id "
            "WHERE l.account_code = ? AND e.entry_date < ?",
            (cash_code, start_date), 0.0) or 0.0

    def counterpart_lines(self, cash_code, start_date=None, end_date=None):
        """Every non-cash line in the period, for direct-method cash flow.

        Each cash movement is attributed to the counterpart leg of its journal,
        which is what makes the sections sum to the change in cash.
        """
        sql = ("SELECT l.account_code, a.name, l.debit, l.credit "
               "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
               "JOIN chart_of_accounts a ON a.code = l.account_code "
               "WHERE l.account_code != ?")
        params = [cash_code]
        if start_date:
            sql += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND e.entry_date <= ?"
            params.append(end_date)
        return self._rows(sql, tuple(params))

    # ------------------------------------------------------------------ #
    # Drill-down (Treasury UI)
    # ------------------------------------------------------------------ #

    def recent_entries(self, limit=200):
        """Recent entries with their total amount (sum of debits)."""
        return self._dicts(
            "SELECT e.id, e.entry_date, e.memo, e.source, e.status, "
            "COALESCE(SUM(l.debit), 0) AS amount "
            "FROM journal_entries e LEFT JOIN journal_lines l ON l.entry_id = e.id "
            "GROUP BY e.id ORDER BY e.entry_date DESC, e.id DESC LIMIT ?", (limit,))

    def account_postings(self, account_code, as_of=None):
        """One account's postings in date order, for a running balance."""
        sql = ("SELECT e.id, e.entry_date, e.memo, e.source, l.debit, l.credit "
               "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
               "WHERE l.account_code = ?")
        params = [account_code]
        if as_of:
            sql += " AND e.entry_date <= ?"
            params.append(as_of)
        return self._rows(sql + " ORDER BY e.entry_date, e.id, l.id", tuple(params))

    # ------------------------------------------------------------------ #
    # Legacy single-entry treasury (frozen; migration source only)
    # ------------------------------------------------------------------ #

    def legacy_rows(self):
        return self._rows(
            "SELECT id, date, category, type, amount, notes FROM general_ledger ORDER BY date, id")
