"""Double-entry General Ledger service.

This is the accounting core: a balanced-journal posting engine on top of the
``chart_of_accounts`` / ``journal_entries`` / ``journal_lines`` tables. Every
entry that reaches the ledger satisfies ``SUM(debit) == SUM(credit)``, so the
books balance by construction.

Member activity (loan disbursements, repayments, interest accrual, savings) is
posted here through the ``post_*`` helpers, which means the existing ``ledger``
and ``savings`` tables become subledgers that reconcile to GL control accounts
rather than three disconnected sources of truth.
"""
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from src.exceptions import (PERIOD_LOCK_MARKER, ImmutableEntryError,
                            PeriodClosedError, UnbalancedJournalError,
                            UnknownAccountError)
from src.config import (
    GL_CASH, GL_LOANS_RECEIVABLE, GL_INTEREST_RECEIVABLE, GL_MEMBER_DEPOSITS,
    GL_LOAN_INTEREST_INCOME, GL_OPENING_EQUITY, GL_FEES_INCOME,
    GL_BANK_INTEREST_INCOME, GL_OTHER_INCOME, GL_OTHER_EXPENSE,
    GL_ALLOWANCE_LOAN_LOSS, GL_SHARE_CAPITAL, GL_RETAINED_EARNINGS,
    GL_CHRISTMAS_SAVINGS, GL_BENEVOLENT_FUND,
)

logger = logging.getLogger(__name__)

# Money is tracked to whole cents; debits and credits must agree within this
# tolerance for an entry to be considered balanced.
_BALANCE_TOLERANCE = 0.005


class GLService:
    """Posting engine and queries for the double-entry general ledger."""

    def __init__(self, db_manager):
        self.db = db_manager
        self._account_codes = None  # lazily-loaded cache of valid codes
        self._bulk_depth = 0        # >0 while inside a batched transaction

    @contextmanager
    def _bulk(self):
        """Batch many posts into one transaction (one commit, not one per row).

        Committing per journal means an fsync per row; for the ~thousands of
        projected member journals that is the difference between seconds and a
        UI freeze. Inside this context post_journal defers committing; the
        single commit happens when the outermost context exits.
        """
        self._bulk_depth += 1
        try:
            yield
        except Exception:
            self.db.conn.rollback()
            raise
        finally:
            self._bulk_depth -= 1
            if self._bulk_depth == 0:
                self.db.conn.commit()

    def _maybe_commit(self):
        if self._bulk_depth == 0:
            self.db.conn.commit()

    # ------------------------------------------------------------------ #
    # Account helpers
    # ------------------------------------------------------------------ #
    def _valid_codes(self):
        if self._account_codes is None:
            self._account_codes = self.db.journal.account_codes()
        return self._account_codes

    def get_accounts(self, active_only=True):
        """Return the chart of accounts as a list of dicts."""
        return self.db.journal.accounts(active_only)

    # ------------------------------------------------------------------ #
    # Posting
    # ------------------------------------------------------------------ #
    def post_journal(self, entry_date, lines, memo="", source="manual",
                     source_ref=None, created_by=None):
        """Post a balanced journal entry atomically.

        Args:
            entry_date: "YYYY-MM-DD".
            lines: list of dicts, each {'account': code, 'debit': x} or
                {'account': code, 'credit': y}. Amounts are rounded to 2dp.
            memo: entry-level description.
            source: origin tag (manual, repayment, savings, migration, ...).
            source_ref: idempotency key linking back to the originating row;
                if an entry with the same (source, source_ref) already exists,
                this is a no-op and the existing id is returned.
            created_by: optional actor.

        Returns:
            The journal_entries.id of the posted (or pre-existing) entry.

        Raises:
            UnbalancedJournalError: debits != credits.
            UnknownAccountError: a line references an unknown account code.
            ValueError: no lines supplied.
        """
        if not lines:
            raise ValueError("A journal entry needs at least one line.")

        # Idempotency: never post the same source row twice.
        if source_ref is not None:
            existing = self._find_entry(source, source_ref)
            if existing is not None:
                return existing

        valid = self._valid_codes()
        norm = []
        total_debit = 0.0
        total_credit = 0.0
        for ln in lines:
            code = ln.get('account')
            if code not in valid:
                raise UnknownAccountError(code)
            debit = round(float(ln.get('debit', 0) or 0), 2)
            credit = round(float(ln.get('credit', 0) or 0), 2)
            if debit < 0 or credit < 0:
                raise ValueError("Journal line amounts must be non-negative.")
            total_debit += debit
            total_credit += credit
            norm.append((code, debit, credit, ln.get('memo')))

        if abs(round(total_debit - total_credit, 2)) > _BALANCE_TOLERANCE:
            raise UnbalancedJournalError(total_debit, total_credit)

        try:
            entry_id = self.db.journal.add_entry(
                entry_date, memo, source, source_ref,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), created_by)
            self.db.journal.add_lines(
                [(entry_id, c, d, cr, lm) for (c, d, cr, lm) in norm])
        except sqlite3.IntegrityError as e:
            # Posting does not go through DatabaseManager.transaction, so the
            # period lock (migration 009) is translated here instead — same
            # contract, so a caller sees one exception type either way.
            if PERIOD_LOCK_MARKER in str(e):
                raise PeriodClosedError(entry_date, "a closed period",
                                        what="This journal entry") from e
            raise
        self._maybe_commit()
        return entry_id

    def reverse_entry(self, entry_id, reversal_date=None, memo=None):
        """Post a reversing entry (swap debits/credits) and flag the original.

        Corrections never edit or delete a posted entry; they post the inverse.
        Both entries remain in the ledger and net to zero.

        Two entries cannot be reversed, and the rule belongs here rather than in
        the dialog that happens to offer the button:

        * **a projected entry**, because the reversal is authored and survives
          while the original is re-derived on the next rebuild. The pair stops
          netting to zero and the correction silently doubles. Such an entry is
          corrected by editing the loan or savings row behind it, which
          re-projects through the change queue.
        * **an entry that is already reversed**, which would net the correction
          back to the original amount.
        """
        head = self.db.journal.entry_header(entry_id)
        if not head:
            raise ValueError(f"Journal entry {entry_id} not found.")
        source, status = self.db.journal.entry_origin(entry_id)
        if self.is_auto_source(source):
            raise ImmutableEntryError(
                entry_id, "it is projected from member activity — correct the "
                          "underlying loan or savings record instead")
        if status != "posted":
            raise ImmutableEntryError(entry_id, f"it is already '{status}'")
        orig_lines = self.db.journal.entry_lines(entry_id)

        rev_lines = [{'account': code, 'debit': credit, 'credit': debit}
                     for (code, debit, credit) in orig_lines]
        reversal_id = self.post_journal(
            reversal_date or datetime.now().strftime("%Y-%m-%d"),
            rev_lines,
            memo=memo or f"Reversal of entry #{entry_id}",
            source="reversal",
        )
        self.db.journal.mark_reversed(entry_id)
        self.db.journal.mark_reversal(reversal_id, entry_id)
        self.db.conn.commit()
        return reversal_id

    def _find_entry(self, source, source_ref):
        return self.db.journal.find_by_source_ref(source, source_ref)

    def _entry_count(self):
        return self.db.journal.entry_count()

    # ------------------------------------------------------------------ #
    # Period-end snapshots (migration 010)
    # ------------------------------------------------------------------ #
    def snapshot_period(self, period):
        """Record every account's cumulative position at a period's end date.

        Called when a period closes. What makes this a record rather than a
        cache is that a closed period cannot change — migration 009 refuses
        every write into one — so the figures stored here are settled by
        definition and never need invalidating.

        Returns the number of accounts written.
        """
        as_of = period["end_date"]
        rows = self.db.journal.cumulative_totals(as_of)
        with self._bulk():
            self.db.snapshots.write(period["id"], as_of, rows)
        return len(rows)

    def discard_snapshots_from(self, as_of):
        """Drop snapshots dated on or after ``as_of``. Returns the count.

        Reopening a period invalidates its own snapshot *and every later one*:
        the totals are cumulative, so a change inside the reopened period moves
        every figure that includes it.
        """
        with self._bulk():
            return self.db.snapshots.discard_from(as_of)

    def rebuild_snapshots(self):
        """Re-derive the snapshot of every closed period, earliest first.

        The counterpart of ``sync(full=True)``. Needed after a period is
        reopened and re-closed, since the later snapshots discarded at reopen
        are not restored by closing one period.
        """
        written = 0
        with self._bulk():
            self.db.snapshots.discard_from("0000-00-00")
            for period in self.db.periods.all():
                if period["status"] == "closed":
                    as_of = period["end_date"]
                    self.db.snapshots.write(
                        period["id"], as_of, self.db.journal.cumulative_totals(as_of))
                    written += 1
        return written

    def snapshot_discrepancies(self):
        """Snapshots that disagree with a fresh computation.

        The guarantee behind the read path. Returns
        ``[(as_of, account_code, stored, recomputed)]`` — empty when every
        snapshot still reflects the journal it was taken from.
        """
        out = []
        for as_of in self.db.snapshots.dates():
            stored = self.db.snapshots.balances_at(as_of)
            fresh = {code: (d, c)
                     for code, d, c in self.db.journal.cumulative_totals(as_of)}
            for code in set(stored) | set(fresh):
                s = stored.get(code, (0.0, 0.0))
                f = fresh.get(code, (0.0, 0.0))
                if abs(round((s[0] - s[1]) - (f[0] - f[1]), 2)) > _BALANCE_TOLERANCE:
                    out.append((as_of, code, round(s[0] - s[1], 2), round(f[0] - f[1], 2)))
        return out

    # ------------------------------------------------------------------ #
    # Balances & trial balance
    # ------------------------------------------------------------------ #
    def _trial_balance_source(self, as_of):
        """``(code, name, type, net)`` per account, via a snapshot when one covers
        ``as_of``. Falls back to the full aggregate otherwise."""
        baseline = self.db.snapshots.latest_at_or_before(as_of) if as_of else None
        if not baseline:
            return self.db.journal.trial_balance_rows(as_of)

        nets = {code: d - c
                for code, (d, c) in self.db.snapshots.balances_at(baseline).items()}
        if baseline < as_of:
            for code, debit, credit in self.db.journal.movements_between(baseline, as_of):
                nets[code] = nets.get(code, 0.0) + debit - credit
        return [(code, name, typ, nets.get(code, 0.0))
                for code, name, typ in self.db.journal.accounts_with_names()]

    def _net_via_snapshot(self, account_code, as_of):
        """``SUM(debit) - SUM(credit)`` for one account, using a snapshot baseline.

        Falls back to a full aggregate when no snapshot covers the date, which
        is every journal until a period is closed.
        """
        if not as_of:
            return None
        baseline = self.db.snapshots.latest_at_or_before(as_of)
        if not baseline:
            return None
        stored = self.db.snapshots.balance_at(account_code, baseline)
        net = (stored[0] - stored[1]) if stored else 0.0
        if baseline < as_of:
            net += self.db.journal.account_net_between(account_code, baseline, as_of)
        return net

    def get_account_balance(self, account_code, as_of=None):
        """Return an account's balance in its natural (positive) sense.

        Debit-normal accounts are positive when debit-heavy; credit-normal
        accounts are positive when credit-heavy.

        When a closed period's snapshot covers the date, the settled part is
        read rather than re-aggregated and only the movement since is summed.
        """
        debit_minus_credit = self._net_via_snapshot(account_code, as_of)
        if debit_minus_credit is None:
            debit_minus_credit = self.db.journal.account_net(account_code, as_of)
        normal = self.db.journal.normal_balance(account_code) or 'debit'
        signed = debit_minus_credit if normal == 'debit' else -debit_minus_credit
        return round(signed, 2)

    def get_trial_balance(self, as_of=None):
        """Return (rows, is_balanced).

        rows: per-account {code, name, type, debit, credit} where each account's
        net balance is shown in exactly one column. Across all accounts the debit
        and credit columns must be equal — that equality is the proof the ledger
        is internally consistent.
        """
        rows = []
        total_debit = 0.0
        total_credit = 0.0
        for code, name, typ, net in self._trial_balance_source(as_of):
            net = round(net or 0.0, 2)
            if abs(net) < 0.005:
                continue  # skip accounts with no balance
            debit = net if net > 0 else 0.0
            credit = -net if net < 0 else 0.0
            total_debit += debit
            total_credit += credit
            rows.append({'code': code, 'name': name, 'type': typ,
                         'debit': round(debit, 2), 'credit': round(credit, 2)})

        is_balanced = abs(round(total_debit - total_credit, 2)) <= _BALANCE_TOLERANCE
        return rows, is_balanced

    # ------------------------------------------------------------------ #
    # Financial statements (derived from the ledger, not hand-assembled)
    # ------------------------------------------------------------------ #
    def _statement_lines(self, acct_type, start_date, end_date, positive_side):
        """Per-account amounts for one account type over a date range.

        positive_side is the normal side of the section: 'debit' for
        assets/expenses, 'credit' for liabilities/equity/income. Amounts are
        returned positive when the account sits on its normal side. Accounts
        netting to zero in the range are omitted.
        """
        lines = []
        for code, name, net in self.db.journal.statement_rows(acct_type, start_date, end_date):
            net = round(net or 0.0, 2)
            amount = net if positive_side == 'debit' else -net
            amount = round(amount, 2)
            if abs(amount) < 0.005:
                continue
            lines.append({'code': code, 'name': name, 'amount': amount})
        return lines

    def get_income_statement(self, start_date=None, end_date=None):
        """Income statement for a period (activity within [start, end])."""
        revenue = self._statement_lines('Income', start_date, end_date, 'credit')
        expenses = self._statement_lines('Expense', start_date, end_date, 'debit')
        total_rev = round(sum(l['amount'] for l in revenue), 2)
        total_exp = round(sum(l['amount'] for l in expenses), 2)
        return {
            'period': (start_date, end_date),
            'revenue': revenue,
            'expenses': expenses,
            'total_revenue': total_rev,
            'total_expenses': total_exp,
            'net_surplus': round(total_rev - total_exp, 2),
        }

    def get_balance_sheet(self, as_of=None):
        """Balance sheet as of a date.

        Because every journal balances, Assets == Liabilities + Equity holds by
        construction; ``is_balanced`` is the proof. Until a period close exists,
        the cumulative net surplus is folded into equity as 'Current Surplus'.
        """
        assets = self._statement_lines('Asset', None, as_of, 'debit')
        liabilities = self._statement_lines('Liability', None, as_of, 'credit')
        equity = self._statement_lines('Equity', None, as_of, 'credit')

        total_assets = round(sum(l['amount'] for l in assets), 2)
        total_liabilities = round(sum(l['amount'] for l in liabilities), 2)
        equity_accounts_total = round(sum(l['amount'] for l in equity), 2)

        net_surplus = self.get_income_statement(None, as_of)['net_surplus']
        equity_lines = list(equity) + [
            {'code': '', 'name': 'Current Surplus / (Deficit)', 'amount': net_surplus}
        ]
        total_equity = round(equity_accounts_total + net_surplus, 2)
        total_liab_and_equity = round(total_liabilities + total_equity, 2)

        is_balanced = abs(round(total_assets - total_liab_and_equity, 2)) <= _BALANCE_TOLERANCE
        return {
            'as_of': as_of,
            'assets': assets,
            'liabilities': liabilities,
            'equity': equity_lines,
            'total_assets': total_assets,
            'total_liabilities': total_liabilities,
            'total_equity': total_equity,
            'total_liabilities_and_equity': total_liab_and_equity,
            'is_balanced': is_balanced,
        }

    # Cash-flow categorisation by the counterpart account.
    _LENDING_ACCOUNTS = {GL_LOANS_RECEIVABLE, GL_ALLOWANCE_LOAN_LOSS}
    _FINANCING_ACCOUNTS = {GL_MEMBER_DEPOSITS, GL_CHRISTMAS_SAVINGS, GL_BENEVOLENT_FUND,
                           "2100", GL_SHARE_CAPITAL, GL_RETAINED_EARNINGS, GL_OPENING_EQUITY}

    @classmethod
    def _cash_flow_category(cls, code):
        if code in cls._LENDING_ACCOUNTS:
            return 'Lending to members'
        if code in cls._FINANCING_ACCOUNTS:
            return 'Financing'
        return 'Operating'

    def get_cash_flow(self, start_date=None, end_date=None):
        """Direct-method cash flow that ties to the cash account exactly.

        Every cash movement is attributed to the counterpart leg of its journal
        (contribution = credit − debit of the non-cash line), grouped into
        Operating / Lending / Financing. Because entries balance, the section
        subtotals sum to the change in the cash account, so opening + net change
        == closing cash by construction.
        """
        opening = (round(self.db.journal.cash_before(GL_CASH, start_date), 2)
                   if start_date else 0.0)

        cats = {'Operating': {}, 'Lending to members': {}, 'Financing': {}}
        for code, name, debit, credit in self.db.journal.counterpart_lines(
                GL_CASH, start_date, end_date):
            contrib = round((credit or 0.0) - (debit or 0.0), 2)  # +ve = cash in
            if contrib == 0:
                continue
            bucket = cats[self._cash_flow_category(code)]
            bucket[name] = round(bucket.get(name, 0.0) + contrib, 2)

        sections = []
        net = 0.0
        for label in ('Operating', 'Lending to members', 'Financing'):
            lines = sorted(
                ({'name': n, 'amount': a} for n, a in cats[label].items() if abs(a) >= 0.005),
                key=lambda x: x['name'])
            subtotal = round(sum(l['amount'] for l in lines), 2)
            net = round(net + subtotal, 2)
            sections.append({'label': label, 'lines': lines, 'subtotal': subtotal})

        return {
            'period': (start_date, end_date),
            'sections': sections,
            'opening_cash': opening,
            'net_change': net,
            'closing_cash': round(opening + net, 2),
        }

    # ------------------------------------------------------------------ #
    # Journal & account drill-down (for the Treasury UI)
    # ------------------------------------------------------------------ #
    def get_journal_entries(self, limit=200):
        """Recent journal entries with their total amount (sum of debits)."""
        return self.db.journal.recent_entries(limit)

    def get_account_ledger(self, account_code, as_of=None):
        """One account's posting history with a running balance (natural sign)."""
        normal = self.db.journal.normal_balance(account_code) or 'debit'

        running = 0.0
        out = []
        for (eid, date, memo, source, debit, credit) in self.db.journal.account_postings(
                account_code, as_of):
            debit = round(debit or 0.0, 2)
            credit = round(credit or 0.0, 2)
            delta = (debit - credit) if normal == 'debit' else (credit - debit)
            running = round(running + delta, 2)
            out.append({'entry_id': eid, 'date': date, 'memo': memo, 'source': source,
                        'debit': debit, 'credit': credit, 'balance': running})
        return out

    def is_auto_source(self, source):
        """True for subledger-projected entries that must not be hand-edited."""
        return source in self._AUTO_SOURCES

    # ------------------------------------------------------------------ #
    # Member-activity posting helpers (auto-posting)
    # ------------------------------------------------------------------ #
    def post_loan_disbursement(self, principal, date, source_ref, memo=None):
        """Dr Loans Receivable / Cr Cash (principal moves out as cash)."""
        principal = round(float(principal), 2)
        if principal <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_LOANS_RECEIVABLE, 'debit': principal},
            {'account': GL_CASH, 'credit': principal},
        ], memo=memo or "Loan disbursed", source="loan_disbursement", source_ref=source_ref)

    def post_repayment(self, principal_portion, interest_portion, date, source_ref, memo=None):
        """Dr Cash / Cr Loans Receivable (principal) + Cr Interest Receivable (interest)."""
        p = round(float(principal_portion or 0), 2)
        i = round(float(interest_portion or 0), 2)
        cash = round(p + i, 2)
        if cash <= 0:
            return None
        lines = [{'account': GL_CASH, 'debit': cash}]
        if p:
            lines.append({'account': GL_LOANS_RECEIVABLE, 'credit': p})
        if i:
            lines.append({'account': GL_INTEREST_RECEIVABLE, 'credit': i})
        return self.post_journal(date, lines, memo=memo or "Loan repayment",
                                 source="repayment", source_ref=source_ref)

    def post_interest_accrual(self, interest, date, source_ref, memo=None):
        """Dr Interest Receivable / Cr Loan Interest Income (revenue earned)."""
        interest = round(float(interest), 2)
        if interest <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_INTEREST_RECEIVABLE, 'debit': interest},
            {'account': GL_LOAN_INTEREST_INCOME, 'credit': interest},
        ], memo=memo or "Interest accrued", source="interest", source_ref=source_ref)

    def post_savings_deposit(self, amount, date, source_ref, memo=None):
        """Dr Cash / Cr Member Deposits."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_CASH, 'debit': amount},
            {'account': GL_MEMBER_DEPOSITS, 'credit': amount},
        ], memo=memo or "Member deposit", source="savings", source_ref=source_ref)

    def post_savings_withdrawal(self, amount, date, source_ref, memo=None):
        """Dr Member Deposits / Cr Cash."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_MEMBER_DEPOSITS, 'debit': amount},
            {'account': GL_CASH, 'credit': amount},
        ], memo=memo or "Member withdrawal", source="savings", source_ref=source_ref)

    def post_savings_interest(self, amount, date, source_ref, memo=None):
        """Dr Other Expense / Cr Member Deposits (interest credited to members)."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_OTHER_EXPENSE, 'debit': amount},
            {'account': GL_MEMBER_DEPOSITS, 'credit': amount},
        ], memo=memo or "Interest on member deposits", source="savings", source_ref=source_ref)

    def post_christmas_deposit(self, amount, date, source_ref, memo=None):
        """Dr Cash / Cr Christmas Savings (a member-held liability)."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_CASH, 'debit': amount},
            {'account': GL_CHRISTMAS_SAVINGS, 'credit': amount},
        ], memo=memo or "Christmas deposit", source="christmas", source_ref=source_ref)

    def post_christmas_withdrawal(self, amount, date, source_ref, memo=None):
        """Dr Christmas Savings / Cr Cash."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_CHRISTMAS_SAVINGS, 'debit': amount},
            {'account': GL_CASH, 'credit': amount},
        ], memo=memo or "Christmas withdrawal", source="christmas", source_ref=source_ref)

    def post_benevolent_contribution(self, amount, date, source_ref, memo=None):
        """Dr Cash / Cr Benevolent Fund (welfare reserve held for the membership)."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_CASH, 'debit': amount},
            {'account': GL_BENEVOLENT_FUND, 'credit': amount},
        ], memo=memo or "Benevolent contribution", source="benevolent", source_ref=source_ref)

    def post_benevolent_payout(self, amount, date, source_ref, memo=None):
        """Dr Benevolent Fund / Cr Cash (welfare paid out)."""
        amount = round(float(amount), 2)
        if amount <= 0:
            return None
        return self.post_journal(date, [
            {'account': GL_BENEVOLENT_FUND, 'debit': amount},
            {'account': GL_CASH, 'credit': amount},
        ], memo=memo or "Benevolent payout", source="benevolent", source_ref=source_ref)

    # ------------------------------------------------------------------ #
    # Backfill, sync & migration
    # ------------------------------------------------------------------ #
    # Journals whose truth lives in the member subledgers — a pure projection
    # that can be wiped and re-derived. Manual and migration journals are not
    # in this set and are never touched by a rebuild.
    _AUTO_SOURCES = ("loan_disbursement", "repayment", "interest", "savings",
                     "christmas", "benevolent")

    # Set once a full rebuild has established the projection. Until then the
    # change queue cannot be trusted as the complete list of outstanding work,
    # because an empty queue is indistinguishable from an empty GL.
    _PROJECTION_MARKER = "gl_projection_established"

    def projection_established(self):
        return self.db.settings.get(self._PROJECTION_MARKER) == "1"

    def rebuild_auto_journals(self, progress=None):
        """Drop and re-derive the subledger-projected journals.

        The unconditional path: correct from any starting state, and the cost of
        the whole projection every time. It is what establishes the baseline the
        change queue is measured against, so it empties the queue and marks the
        projection established. Manual and migration entries are preserved.
        Returns the new count.
        """
        with self._bulk():
            self.db.journal.delete_by_sources(self._AUTO_SOURCES)
            count = self.backfill_from_subledgers(progress=progress)
            self.db.journal.outbox_clear()
            self.db.settings.set(self._PROJECTION_MARKER, "1")
        return count

    def drain_outbox(self, progress=None):
        """Project the rows recorded in the change queue. Returns (posted, withdrawn).

        The queue is emptied only up to the id that was read, so a change
        arriving mid-drain keeps its place instead of being discarded
        unprojected. The whole drain is one transaction: if it fails the queue
        survives intact and the next sync retries it.
        """
        high, batch = self.db.journal.outbox_batch()
        if not batch:
            return (0, 0)
        posted = withdrawn = 0
        with self._bulk():
            for table, row_ids in batch.items():
                if table not in self._PROJECTION_PREFIX:
                    logger.warning("Ignoring queued change for unknown table %r", table)
                    continue
                if progress:
                    progress(0, 0, f"Updating the ledger for {len(row_ids)} change(s)…")
                p, w = self.project_rows(table, row_ids)
                posted += p
                withdrawn += w
            self.db.journal.outbox_clear(up_to_id=high)
        return (posted, withdrawn)

    def sync(self, progress=None, full=False):
        """Bring the GL fully up to date before reading statements.

        Applies everything the subledgers have done since the last sync —
        new activity, edits and undos — and migrates any not-yet-migrated legacy
        treasury rows. The whole operation runs in a single transaction (one
        commit), and reports coarse progress via ``progress(done, total, msg)``.

        Normally this drains the change queue, so the cost is proportional to
        what changed rather than to the size of the journal. It falls back to a
        full rebuild when ``full`` is set, or when no rebuild has established
        the projection yet — on a journal opened for the first time after
        migration 005, and on any journal whose marker has been cleared.
        """
        with self._bulk():
            if full or not self.projection_established():
                self.rebuild_auto_journals(progress=progress)
            else:
                self.drain_outbox(progress=progress)
            if progress:
                progress(0, 0, "Migrating legacy treasury entries…")
            self.migrate_legacy_gl()
            if progress:
                progress(1, 1, "Finalising…")

    def backfill_from_subledgers(self, progress=None):
        """Post journals for all existing ledger & savings activity.

        Idempotent (each row's source_ref guards against re-posting), so it is
        safe to run repeatedly. Returns the count of newly posted entries.
        """
        before = self._entry_count()
        with self._bulk():
            ledger_rows = self.db.ledger.gl_projection_rows()
            savings_rows = self.db.funds.gl_projection_rows("savings")
            christmas_rows = self.db.funds.gl_projection_rows("christmas_savings")
            benevolent_rows = self.db.funds.gl_projection_rows("benevolent_ledger")

            total = len(ledger_rows) + len(savings_rows) + len(christmas_rows) + len(benevolent_rows)
            done = 0
            for row in ledger_rows:
                (lid, date, event, added, deducted, p_portion, i_portion, i_amt) = row
                self._post_ledger_event(event, date, added, deducted,
                                        p_portion, i_portion, i_amt, f"ledger:{lid}")
                done += 1
                if progress and done % 200 == 0:
                    progress(done, total, "Posting member activity to the ledger…")

            for (sid, date, ttype, amount) in savings_rows:
                self._post_savings_event(ttype, amount, date, f"savings:{sid}")
                done += 1
                if progress and done % 200 == 0:
                    progress(done, total, "Posting member activity to the ledger…")

            for (cid, date, ttype, amount) in christmas_rows:
                self._post_christmas_event(ttype, amount, date, f"christmas:{cid}")
                done += 1
                if progress and done % 200 == 0:
                    progress(done, total, "Posting member activity to the ledger…")

            for (bid, date, ttype, amount) in benevolent_rows:
                self._post_benevolent_event(ttype, amount, date, f"benevolent:{bid}")
                done += 1
                if progress and done % 200 == 0:
                    progress(done, total, "Posting member activity to the ledger…")

        return self._entry_count() - before

    # ------------------------------------------------------------------ #
    # Incremental projection
    # ------------------------------------------------------------------ #
    # Subledger table -> the source_ref prefix its rows are projected under.
    # `savings` and `christmas_savings` are distinct tables with independent id
    # sequences, so the prefix is what keeps "savings:7" and "christmas:7" apart.
    _PROJECTION_PREFIX = {
        "ledger": "ledger",
        "savings": "savings",
        "christmas_savings": "christmas",
        "benevolent_ledger": "benevolent",
    }

    def project_rows(self, table, row_ids):
        """Bring the GL into line with specific subledger rows.

        The single-row equivalent of ``rebuild_auto_journals``, and the reason
        it exists: the wipe-and-re-derive is correct but costs the whole
        projection every time, because posting alone cannot express the two
        things a member can do after the fact.

        Each id is re-derived from scratch — the previous entry is withdrawn,
        then the current state of the row is posted — which covers all three
        cases with one path:

        * **new row** — nothing to withdraw, the entry is posted;
        * **edited row** — the stale entry is withdrawn and replaced, rather
          than being kept by ``post_journal``'s idempotency short-circuit;
        * **deleted or undone row** — nothing remains to derive, so the
          withdrawal stands and the entry is gone.

        Only the projected sources are touched. Manual, migration and reversal
        entries are never withdrawn, whatever ``source_ref`` they carry.

        Returns ``(posted, withdrawn)``.
        """
        prefix = self._PROJECTION_PREFIX.get(table)
        if prefix is None:
            raise ValueError(f"{table!r} is not a projected subledger")
        ids = [int(i) for i in row_ids]
        if not ids:
            return (0, 0)

        refs = [f"{prefix}:{i}" for i in ids]
        posted = 0
        with self._bulk():
            withdrawn = self.db.journal.delete_projected(refs, self._AUTO_SOURCES)
            for row_id, ref in zip(ids, refs):
                if self._project_one(table, row_id, ref) is not None:
                    posted += 1
        return (posted, withdrawn)

    def _project_one(self, table, row_id, ref):
        """Derive and post the journal for one subledger row, or None if the
        row is gone or its event has no accounting effect."""
        if table == "ledger":
            row = self.db.ledger.gl_projection_row(row_id)
            if not row:
                return None
            date, event, added, deducted, p_portion, i_portion, i_amt = row
            return self._post_ledger_event(event, date, added, deducted,
                                           p_portion, i_portion, i_amt, ref)
        row = self.db.funds.gl_projection_row(table, row_id)
        if not row:
            return None
        date, ttype, amount = row
        if table == "savings":
            return self._post_savings_event(ttype, amount, date, ref)
        if table == "christmas_savings":
            return self._post_christmas_event(ttype, amount, date, ref)
        return self._post_benevolent_event(ttype, amount, date, ref)

    # ----- Live write-path hooks (post journals as rows are created) ----- #
    # Registered on DatabaseManager so member activity posts to the GL the moment
    # it is written, rather than only on the next rebuild. They delegate to
    # project_rows, so re-firing one for a row that has since been edited
    # corrects its entry instead of leaving the stale one in place.
    def post_ledger_rows(self, ledger_ids):
        """Post journals for the given ledger rows (one batched transaction)."""
        self.project_rows("ledger", ledger_ids)

    def post_savings_rows(self, savings_ids):
        """Post journals for the given savings rows (one batched transaction)."""
        self.project_rows("savings", savings_ids)

    def _post_ledger_event(self, event, date, added, deducted,
                           p_portion, i_portion, i_amt, ref):
        """Dispatch one ledger row to the matching journal helper."""
        if event in ("Loan Issued", "Loan Top-Up"):
            return self.post_loan_disbursement(added, date, ref,
                                               memo=f"{event}")
        if event == "Interest Earned":
            return self.post_interest_accrual(i_amt or added, date, ref)
        if event in ("Repayment", "Loan Buyoff"):
            p = round(float(p_portion or 0), 2)
            i = round(float(i_portion or 0), 2)
            if p + i <= 0:
                # Legacy rows without a principal/interest split: treat the whole
                # repayment as principal reduction so the entry still balances.
                p = round(float(deducted or 0), 2)
            return self.post_repayment(p, i, date, ref, memo=event)
        return None  # unknown / non-cash event — nothing to post

    def _post_savings_event(self, ttype, amount, date, ref):
        if ttype == "Deposit":
            return self.post_savings_deposit(amount, date, ref)
        if ttype == "Withdrawal":
            return self.post_savings_withdrawal(amount, date, ref)
        if ttype == "Interest":
            return self.post_savings_interest(amount, date, ref)
        return None

    def _post_christmas_event(self, ttype, amount, date, ref):
        if ttype in ("Deposit", "Interest"):
            return self.post_christmas_deposit(amount, date, ref)
        if ttype == "Withdrawal":
            return self.post_christmas_withdrawal(amount, date, ref)
        return None

    def _post_benevolent_event(self, ttype, amount, date, ref):
        if ttype == "Contribution":
            return self.post_benevolent_contribution(amount, date, ref)
        if ttype == "Withdrawal":  # welfare payout (rare)
            return self.post_benevolent_payout(amount, date, ref)
        return None

    # Best-effort mapping of legacy treasury categories to chart accounts.
    _LEGACY_EXPENSE_MAP = {
        "Office Rent": "5100", "Staff Salaries": "5000",
        "Stationery / Supplies": "5300", "Utility Bills": "5200",
        "Bank Fees / Charges": "5400", "Marketing / PR": "5500",
    }
    _LEGACY_INCOME_MAP = {
        "Fines & Fees (Income)": GL_FEES_INCOME,
        "Bank Interest Earned": GL_BANK_INTEREST_INCOME,
        "Other Income": GL_OTHER_INCOME,
    }

    def migrate_legacy_gl(self):
        """Convert single-entry ``general_ledger`` rows into balanced journals.

        Each legacy row carries one tagged amount; the offsetting side is Cash
        (most treasury items are cash movements), except Asset/Equity injections
        whose counterpart is Opening Balance Equity. Idempotent via source_ref.
        Returns the count of newly migrated entries.
        """
        before = self._entry_count()
        with self._bulk():
            for (gid, date, category, gtype, amount, notes) in self.db.journal.legacy_rows():
                amount = round(float(amount or 0), 2)
                if amount <= 0:
                    continue
                ref = f"gl:{gid}"
                memo = notes or category

                if gtype == "Expense":
                    acct = self._LEGACY_EXPENSE_MAP.get(category, GL_OTHER_EXPENSE)
                    lines = [{'account': acct, 'debit': amount},
                             {'account': GL_CASH, 'credit': amount}]
                elif gtype == "Income":
                    acct = self._LEGACY_INCOME_MAP.get(category, GL_OTHER_INCOME)
                    lines = [{'account': GL_CASH, 'debit': amount},
                             {'account': acct, 'credit': amount}]
                elif gtype == "Liability/Equity":
                    # Borrowings / capital received increased cash; park the credit
                    # in Opening Balance Equity (a later pass can reclassify).
                    lines = [{'account': GL_CASH, 'debit': amount},
                             {'account': GL_OPENING_EQUITY, 'credit': amount}]
                else:  # Asset (e.g. bank deposit / initial capital booked as asset)
                    lines = [{'account': GL_CASH, 'debit': amount},
                             {'account': GL_OPENING_EQUITY, 'credit': amount}]

                self.post_journal(date, lines, memo=memo, source="migration", source_ref=ref)
        return self._entry_count() - before
