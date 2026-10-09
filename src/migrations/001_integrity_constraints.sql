-- 001_integrity_constraints
--
-- Phase 1b — referential integrity and the indexes the hot paths need.
--
-- ADDITIVE ONLY. Every statement creates a trigger or an index; no table is
-- rebuilt, no row is read or rewritten, and nothing existing is altered. That
-- keeps the migration O(index build) rather than O(table copy) and makes the
-- rollback a handful of DROP statements (see docs/db/phase-1-migration.md).
--
-- Deliberately NOT here — declarative ON DELETE CASCADE, NOT NULL, and CHECK
-- domains. All three need a full table rebuild, and the evidence says the
-- domains cannot be enforced yet: `import_selected_data` copies event_type,
-- status and start_date verbatim out of arbitrary external journals, which are
-- known to contain values outside the current code's vocabulary (for example
-- event_type 'Loan Disbursed', and loans with no start_date). Enforcing them
-- today would reject legitimate imports. Phase 4 adds an import normalisation
-- layer and then does one table rebuild that applies all three at once —
-- rebuilding once rather than twice. src/integrity.py reports which rows would
-- violate them so the data can be cleaned before then.

-- ---------------------------------------------------------------------------
-- 1. Cascading deletes
-- ---------------------------------------------------------------------------
-- delete_individual only ever removed `ledger` rows, so a deleted member left
-- savings, both funds, their benevolent enrolment and their suspension history
-- behind. Those orphans skew every aggregate that sums a child table, and —
-- because foreign-key enforcement is switched on only when the journal is
-- already clean — a single orphan disables enforcement for the whole
-- connection, letting the next deletion orphan silently too.
--
-- BEFORE DELETE (not AFTER): SQLite raises an immediate foreign-key violation
-- as the parent row is deleted, before AFTER triggers would run. Clearing the
-- children first means the parent delete finds nothing referencing it.
--
-- recursive_triggers is OFF by default, so trg_cascade_loan_delete does NOT
-- fire for the loans deleted inside trg_cascade_individual_delete. The member
-- trigger therefore clears loan_suspensions itself. It also clears them BEFORE
-- deleting loans, because loan_suspensions.loan_id references loans(id).

CREATE TRIGGER IF NOT EXISTS trg_cascade_individual_delete
BEFORE DELETE ON individuals
BEGIN
    DELETE FROM ledger              WHERE individual_id = OLD.id;
    DELETE FROM savings             WHERE individual_id = OLD.id;
    DELETE FROM christmas_savings   WHERE individual_id = OLD.id;
    DELETE FROM benevolent_ledger   WHERE individual_id = OLD.id;
    DELETE FROM benevolent_accounts WHERE individual_id = OLD.id;
    DELETE FROM loan_suspensions    WHERE individual_id = OLD.id;
    DELETE FROM loans               WHERE individual_id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_cascade_loan_delete
BEFORE DELETE ON loans
BEGIN
    DELETE FROM loan_suspensions WHERE loan_id = OLD.id;
END;

-- ---------------------------------------------------------------------------
-- 2. Uniqueness the application currently defends by hand
-- ---------------------------------------------------------------------------
-- A unique index gives exactly the guarantee a table-level UNIQUE constraint
-- would, without rebuilding the table.

-- Loan references are generated per member as max(ref)+1 in Python — a race
-- between two clients, and the import path has to hand-roll "-Import" suffixes
-- to avoid collisions. This makes the rule structural.
CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_member_ref
    ON loans(individual_id, ref);

-- GLService.post_journal guarantees a subledger row is never posted twice by
-- SELECTing for (source, source_ref) and then INSERTing — racy, and bypassed
-- entirely by any other writer. Partial, because manual journals have no
-- source_ref and must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_entries_source_ref
    ON journal_entries(source, source_ref)
    WHERE source_ref IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 3. Indexes for the hot paths
-- ---------------------------------------------------------------------------
-- Before this migration the journal had six indexes, all on audit_log,
-- individuals(pf_no/id_no) and the journal tables: there was no index at all on
-- ledger, loans, savings, the fund tables or loan_suspensions, so every read of
-- a member's history was a full table scan — inside loops that run once per
-- member.

-- get_ledger / get_statement_data / every recalculation pass.
CREATE INDEX IF NOT EXISTS idx_ledger_individual_date
    ON ledger(individual_id, date, id);
-- The ~15 loan-scoped COUNT/MIN/MAX/DELETE probes in loan_service, engine and
-- transaction_manager, all of which filter (individual_id, loan_id).
CREATE INDEX IF NOT EXISTS idx_ledger_individual_loan
    ON ledger(individual_id, loan_id, date, id);
-- GLService.backfill_from_subledgers scans the whole table ordered by date, id.
CREATE INDEX IF NOT EXISTS idx_ledger_date ON ledger(date, id);
-- revert_batch_loans / delete_batch; undo_import.
CREATE INDEX IF NOT EXISTS idx_ledger_batch  ON ledger(batch_id)  WHERE batch_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ledger_import ON ledger(import_id) WHERE import_id IS NOT NULL;

-- get_loans / get_active_loans / get_loan_by_ref / _recalculate_default_deduction.
CREATE INDEX IF NOT EXISTS idx_loans_individual ON loans(individual_id);
-- get_all_active_loans, get_overdue_count, ProvisioningService.classify_loans.
CREATE INDEX IF NOT EXISTS idx_loans_status_due ON loans(status, next_due_date);
CREATE INDEX IF NOT EXISTS idx_loans_import     ON loans(import_id) WHERE import_id IS NOT NULL;

-- Balance lookups (ORDER BY date DESC, id DESC LIMIT 1) and recalculation.
CREATE INDEX IF NOT EXISTS idx_savings_individual_date
    ON savings(individual_id, date, id);
CREATE INDEX IF NOT EXISTS idx_savings_date   ON savings(date, id);
CREATE INDEX IF NOT EXISTS idx_savings_batch  ON savings(batch_id)  WHERE batch_id  IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_savings_import ON savings(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_christmas_individual_date
    ON christmas_savings(individual_id, date, id);
CREATE INDEX IF NOT EXISTS idx_christmas_batch
    ON christmas_savings(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_benevolent_individual_date
    ON benevolent_ledger(individual_id, date, id);
CREATE INDEX IF NOT EXISTS idx_benevolent_batch
    ON benevolent_ledger(batch_id) WHERE batch_id IS NOT NULL;

-- get_loan_suspensions (per member) and the per-loan window lookup in
-- LoanService._suspension_windows.
CREATE INDEX IF NOT EXISTS idx_suspensions_individual ON loan_suspensions(individual_id);
CREATE INDEX IF NOT EXISTS idx_suspensions_loan       ON loan_suspensions(loan_id);

-- Trial balance and the financial statements filter/order by entry_date.
CREATE INDEX IF NOT EXISTS idx_journal_entries_date ON journal_entries(entry_date, id);

-- Duplicate detection runs LOWER(name) against every row for every import row.
CREATE INDEX IF NOT EXISTS idx_individuals_name_nocase
    ON individuals(name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_individuals_import
    ON individuals(import_id) WHERE import_id IS NOT NULL;

-- The audit view reads most-recent-first.
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ---------------------------------------------------------------------------
-- 4. Refresh planner statistics
-- ---------------------------------------------------------------------------
-- Without sqlite_stat1 the planner guesses, and with this many new indexes to
-- choose between the guesses get worse, not better.
ANALYZE;
