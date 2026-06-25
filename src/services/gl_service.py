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
from contextlib import contextmanager
from datetime import datetime

from src.exceptions import UnbalancedJournalError, UnknownAccountError
from src.config import (
    GL_CASH, GL_LOANS_RECEIVABLE, GL_INTEREST_RECEIVABLE, GL_MEMBER_DEPOSITS,
    GL_LOAN_INTEREST_INCOME, GL_OPENING_EQUITY, GL_FEES_INCOME,
    GL_BANK_INTEREST_INCOME, GL_OTHER_INCOME, GL_OTHER_EXPENSE,
    GL_ALLOWANCE_LOAN_LOSS, GL_SHARE_CAPITAL, GL_RETAINED_EARNINGS,
)

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
            cur = self.db.conn.cursor()
            cur.execute("SELECT code FROM chart_of_accounts")
            self._account_codes = {r[0] for r in cur.fetchall()}
        return self._account_codes

    def get_accounts(self, active_only=True):
        """Return the chart of accounts as a list of dicts."""
        cur = self.db.conn.cursor()
        q = "SELECT code, name, type, normal_balance, is_active, description FROM chart_of_accounts"
        if active_only:
            q += " WHERE is_active = 1"
        q += " ORDER BY code"
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

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

        cur = self.db.conn.cursor()
        cur.execute(
            """INSERT INTO journal_entries
                   (entry_date, memo, source, source_ref, status, created_at, created_by)
               VALUES (?, ?, ?, ?, 'posted', ?, ?)""",
            (entry_date, memo, source, source_ref,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), created_by),
        )
        entry_id = cur.lastrowid
        cur.executemany(
            "INSERT INTO journal_lines (entry_id, account_code, debit, credit, line_memo) VALUES (?, ?, ?, ?, ?)",
            [(entry_id, c, d, cr, lm) for (c, d, cr, lm) in norm],
        )
        self._maybe_commit()
        return entry_id

    def reverse_entry(self, entry_id, reversal_date=None, memo=None):
        """Post a reversing entry (swap debits/credits) and flag the original.

        Corrections never edit or delete a posted entry; they post the inverse.
        Both entries remain in the ledger and net to zero.
        """
        cur = self.db.conn.cursor()
        cur.execute("SELECT entry_date, memo FROM journal_entries WHERE id=?", (entry_id,))
        head = cur.fetchone()
        if not head:
            raise ValueError(f"Journal entry {entry_id} not found.")
        cur.execute("SELECT account_code, debit, credit FROM journal_lines WHERE entry_id=?", (entry_id,))
        orig_lines = cur.fetchall()

        rev_lines = [{'account': code, 'debit': credit, 'credit': debit}
                     for (code, debit, credit) in orig_lines]
        reversal_id = self.post_journal(
            reversal_date or datetime.now().strftime("%Y-%m-%d"),
            rev_lines,
            memo=memo or f"Reversal of entry #{entry_id}",
            source="reversal",
        )
        cur.execute("UPDATE journal_entries SET status='reversed' WHERE id=?", (entry_id,))
        cur.execute("UPDATE journal_entries SET status='reversal', reversal_of=? WHERE id=?",
                    (entry_id, reversal_id))
        self.db.conn.commit()
        return reversal_id

    def _find_entry(self, source, source_ref):
        cur = self.db.conn.cursor()
        cur.execute(
            "SELECT id FROM journal_entries WHERE source=? AND source_ref=? LIMIT 1",
            (source, source_ref),
        )
        row = cur.fetchone()
        return row[0] if row else None

    def _entry_count(self):
        cur = self.db.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM journal_entries")
        return cur.fetchone()[0]

    # ------------------------------------------------------------------ #
    # Balances & trial balance
    # ------------------------------------------------------------------ #
    def get_account_balance(self, account_code, as_of=None):
        """Return an account's balance in its natural (positive) sense.

        Debit-normal accounts are positive when debit-heavy; credit-normal
        accounts are positive when credit-heavy.
        """
        cur = self.db.conn.cursor()
        q = ("SELECT COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) "
             "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
             "WHERE l.account_code = ?")
        params = [account_code]
        if as_of:
            q += " AND e.entry_date <= ?"
            params.append(as_of)
        cur.execute(q, params)
        debit_minus_credit = cur.fetchone()[0] or 0.0

        cur.execute("SELECT normal_balance FROM chart_of_accounts WHERE code=?", (account_code,))
        row = cur.fetchone()
        normal = row[0] if row else 'debit'
        signed = debit_minus_credit if normal == 'debit' else -debit_minus_credit
        return round(signed, 2)

    def get_trial_balance(self, as_of=None):
        """Return (rows, is_balanced).

        rows: per-account {code, name, type, debit, credit} where each account's
        net balance is shown in exactly one column. Across all accounts the debit
        and credit columns must be equal — that equality is the proof the ledger
        is internally consistent.
        """
        cur = self.db.conn.cursor()
        q = ("SELECT a.code, a.name, a.type, "
             "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
             "FROM chart_of_accounts a "
             "LEFT JOIN journal_lines l ON l.account_code = a.code "
             "LEFT JOIN journal_entries e ON l.entry_id = e.id")
        params = []
        if as_of:
            q += " AND e.entry_date <= ?"
            params.append(as_of)
        q += " GROUP BY a.code, a.name, a.type ORDER BY a.code"
        cur.execute(q, params)

        rows = []
        total_debit = 0.0
        total_credit = 0.0
        for code, name, typ, net in cur.fetchall():
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
        cur = self.db.conn.cursor()
        q = ("SELECT a.code, a.name, "
             "COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0) AS net "
             "FROM chart_of_accounts a "
             "LEFT JOIN journal_lines l ON l.account_code = a.code "
             "LEFT JOIN journal_entries e ON l.entry_id = e.id "
             "WHERE a.type = ?")
        params = [acct_type]
        if start_date:
            q += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            q += " AND e.entry_date <= ?"
            params.append(end_date)
        q += " GROUP BY a.code, a.name ORDER BY a.code"
        cur.execute(q, params)

        lines = []
        for code, name, net in cur.fetchall():
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
    _FINANCING_ACCOUNTS = {GL_MEMBER_DEPOSITS, "2100", GL_SHARE_CAPITAL,
                           GL_RETAINED_EARNINGS, GL_OPENING_EQUITY}

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
        cur = self.db.conn.cursor()

        if start_date:
            cur.execute(
                "SELECT COALESCE(SUM(l.debit - l.credit),0) FROM journal_lines l "
                "JOIN journal_entries e ON l.entry_id = e.id "
                "WHERE l.account_code = ? AND e.entry_date < ?",
                (GL_CASH, start_date))
            opening = round(cur.fetchone()[0] or 0.0, 2)
        else:
            opening = 0.0

        q = ("SELECT l.account_code, a.name, l.debit, l.credit "
             "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
             "JOIN chart_of_accounts a ON a.code = l.account_code "
             "WHERE l.account_code != ?")
        params = [GL_CASH]
        if start_date:
            q += " AND e.entry_date >= ?"
            params.append(start_date)
        if end_date:
            q += " AND e.entry_date <= ?"
            params.append(end_date)
        cur.execute(q, params)

        cats = {'Operating': {}, 'Lending to members': {}, 'Financing': {}}
        for code, name, debit, credit in cur.fetchall():
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
        cur = self.db.conn.cursor()
        cur.execute(
            "SELECT e.id, e.entry_date, e.memo, e.source, e.status, "
            "COALESCE(SUM(l.debit), 0) AS amount "
            "FROM journal_entries e LEFT JOIN journal_lines l ON l.entry_id = e.id "
            "GROUP BY e.id ORDER BY e.entry_date DESC, e.id DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_account_ledger(self, account_code, as_of=None):
        """One account's posting history with a running balance (natural sign)."""
        cur = self.db.conn.cursor()
        cur.execute("SELECT normal_balance FROM chart_of_accounts WHERE code=?", (account_code,))
        row = cur.fetchone()
        normal = row[0] if row else 'debit'

        q = ("SELECT e.id, e.entry_date, e.memo, e.source, l.debit, l.credit "
             "FROM journal_lines l JOIN journal_entries e ON l.entry_id = e.id "
             "WHERE l.account_code = ?")
        params = [account_code]
        if as_of:
            q += " AND e.entry_date <= ?"
            params.append(as_of)
        q += " ORDER BY e.entry_date, e.id, l.id"
        cur.execute(q, params)

        running = 0.0
        out = []
        for (eid, date, memo, source, debit, credit) in cur.fetchall():
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

    # ------------------------------------------------------------------ #
    # Backfill, sync & migration
    # ------------------------------------------------------------------ #
    # Journals whose truth lives in the member subledgers — a pure projection
    # that can be wiped and re-derived. Manual and migration journals are not
    # in this set and are never touched by a rebuild.
    _AUTO_SOURCES = ("loan_disbursement", "repayment", "interest", "savings")

    def rebuild_auto_journals(self, progress=None):
        """Drop and re-derive the subledger-projected journals.

        Member activity supports edits and undos, which incremental backfill
        cannot reflect (it only ever adds). Rebuilding the projection slice
        keeps the GL exactly consistent with the current ledger/savings state
        while preserving manual and migration entries. Returns the new count.
        """
        with self._bulk():
            cur = self.db.conn.cursor()
            placeholders = ",".join("?" * len(self._AUTO_SOURCES))
            cur.execute(
                f"DELETE FROM journal_lines WHERE entry_id IN "
                f"(SELECT id FROM journal_entries WHERE source IN ({placeholders}))",
                self._AUTO_SOURCES,
            )
            cur.execute(
                f"DELETE FROM journal_entries WHERE source IN ({placeholders})",
                self._AUTO_SOURCES,
            )
            return self.backfill_from_subledgers(progress=progress)

    def sync(self, progress=None):
        """Bring the GL fully up to date before reading statements.

        Rebuilds the subledger projection (covering new activity, edits and
        undos) and migrates any not-yet-migrated legacy treasury rows. The whole
        operation runs in a single transaction (one commit), and reports coarse
        progress via the optional callback ``progress(done, total, message)``.
        """
        with self._bulk():
            self.rebuild_auto_journals(progress=progress)
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
            cur = self.db.conn.cursor()
            cur.execute("SELECT id, date, event_type, added, deducted, "
                        "principal_portion, interest_portion, interest_amount "
                        "FROM ledger ORDER BY date, id")
            ledger_rows = cur.fetchall()
            cur.execute("SELECT id, date, transaction_type, amount FROM savings ORDER BY date, id")
            savings_rows = cur.fetchall()

            total = len(ledger_rows) + len(savings_rows)
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

        return self._entry_count() - before

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
            cur = self.db.conn.cursor()
            cur.execute("SELECT id, date, category, type, amount, notes FROM general_ledger ORDER BY date, id")
            for (gid, date, category, gtype, amount, notes) in cur.fetchall():
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
