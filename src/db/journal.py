"""The double-entry general ledger: ``chart_of_accounts``, ``journal_entries``,
``journal_lines``, and the frozen legacy ``general_ledger``.

``GLService`` keeps the accounting logic — posting rules, the balance
assertion, statement assembly, the cash-flow categorisation — and this holds
the SQL. The split is clean because the two were already separable: every
posting helper builds a list of ``{account, debit|credit}`` dicts and hands it
to one insert.

The GL is a *projection*. Entries whose ``source`` is one of the auto set are
derived from the subledgers, which is why ``source_ref`` links each one back to
its row — and, since migration 001, why ``(source, source_ref)`` is unique.
Since migration 005 the projection is maintained from the ``gl_outbox`` change
queue rather than wiped and re-derived on every sync; the wipe remains as
``rebuild_auto_journals``, the baseline the queue is measured against.

Repositories do not commit; the caller owns the transaction. ``GLService``
batches thousands of posts into one transaction, so this matters.
"""
import logging
import sqlite3

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

    def entry_origin(self, entry_id):
        """(source, status) — what kind of entry this is and where it stands.

        Both decide whether it may be reversed, so they are read together.
        """
        return self._row("SELECT source, status FROM journal_entries WHERE id=?",
                         (entry_id,)) or (None, None)

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

    # SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds, so
    # any IN (...) over a caller-supplied list is chunked below that.
    _PARAM_CHUNK = 400

    def delete_projected(self, source_refs, sources):
        """Delete the projected entries for specific source rows.

        The single-row counterpart of :meth:`delete_by_sources`: it is how a
        re-projection clears the previous entry before deriving the new one,
        and how an undone subledger row has its entry withdrawn.

        Matching is by ``source_ref`` alone within ``sources``, deliberately
        *not* by ``(source, source_ref)``. A ledger row's source is decided by
        its event type, so editing ``Repayment`` into ``Interest Earned`` moves
        the entry between sources; a lookup pinned to the old source would miss
        it and the re-post would leave a duplicate behind.

        Returns the number of entries removed.
        """
        refs = list(source_refs)
        if not refs:
            return 0
        src_ph = ",".join("?" * len(sources))
        removed = 0
        for start in range(0, len(refs), self._PARAM_CHUNK):
            chunk = refs[start:start + self._PARAM_CHUNK]
            ref_ph = ",".join("?" * len(chunk))
            where = (f"source_ref IN ({ref_ph}) AND source IN ({src_ph})")
            params = tuple(chunk) + tuple(sources)
            self._execute(
                f"DELETE FROM journal_lines WHERE entry_id IN "
                f"(SELECT id FROM journal_entries WHERE {where})", params)
            removed += self._execute(
                f"DELETE FROM journal_entries WHERE {where}", params).rowcount
        return removed

    # ------------------------------------------------------------------ #
    # Projection change queue (migration 005)
    # ------------------------------------------------------------------ #

    def outbox_depth(self):
        """How many queued changes are waiting to be projected."""
        try:
            return self._scalar("SELECT COUNT(*) FROM gl_outbox", (), 0)
        except sqlite3.OperationalError:
            return 0        # pre-005 journal: the queue does not exist yet

    def outbox_batch(self):
        """The queued changes as ``(high_water_id, {table: [row_id, ...]})``.

        The high-water id is returned so the drain can delete exactly what it
        read: anything queued while the projection runs keeps its place in the
        queue instead of being discarded unprojected.
        """
        try:
            rows = self._rows(
                "SELECT id, source_table, row_id FROM gl_outbox ORDER BY id")
        except sqlite3.OperationalError:
            return (0, {})
        high = 0
        batch = {}
        for entry_id, table, row_id in rows:
            high = max(high, entry_id)
            seen = batch.setdefault(table, [])
            if row_id not in seen:
                seen.append(row_id)     # a row touched twice needs projecting once
        return (high, batch)

    def outbox_clear(self, up_to_id=None):
        """Drop the drained changes, or the whole queue when ``up_to_id`` is None."""
        try:
            if up_to_id is None:
                self._execute("DELETE FROM gl_outbox")
            else:
                self._execute("DELETE FROM gl_outbox WHERE id <= ?", (up_to_id,))
        except sqlite3.OperationalError:
            pass

    def orphaned_projections(self):
        """Projected entries whose subledger row no longer exists.

        Drift in the dangerous direction: a journal on the books with nothing
        behind it. The opposite direction — a subledger row with no journal —
        is what the control-account reconciliation in :mod:`src.integrity`
        already measures, and measuring it here would mean duplicating the
        event vocabulary that decides which rows post at all.
        """
        return self._rows(
            "SELECT e.id, e.source, e.source_ref FROM journal_entries e "
            "WHERE e.source_ref LIKE 'ledger:%' "
            "  AND NOT EXISTS (SELECT 1 FROM ledger l "
            "                   WHERE l.id = CAST(substr(e.source_ref, 8) AS INTEGER)) "
            "UNION ALL "
            "SELECT e.id, e.source, e.source_ref FROM journal_entries e "
            "WHERE e.source_ref LIKE 'savings:%' "
            "  AND NOT EXISTS (SELECT 1 FROM savings s "
            "                   WHERE s.id = CAST(substr(e.source_ref, 9) AS INTEGER)) "
            "UNION ALL "
            "SELECT e.id, e.source, e.source_ref FROM journal_entries e "
            "WHERE e.source_ref LIKE 'christmas:%' "
            "  AND NOT EXISTS (SELECT 1 FROM christmas_savings c "
            "                   WHERE c.id = CAST(substr(e.source_ref, 11) AS INTEGER)) "
            "UNION ALL "
            "SELECT e.id, e.source, e.source_ref FROM journal_entries e "
            "WHERE e.source_ref LIKE 'benevolent:%' "
            "  AND NOT EXISTS (SELECT 1 FROM benevolent_ledger b "
            "                   WHERE b.id = CAST(substr(e.source_ref, 12) AS INTEGER))")

    # ------------------------------------------------------------------ #
    # Balances and statements
    # ------------------------------------------------------------------ #

    def account_net(self, account_code, as_of=None):
        """SUM(debit) - SUM(credit) for one account, optionally as at a date.

        The date is a WHERE predicate, so it genuinely filters.
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
        """(code, name, type, net) for every account, optionally as at a date.

        The date filter lives in a subquery rather than on the outer join. The
        previous form appended ``AND e.entry_date <= ?`` to a second
        ``LEFT JOIN``'s ``ON`` clause, which does not exclude anything: a line
        whose entry falls after the date simply failed to match the header
        while the line itself survived the outer join and still contributed to
        the sums, so a dated trial balance was an all-time trial balance.
        Filtering inside the subquery drops the line, and the outer ``LEFT
        JOIN`` still lists every account.
        """
        if as_of:
            sql = ("SELECT a.code, a.name, a.type, "
                   "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
                   "FROM chart_of_accounts a "
                   "LEFT JOIN (SELECT jl.account_code, jl.debit, jl.credit "
                   "           FROM journal_lines jl "
                   "           JOIN journal_entries je ON jl.entry_id = je.id "
                   "           WHERE je.entry_date <= ?) l ON l.account_code = a.code")
            params = (as_of,)
        else:
            sql = ("SELECT a.code, a.name, a.type, "
                   "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
                   "FROM chart_of_accounts a "
                   "LEFT JOIN journal_lines l ON l.account_code = a.code")
            params = ()
        return self._rows(sql + " GROUP BY a.code, a.name, a.type ORDER BY a.code", params)

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
