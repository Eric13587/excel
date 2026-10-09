-- 011: remove the double-entry general ledger.
--
-- The Treasury / GL subsystem is being withdrawn at the owner's request. This
-- drops everything it owned: the chart of accounts, the journals, the change
-- queue that kept them in step, the fiscal-period register and the period-end
-- snapshots built on top of them.
--
-- WHAT THIS REMOVES, PLAINLY
--
-- The general ledger was the institutional accounting layer: the Balance
-- Sheet, the Profit & Loss, the trial balance, SASRA loan-loss provisioning,
-- fiscal-period closing, and the four control-account reconciliations that
-- compared the GL against the member subledgers. All of that goes.
--
-- WHAT IS DELIBERATELY LEFT ALONE
--
-- Everything member-facing. `ledger`, `savings`, `christmas_savings`,
-- `benevolent_ledger`, `loans` and `individuals` are untouched, so balances,
-- statements, deductions and both funds carry on exactly as before. The audit
-- trail (migration 007) also stays: it records changes to member money and has
-- nothing to do with the GL.
--
-- The earlier migrations that built these objects are NOT edited. They are the
-- record of how the schema got here, and rewriting applied history would break
-- schema_migrations on every existing journal. A new journal therefore creates
-- a few of these objects and drops them again a moment later, which is a small
-- waste and the safe trade.
--
-- IRREVERSIBLE. Restoring means restoring a backup taken before this ran; the
-- journals themselves are deleted, not archived. On the production journal at
-- the time of writing that is 25 accounts, 5,327 entries and 11,678 lines.

-- ------------------------------------------------------------------ --
-- Triggers first: several reference tables dropped below, and a trigger
-- outliving its table makes the next write fail rather than the DROP.
-- ------------------------------------------------------------------ --

-- The GL projection change queue (migration 005).
DROP TRIGGER IF EXISTS trg_outbox_ledger_ins;
DROP TRIGGER IF EXISTS trg_outbox_ledger_upd;
DROP TRIGGER IF EXISTS trg_outbox_ledger_del;
DROP TRIGGER IF EXISTS trg_outbox_savings_ins;
DROP TRIGGER IF EXISTS trg_outbox_savings_upd;
DROP TRIGGER IF EXISTS trg_outbox_savings_del;
DROP TRIGGER IF EXISTS trg_outbox_christmas_ins;
DROP TRIGGER IF EXISTS trg_outbox_christmas_upd;
DROP TRIGGER IF EXISTS trg_outbox_christmas_del;
DROP TRIGGER IF EXISTS trg_outbox_benevolent_ins;
DROP TRIGGER IF EXISTS trg_outbox_benevolent_upd;
DROP TRIGGER IF EXISTS trg_outbox_benevolent_del;

-- Journal immutability (migration 006).
DROP TRIGGER IF EXISTS trg_journal_entries_no_update;
DROP TRIGGER IF EXISTS trg_journal_entries_no_delete;
DROP TRIGGER IF EXISTS trg_journal_lines_no_update;
DROP TRIGGER IF EXISTS trg_journal_lines_no_delete;

-- The fiscal-period lock (migration 009). Every one of these consults
-- fiscal_periods, so all thirteen go with it — including the guard on
-- journal_entries, whose table is dropped below.
DROP TRIGGER IF EXISTS trg_period_lock_ledger_ins;
DROP TRIGGER IF EXISTS trg_period_lock_ledger_upd;
DROP TRIGGER IF EXISTS trg_period_lock_ledger_del;
DROP TRIGGER IF EXISTS trg_period_lock_savings_ins;
DROP TRIGGER IF EXISTS trg_period_lock_savings_upd;
DROP TRIGGER IF EXISTS trg_period_lock_savings_del;
DROP TRIGGER IF EXISTS trg_period_lock_christmas_ins;
DROP TRIGGER IF EXISTS trg_period_lock_christmas_upd;
DROP TRIGGER IF EXISTS trg_period_lock_christmas_del;
DROP TRIGGER IF EXISTS trg_period_lock_benevolent_ins;
DROP TRIGGER IF EXISTS trg_period_lock_benevolent_upd;
DROP TRIGGER IF EXISTS trg_period_lock_benevolent_del;
DROP TRIGGER IF EXISTS trg_period_lock_journal_ins;

-- The period register's own integrity (migration 008).
DROP TRIGGER IF EXISTS trg_fiscal_periods_no_overlap_ins;
DROP TRIGGER IF EXISTS trg_fiscal_periods_no_overlap_upd;
DROP TRIGGER IF EXISTS trg_fiscal_periods_closed_range_frozen;

-- ------------------------------------------------------------------ --
-- Indexes on tables that are about to go.
-- ------------------------------------------------------------------ --
DROP INDEX IF EXISTS idx_journal_entries_source_ref;
DROP INDEX IF EXISTS idx_journal_entries_source;
DROP INDEX IF EXISTS idx_journal_entries_date;
DROP INDEX IF EXISTS idx_journal_lines_entry;
DROP INDEX IF EXISTS idx_journal_lines_account;
DROP INDEX IF EXISTS idx_gl_outbox_table_row;
DROP INDEX IF EXISTS idx_fiscal_periods_range;
DROP INDEX IF EXISTS idx_fiscal_periods_status;
DROP INDEX IF EXISTS idx_account_snapshots_as_of;
DROP INDEX IF EXISTS idx_general_ledger_date;

-- ------------------------------------------------------------------ --
-- Tables. Children before parents so foreign keys never dangle, even
-- with enforcement on.
-- ------------------------------------------------------------------ --
DROP TABLE IF EXISTS account_snapshots;      -- FK -> fiscal_periods, chart_of_accounts
DROP TABLE IF EXISTS journal_lines;          -- FK -> journal_entries, chart_of_accounts
DROP TABLE IF EXISTS journal_entries;
DROP TABLE IF EXISTS fiscal_periods;
DROP TABLE IF EXISTS gl_outbox;
DROP TABLE IF EXISTS chart_of_accounts;
DROP TABLE IF EXISTS general_ledger;         -- the frozen single-entry predecessor

-- The projection marker written by GLService (migration 005's baseline flag).
DELETE FROM settings WHERE key = 'gl_projection_established';

-- Space held by the deleted journals is returned to the file on the next
-- VACUUM; the runner does not do it here because VACUUM cannot run inside a
-- transaction.
