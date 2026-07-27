-- =============================================================================
-- PROPOSED — Phase 4. NOT APPLIED, NOT WIRED INTO THE MIGRATION RUNNER.
-- =============================================================================
-- Superseded as migration 001 (see src/migrations/001_integrity_constraints.sql,
-- which delivers cascading deletes, unique constraints and indexes additively).
--
-- This draft rebuilds every table to add declarative ON DELETE CASCADE, NOT NULL
-- and CHECK domains. It is held back because the evidence says the domains
-- cannot be enforced yet: import_selected_data copies event_type, status and
-- start_date verbatim from arbitrary external journals, which are known to carry
-- values outside the current vocabulary (event_type 'Loan Disbursed', loans with
-- no start_date, savings rows with a NULL individual_id). Enforcing them today
-- would reject legitimate imports.
--
-- Phase 4 will: add an import normalisation layer, clean the flagged rows using
-- the report from `python -m src.integrity`, then apply this as migration 004 —
-- one table rebuild rather than two.
--
-- It has been rehearsed against a copy of the production journal (identical row
-- counts, FK-clean, 0.13 s) and is kept verbatim for that reason.
-- =============================================================================

-- =============================================================================
-- 001_initial_schema — adopt the canonical schema on an existing journal
-- =============================================================================
-- Upgrades a live LoanMaster journal (written by the scattered inline SQL in
-- src/database.py) to the model in ../schema.sql, WITHOUT DATA LOSS.
--
-- What it does, and why it has to rebuild tables:
--   SQLite's ALTER TABLE cannot add NOT NULL / CHECK / FOREIGN KEY / UNIQUE to
--   an existing table, so every table that gains a constraint is migrated with
--   the official 12-step procedure (create *_new, copy, drop, rename). Row ids
--   are copied explicitly, so every foreign key and every source_ref like
--   'ledger:1234' keeps pointing at the same row.
--
-- Net changes:
--   * NOT NULL + DEFAULT on columns the app never leaves null
--   * CHECK constraints for the enum domains the app already enforces in Python
--   * FOREIGN KEYs with ON DELETE CASCADE (this is what stops the orphaned-row
--     class of bug the Repair tool exists to clean up)
--   * UNIQUE (individual_id, ref) on loans; unique idempotency key on journals
--   * ledger.loan_pk — a real FK alongside the legacy loan_id reference string
--   * ~20 missing indexes on the hot lookup paths
--   * three read-model views
--
-- HOW TO RUN (offline, app closed):
--     cp loan_master.db loan_master.pre-001.db.bak
--     sqlite3 -bail loan_master.db < migrations/001_initial_schema.sql
--
--   -bail is REQUIRED: the pre-flight guards abort the script by failing a
--   CHECK constraint, and without -bail sqlite3 would carry on past the error.
--   The whole migration is one transaction, so an abort leaves the journal
--   untouched.
--
-- Expected runtime on a ~2 MB journal (119 members / ~17k rows): well under a
-- second. Verified against loan_master.db as of 2026-07-28.
-- =============================================================================

PRAGMA foreign_keys = OFF;   -- required by the 12-step rebuild procedure

BEGIN;

-- =============================================================================
-- PHASE 0 — PRE-FLIGHT GUARDS
-- =============================================================================
-- Each guard inserts 'OK' or a failure message; the CHECK makes a failure abort
-- the transaction with "CHECK constraint failed", naming the problem. Fix the
-- data (the app's Repair tools cover orphans and loan terms), then re-run.

CREATE TEMP TABLE preflight (
    guard  TEXT PRIMARY KEY,
    result TEXT NOT NULL CHECK (result = 'OK')
);

-- No child row may reference a missing (or NULL) member — CASCADE FKs are about
-- to be enforced. Dashboard → Repair → Orphaned records clears these.
INSERT INTO preflight
SELECT 'no_orphaned_children',
       CASE WHEN (SELECT COUNT(*) FROM (
                    SELECT individual_id FROM ledger
                    UNION ALL SELECT individual_id FROM loans
                    UNION ALL SELECT individual_id FROM savings
                    UNION ALL SELECT individual_id FROM christmas_savings
                    UNION ALL SELECT individual_id FROM benevolent_ledger
                    UNION ALL SELECT individual_id FROM benevolent_accounts
                    UNION ALL SELECT individual_id FROM loan_suspensions
                  ) x
                  WHERE x.individual_id IS NULL
                     OR x.individual_id NOT IN (SELECT id FROM individuals)) = 0
            THEN 'OK' ELSE 'FAIL: orphaned child rows — run the Repair tool first' END;

-- loans.(individual_id, ref) is about to become UNIQUE.
INSERT INTO preflight
SELECT 'loan_ref_unique_per_member',
       CASE WHEN (SELECT COUNT(*) FROM (SELECT individual_id, ref FROM loans
                                        GROUP BY 1, 2 HAVING COUNT(*) > 1)) = 0
            THEN 'OK' ELSE 'FAIL: duplicate (individual_id, ref) in loans' END;

-- ledger.event_type is about to become a closed domain.
INSERT INTO preflight
SELECT 'ledger_event_types_known',
       CASE WHEN (SELECT COUNT(*) FROM ledger WHERE event_type NOT IN (
                    'Loan Issued', 'Loan Top-Up', 'Interest Earned',
                    'Repayment', 'Loan Buyoff', 'Loan Restructure')) = 0
            THEN 'OK' ELSE 'FAIL: unknown ledger.event_type values' END;

-- Fund/savings transaction types likewise.
INSERT INTO preflight
SELECT 'fund_txn_types_known',
       CASE WHEN (SELECT COUNT(*) FROM savings
                   WHERE transaction_type NOT IN ('Deposit', 'Withdrawal', 'Interest'))
                + (SELECT COUNT(*) FROM christmas_savings
                   WHERE transaction_type NOT IN ('Deposit', 'Withdrawal', 'Interest'))
                + (SELECT COUNT(*) FROM benevolent_ledger
                   WHERE transaction_type NOT IN ('Contribution', 'Withdrawal', 'Deposit', 'Interest')) = 0
            THEN 'OK' ELSE 'FAIL: unknown transaction_type values' END;

-- Money signs: amounts are magnitudes, direction is carried by the type column.
INSERT INTO preflight
SELECT 'amounts_non_negative',
       CASE WHEN (SELECT COUNT(*) FROM ledger WHERE added < 0 OR deducted < 0)
                + (SELECT COUNT(*) FROM savings WHERE amount < 0)
                + (SELECT COUNT(*) FROM christmas_savings WHERE amount < 0)
                + (SELECT COUNT(*) FROM benevolent_ledger WHERE amount < 0)
                + (SELECT COUNT(*) FROM journal_lines WHERE debit < 0 OR credit < 0) = 0
            THEN 'OK' ELSE 'FAIL: negative amounts present' END;

-- No ledger row may both increase and decrease debt.
INSERT INTO preflight
SELECT 'ledger_one_sided',
       CASE WHEN (SELECT COUNT(*) FROM ledger WHERE added <> 0 AND deducted <> 0) = 0
            THEN 'OK' ELSE 'FAIL: ledger rows with both added and deducted' END;

-- Journal lines are one-sided, and every entry balances.
INSERT INTO preflight
SELECT 'journal_lines_one_sided',
       CASE WHEN (SELECT COUNT(*) FROM journal_lines
                   WHERE (debit = 0) = (credit = 0)) = 0
            THEN 'OK' ELSE 'FAIL: journal lines with both/neither debit and credit' END;

INSERT INTO preflight
SELECT 'journals_balanced',
       CASE WHEN (SELECT COUNT(*) FROM (SELECT entry_id FROM journal_lines GROUP BY 1
                                        HAVING ABS(SUM(debit) - SUM(credit)) > 0.005)) = 0
            THEN 'OK' ELSE 'FAIL: unbalanced journal entries' END;

-- The idempotency key is about to become UNIQUE.
INSERT INTO preflight
SELECT 'journal_source_ref_unique',
       CASE WHEN (SELECT COUNT(*) FROM (SELECT source, source_ref FROM journal_entries
                                        WHERE source_ref IS NOT NULL
                                        GROUP BY 1, 2 HAVING COUNT(*) > 1)) = 0
            THEN 'OK' ELSE 'FAIL: duplicate (source, source_ref) journal entries' END;

-- Dates must parse as YYYY-MM-DD (a trailing time part is fine).
INSERT INTO preflight
SELECT 'dates_well_formed',
       CASE WHEN (SELECT COUNT(*) FROM ledger
                   WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')
                + (SELECT COUNT(*) FROM savings
                   WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')
                + (SELECT COUNT(*) FROM christmas_savings
                   WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')
                + (SELECT COUNT(*) FROM benevolent_ledger
                   WHERE date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')
                + (SELECT COUNT(*) FROM journal_entries
                   WHERE entry_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*') = 0
            THEN 'OK' ELSE 'FAIL: malformed dates' END;

-- Every ledger row must resolve to a loan of the same member, so loan_pk can be
-- backfilled and made a foreign key.
INSERT INTO preflight
SELECT 'ledger_resolves_to_loan',
       CASE WHEN (SELECT COUNT(*) FROM ledger l
                   WHERE NOT EXISTS (SELECT 1 FROM loans o
                                      WHERE o.individual_id = l.individual_id
                                        AND o.ref = l.loan_id)) = 0
            THEN 'OK' ELSE 'FAIL: ledger rows with no matching loan for the member' END;

-- =============================================================================
-- PHASE 1 — DATA NORMALISATION (in place, before constraints bite)
-- =============================================================================

-- Legacy corruption: an edited repayment stored the payment AMOUNT in is_edited
-- instead of the 0/1 flag. The recalculator already treats any nonzero as
-- "edited", so collapsing to 1 changes no behaviour.
UPDATE ledger SET is_edited = 1 WHERE is_edited IS NOT NULL AND is_edited NOT IN (0, 1);
UPDATE ledger SET is_edited = 0 WHERE is_edited IS NULL;

-- Retirement flag and date must agree (the invariant
-- DatabaseManager._retirement_for_status maintains).
UPDATE individuals SET retired_date = date('now')
 WHERE is_retired = 1 AND (retired_date IS NULL OR retired_date = '');
UPDATE individuals SET retired_date = NULL
 WHERE (is_retired = 0 OR is_retired IS NULL) AND retired_date IS NOT NULL;

-- An open suspension span is 'active' with no resumed_date; a closed one is
-- 'resumed' with a date.
UPDATE loan_suspensions SET status = 'resumed' WHERE resumed_date IS NOT NULL;
UPDATE loan_suspensions SET status = 'active'  WHERE resumed_date IS NULL;

-- Reversal bookkeeping: only a 'reversal' row carries reversal_of.
UPDATE journal_entries SET reversal_of = NULL WHERE status <> 'reversal';

-- =============================================================================
-- PHASE 2 — DROP TRIGGERS AND VIEWS
-- =============================================================================
-- They are recreated in Phase 5 from the canonical definitions; dropping first
-- keeps them from firing during the copy and from being rewritten by RENAME.

DROP TRIGGER IF EXISTS trg_loans_created;
DROP TRIGGER IF EXISTS trg_savings_created;
DROP TRIGGER IF EXISTS trg_xmas_created;
DROP TRIGGER IF EXISTS trg_benev_created;
DROP TRIGGER IF EXISTS trg_individuals_updated;
DROP TRIGGER IF EXISTS trg_loans_updated;
DROP TRIGGER IF EXISTS trg_audit_ind_ins;
DROP TRIGGER IF EXISTS trg_audit_ind_upd;
DROP TRIGGER IF EXISTS trg_audit_ind_del;
DROP TRIGGER IF EXISTS trg_audit_loan_ins;
DROP TRIGGER IF EXISTS trg_audit_loan_del;

DROP VIEW IF EXISTS v_member_position;
DROP VIEW IF EXISTS v_trial_balance;
DROP VIEW IF EXISTS v_unbalanced_entries;

-- =============================================================================
-- PHASE 3 — TABLE REBUILDS (12-step procedure, ids preserved)
-- =============================================================================

-- ---- individuals ------------------------------------------------------------
CREATE TABLE individuals_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    -- Nullable by necessity: DatabaseManager.add_individual passes the raw
    -- dialog value through, which is None when the field is left blank. See
    -- docs/db/data-model.md §"Nullable columns that shouldn't be nullable" —
    -- these become NOT NULL once that write path coalesces to ''.
    phone             TEXT    DEFAULT '',
    email             TEXT    DEFAULT '',
    default_deduction REAL    NOT NULL DEFAULT 0 CHECK (default_deduction >= 0),
    employment_status TEXT    NOT NULL DEFAULT 'Active',
    is_retired        INTEGER NOT NULL DEFAULT 0 CHECK (is_retired IN (0, 1)),
    retired_date      TEXT    CHECK (retired_date IS NULL OR retired_date = ''
                                     OR retired_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    pf_no             TEXT    NOT NULL DEFAULT '',
    id_no             TEXT    NOT NULL DEFAULT '',
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT,
    CHECK ((is_retired = 0 AND (retired_date IS NULL OR retired_date = ''))
        OR (is_retired = 1 AND retired_date IS NOT NULL))
);
INSERT INTO individuals_new (id, name, phone, email, default_deduction, employment_status,
                             is_retired, retired_date, pf_no, id_no, import_id, created_at, updated_at)
SELECT id,
       name,
       COALESCE(phone, ''),
       COALESCE(email, ''),
       MAX(COALESCE(default_deduction, 0), 0),
       COALESCE(NULLIF(TRIM(employment_status), ''), 'Active'),
       COALESCE(is_retired, 0),
       NULLIF(retired_date, ''),
       COALESCE(pf_no, ''),
       COALESCE(id_no, ''),
       import_id,
       COALESCE(NULLIF(created_at, ''), datetime('now')),
       updated_at
FROM individuals;
DROP TABLE individuals;
ALTER TABLE individuals_new RENAME TO individuals;

-- ---- import_history (rebuilt early: other tables reference it) --------------
CREATE TABLE import_history_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    source_file TEXT,
    details     TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO import_history_new (id, timestamp, source_file, details, item_count)
SELECT id, COALESCE(NULLIF(timestamp, ''), datetime('now')), source_file, details,
       COALESCE(item_count, 0)
FROM import_history;
DROP TABLE import_history;
ALTER TABLE import_history_new RENAME TO import_history;

-- ---- loans ------------------------------------------------------------------
CREATE TABLE loans_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id     INTEGER NOT NULL REFERENCES individuals(id)
                              ON UPDATE CASCADE ON DELETE CASCADE,
    ref               TEXT    NOT NULL CHECK (length(trim(ref)) > 0),
    principal         REAL    NOT NULL DEFAULT 0 CHECK (principal >= 0),
    total_amount      REAL    NOT NULL DEFAULT 0,
    balance           REAL    NOT NULL DEFAULT 0,
    installment       REAL    NOT NULL DEFAULT 0 CHECK (installment >= 0),
    monthly_interest  REAL    NOT NULL DEFAULT 0,
    unearned_interest REAL    NOT NULL DEFAULT 0,
    interest_balance  REAL    NOT NULL DEFAULT 0,
    start_date        TEXT    NOT NULL
                              CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    next_due_date     TEXT    CHECK (next_due_date IS NULL OR next_due_date = ''
                                     OR next_due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    status            TEXT    NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Paid')),
    is_suspended      INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
    suspend_until     TEXT    CHECK (suspend_until IS NULL OR suspend_until = ''
                                     OR suspend_until GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at        TEXT,
    updated_at        TEXT,
    UNIQUE (individual_id, ref)
);
INSERT INTO loans_new (id, individual_id, ref, principal, total_amount, balance, installment,
                       monthly_interest, unearned_interest, interest_balance, start_date,
                       next_due_date, status, is_suspended, suspend_until, import_id,
                       created_at, updated_at)
SELECT id, individual_id, ref,
       MAX(COALESCE(principal, 0), 0),
       COALESCE(total_amount, 0),
       COALESCE(balance, 0),
       MAX(COALESCE(installment, 0), 0),
       COALESCE(monthly_interest, 0),
       COALESCE(unearned_interest, 0),
       COALESCE(interest_balance, 0),
       -- Legacy rows without an issue date fall back to the ledger's first row
       -- for that loan, then to today.
       COALESCE(NULLIF(start_date, ''),
                (SELECT MIN(l.date) FROM ledger l
                  WHERE l.individual_id = loans.individual_id AND l.loan_id = loans.ref),
                date('now')),
       next_due_date,
       COALESCE(NULLIF(status, ''), 'Active'),
       COALESCE(is_suspended, 0),
       NULLIF(suspend_until, ''),
       import_id, created_at, updated_at
FROM loans;
DROP TABLE loans;
ALTER TABLE loans_new RENAME TO loans;

-- ---- ledger (also gains loan_pk, the real FK) -------------------------------
CREATE TABLE ledger_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id      INTEGER NOT NULL REFERENCES individuals(id)
                               ON UPDATE CASCADE ON DELETE CASCADE,
    date               TEXT    NOT NULL
                               CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    event_type         TEXT    NOT NULL CHECK (event_type IN (
                                   'Loan Issued', 'Loan Top-Up', 'Interest Earned',
                                   'Repayment', 'Loan Buyoff', 'Loan Restructure')),
    loan_id            TEXT    NOT NULL,
    loan_pk            INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    added              REAL    NOT NULL DEFAULT 0 CHECK (added >= 0),
    deducted           REAL    NOT NULL DEFAULT 0 CHECK (deducted >= 0),
    balance            REAL    NOT NULL DEFAULT 0,
    notes              TEXT    NOT NULL DEFAULT '',
    installment_amount REAL    NOT NULL DEFAULT 0,
    interest_amount    REAL    NOT NULL DEFAULT 0,
    principal_balance  REAL    NOT NULL DEFAULT 0,
    interest_balance   REAL    NOT NULL DEFAULT 0,
    gross_balance      REAL    NOT NULL DEFAULT 0,
    principal_portion  REAL    NOT NULL DEFAULT 0,
    interest_portion   REAL    NOT NULL DEFAULT 0,
    is_edited          INTEGER NOT NULL DEFAULT 0 CHECK (is_edited IN (0, 1)),
    previous_state     TEXT,
    batch_id           TEXT,
    import_id          INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    CHECK (added = 0 OR deducted = 0)
);
INSERT INTO ledger_new (id, individual_id, date, event_type, loan_id, loan_pk, added, deducted,
                        balance, notes, installment_amount, interest_amount, principal_balance,
                        interest_balance, gross_balance, principal_portion, interest_portion,
                        is_edited, previous_state, batch_id, import_id)
SELECT l.id, l.individual_id, l.date, l.event_type, l.loan_id,
       -- Backfill the real foreign key from (individual_id, ref).
       (SELECT o.id FROM loans o
         WHERE o.individual_id = l.individual_id AND o.ref = l.loan_id),
       COALESCE(l.added, 0), COALESCE(l.deducted, 0), COALESCE(l.balance, 0),
       COALESCE(l.notes, ''), COALESCE(l.installment_amount, 0), COALESCE(l.interest_amount, 0),
       COALESCE(l.principal_balance, 0), COALESCE(l.interest_balance, 0),
       COALESCE(l.gross_balance, 0), COALESCE(l.principal_portion, 0),
       COALESCE(l.interest_portion, 0), COALESCE(l.is_edited, 0),
       l.previous_state, l.batch_id, l.import_id
FROM ledger l;
DROP TABLE ledger;
ALTER TABLE ledger_new RENAME TO ledger;

-- ---- savings ----------------------------------------------------------------
CREATE TABLE savings_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('Deposit', 'Withdrawal', 'Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    import_id        INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at       TEXT
);
INSERT INTO savings_new (id, individual_id, date, transaction_type, amount, balance, notes,
                         batch_id, import_id, created_at)
SELECT id, individual_id, date, transaction_type, COALESCE(amount, 0), COALESCE(balance, 0),
       COALESCE(notes, ''), batch_id, import_id, created_at
FROM savings;
DROP TABLE savings;
ALTER TABLE savings_new RENAME TO savings;

-- ---- christmas_savings ------------------------------------------------------
CREATE TABLE christmas_savings_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('Deposit', 'Withdrawal', 'Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    created_at       TEXT
);
INSERT INTO christmas_savings_new (id, individual_id, date, transaction_type, amount, balance,
                                   notes, batch_id, created_at)
SELECT id, individual_id, date, transaction_type, COALESCE(amount, 0), COALESCE(balance, 0),
       COALESCE(notes, ''), batch_id, created_at
FROM christmas_savings;
DROP TABLE christmas_savings;
ALTER TABLE christmas_savings_new RENAME TO christmas_savings;

-- ---- benevolent_ledger ------------------------------------------------------
CREATE TABLE benevolent_ledger_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('Contribution', 'Withdrawal', 'Deposit', 'Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    created_at       TEXT
);
INSERT INTO benevolent_ledger_new (id, individual_id, date, transaction_type, amount, balance,
                                   notes, batch_id, created_at)
SELECT id, individual_id, date, transaction_type, COALESCE(amount, 0), COALESCE(balance, 0),
       COALESCE(notes, ''), batch_id, created_at
FROM benevolent_ledger;
DROP TABLE benevolent_ledger;
ALTER TABLE benevolent_ledger_new RENAME TO benevolent_ledger;

-- ---- benevolent_accounts ----------------------------------------------------
CREATE TABLE benevolent_accounts_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id  INTEGER NOT NULL UNIQUE REFERENCES individuals(id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    monthly_amount REAL    NOT NULL DEFAULT 0 CHECK (monthly_amount >= 0),
    start_date     TEXT,
    next_due_date  TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
INSERT INTO benevolent_accounts_new (id, individual_id, monthly_amount, start_date,
                                     next_due_date, active)
SELECT id, individual_id, MAX(COALESCE(monthly_amount, 0), 0), start_date, next_due_date,
       COALESCE(active, 1)
FROM benevolent_accounts;
DROP TABLE benevolent_accounts;
ALTER TABLE benevolent_accounts_new RENAME TO benevolent_accounts;

-- ---- loan_suspensions -------------------------------------------------------
CREATE TABLE loan_suspensions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id       INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    individual_id INTEGER NOT NULL REFERENCES individuals(id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
    loan_ref      TEXT,
    start_date    TEXT NOT NULL,
    suspend_until TEXT,
    resumed_date  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resumed')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((status = 'active'  AND resumed_date IS NULL)
        OR (status = 'resumed' AND resumed_date IS NOT NULL))
);
INSERT INTO loan_suspensions_new (id, loan_id, individual_id, loan_ref, start_date,
                                  suspend_until, resumed_date, status, created_at)
SELECT id, loan_id, individual_id, loan_ref,
       COALESCE(NULLIF(start_date, ''), date('now')),
       NULLIF(suspend_until, ''), NULLIF(resumed_date, ''), status,
       COALESCE(NULLIF(created_at, ''), datetime('now'))
FROM loan_suspensions;
DROP TABLE loan_suspensions;
ALTER TABLE loan_suspensions_new RENAME TO loan_suspensions;

-- ---- chart_of_accounts ------------------------------------------------------
CREATE TABLE chart_of_accounts_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT    NOT NULL UNIQUE CHECK (code GLOB '[0-9]*'),
    name           TEXT    NOT NULL,
    type           TEXT    NOT NULL CHECK (type IN ('Asset', 'Liability', 'Equity', 'Income', 'Expense')),
    normal_balance TEXT    NOT NULL CHECK (normal_balance IN ('debit', 'credit')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    description    TEXT
);
INSERT INTO chart_of_accounts_new (id, code, name, type, normal_balance, is_active, description)
SELECT id, code, name, type, normal_balance, COALESCE(is_active, 1), description
FROM chart_of_accounts;
DROP TABLE chart_of_accounts;
ALTER TABLE chart_of_accounts_new RENAME TO chart_of_accounts;

-- ---- journal_entries --------------------------------------------------------
CREATE TABLE journal_entries_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date  TEXT    NOT NULL
                        CHECK (entry_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    memo        TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual' CHECK (source IN (
                            'manual', 'loan_disbursement', 'repayment', 'interest',
                            'savings', 'christmas', 'benevolent', 'provision',
                            'reversal', 'migration', 'backfill')),
    source_ref  TEXT,
    status      TEXT    NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'reversed', 'reversal')),
    reversal_of INTEGER REFERENCES journal_entries(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,
    CHECK ((status = 'reversal' AND reversal_of IS NOT NULL)
        OR (status <> 'reversal' AND reversal_of IS NULL))
);
INSERT INTO journal_entries_new (id, entry_date, memo, source, source_ref, status,
                                 reversal_of, created_at, created_by)
SELECT id, entry_date, memo, COALESCE(NULLIF(source, ''), 'manual'), source_ref,
       COALESCE(NULLIF(status, ''), 'posted'), reversal_of,
       COALESCE(NULLIF(created_at, ''), datetime('now')), created_by
FROM journal_entries;
DROP TABLE journal_entries;
ALTER TABLE journal_entries_new RENAME TO journal_entries;

-- ---- journal_lines ----------------------------------------------------------
CREATE TABLE journal_lines_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     INTEGER NOT NULL REFERENCES journal_entries(id)
                         ON UPDATE CASCADE ON DELETE CASCADE,
    account_code TEXT    NOT NULL REFERENCES chart_of_accounts(code)
                         ON UPDATE CASCADE ON DELETE RESTRICT,
    debit        REAL    NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit       REAL    NOT NULL DEFAULT 0 CHECK (credit >= 0),
    line_memo    TEXT,
    CHECK ((debit = 0) <> (credit = 0))
);
INSERT INTO journal_lines_new (id, entry_id, account_code, debit, credit, line_memo)
SELECT id, entry_id, account_code, COALESCE(debit, 0), COALESCE(credit, 0), line_memo
FROM journal_lines;
DROP TABLE journal_lines;
ALTER TABLE journal_lines_new RENAME TO journal_lines;

-- ---- general_ledger (legacy, frozen — typed only) ---------------------------
CREATE TABLE general_ledger_new (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT,
    category TEXT,
    type     TEXT CHECK (type IS NULL OR type IN ('Income', 'Expense', 'Asset', 'Liability/Equity')),
    amount   REAL,
    notes    TEXT,
    batch_id TEXT
);
INSERT INTO general_ledger_new (id, date, category, type, amount, notes, batch_id)
SELECT id, date, category, type, amount, notes, batch_id FROM general_ledger;
DROP TABLE general_ledger;
ALTER TABLE general_ledger_new RENAME TO general_ledger;

-- ---- audit_log --------------------------------------------------------------
-- Rows whose operation/entity fall outside the domain would fail the CHECK; the
-- log is diagnostic, so unknown rows are relabelled rather than dropped.
UPDATE audit_log SET operation = 'UPDATE'
 WHERE operation IS NULL OR operation NOT IN ('INSERT', 'UPDATE', 'DELETE');
UPDATE audit_log SET entity = 'individual'
 WHERE entity IS NULL OR entity NOT IN ('individual', 'loan');

CREATE TABLE audit_log_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL DEFAULT (datetime('now')),
    operation TEXT    NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    entity    TEXT    NOT NULL CHECK (entity IN ('individual', 'loan')),
    entity_id INTEGER NOT NULL,
    summary   TEXT
);
INSERT INTO audit_log_new (id, ts, operation, entity, entity_id, summary)
SELECT id, COALESCE(NULLIF(ts, ''), datetime('now')), operation, entity,
       COALESCE(entity_id, 0), summary
FROM audit_log;
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;

-- settings is unchanged (key TEXT PRIMARY KEY, value TEXT) — no rebuild needed.

-- =============================================================================
-- PHASE 4 — INDEXES
-- =============================================================================

CREATE UNIQUE INDEX idx_individuals_pf_no ON individuals(pf_no)
    WHERE pf_no IS NOT NULL AND pf_no != '';
CREATE UNIQUE INDEX idx_individuals_id_no ON individuals(id_no)
    WHERE id_no IS NOT NULL AND id_no != '';
CREATE INDEX idx_individuals_name_nocase ON individuals(name COLLATE NOCASE);
CREATE INDEX idx_individuals_import ON individuals(import_id);

CREATE INDEX idx_loans_individual ON loans(individual_id);
CREATE INDEX idx_loans_status_due ON loans(status, next_due_date);
CREATE INDEX idx_loans_import ON loans(import_id);

CREATE INDEX idx_suspensions_individual ON loan_suspensions(individual_id);
CREATE INDEX idx_suspensions_loan ON loan_suspensions(loan_id);
CREATE UNIQUE INDEX idx_suspensions_open ON loan_suspensions(loan_id)
    WHERE resumed_date IS NULL AND status = 'active';

CREATE INDEX idx_ledger_individual_date ON ledger(individual_id, date, id);
CREATE INDEX idx_ledger_individual_loan ON ledger(individual_id, loan_id, date, id);
CREATE INDEX idx_ledger_loan_pk ON ledger(loan_pk);
CREATE INDEX idx_ledger_batch ON ledger(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_ledger_import ON ledger(import_id) WHERE import_id IS NOT NULL;
CREATE INDEX idx_ledger_date ON ledger(date, id);

CREATE INDEX idx_savings_individual_date ON savings(individual_id, date, id);
CREATE INDEX idx_savings_batch ON savings(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_savings_import ON savings(import_id) WHERE import_id IS NOT NULL;
CREATE INDEX idx_savings_date ON savings(date, id);

CREATE INDEX idx_christmas_individual_date ON christmas_savings(individual_id, date, id);
CREATE INDEX idx_christmas_batch ON christmas_savings(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_benevolent_individual_date ON benevolent_ledger(individual_id, date, id);
CREATE INDEX idx_benevolent_batch ON benevolent_ledger(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_journal_entries_source ON journal_entries(source, source_ref);
CREATE UNIQUE INDEX idx_journal_entries_source_ref
    ON journal_entries(source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX idx_journal_entries_date ON journal_entries(entry_date, id);
CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
CREATE INDEX idx_journal_lines_account ON journal_lines(account_code);

CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX idx_audit_ts ON audit_log(ts);

-- =============================================================================
-- PHASE 5 — TRIGGERS AND VIEWS
-- =============================================================================

CREATE TRIGGER trg_loans_created AFTER INSERT ON loans
WHEN NEW.created_at IS NULL
BEGIN UPDATE loans SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_savings_created AFTER INSERT ON savings
WHEN NEW.created_at IS NULL
BEGIN UPDATE savings SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_xmas_created AFTER INSERT ON christmas_savings
WHEN NEW.created_at IS NULL
BEGIN UPDATE christmas_savings SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_benev_created AFTER INSERT ON benevolent_ledger
WHEN NEW.created_at IS NULL
BEGIN UPDATE benevolent_ledger SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_individuals_updated AFTER UPDATE ON individuals
BEGIN UPDATE individuals SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_loans_updated AFTER UPDATE ON loans
BEGIN UPDATE loans SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_audit_ind_ins AFTER INSERT ON individuals
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'INSERT', 'individual', NEW.id, NEW.name); END;

CREATE TRIGGER trg_audit_ind_upd AFTER UPDATE ON individuals
WHEN NEW.name IS NOT OLD.name OR NEW.phone IS NOT OLD.phone
  OR NEW.email IS NOT OLD.email OR NEW.pf_no IS NOT OLD.pf_no
  OR NEW.id_no IS NOT OLD.id_no
  OR NEW.employment_status IS NOT OLD.employment_status
  OR NEW.is_retired IS NOT OLD.is_retired
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'UPDATE', 'individual', NEW.id, NEW.name); END;

CREATE TRIGGER trg_audit_ind_del AFTER DELETE ON individuals
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'DELETE', 'individual', OLD.id, OLD.name); END;

CREATE TRIGGER trg_audit_loan_ins AFTER INSERT ON loans
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'INSERT', 'loan', NEW.id, NEW.ref); END;

CREATE TRIGGER trg_audit_loan_del AFTER DELETE ON loans
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'DELETE', 'loan', OLD.id, OLD.ref); END;

CREATE TRIGGER trg_individuals_retirement AFTER UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT OLD.employment_status
BEGIN
    UPDATE individuals
       SET is_retired   = CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END,
           retired_date = CASE WHEN NEW.employment_status = 'Active' THEN NULL
                               ELSE COALESCE(NULLIF(NEW.retired_date, ''), date('now')) END
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_journal_entries_immutable BEFORE UPDATE ON journal_entries
WHEN NEW.entry_date IS NOT OLD.entry_date OR NEW.source IS NOT OLD.source
  OR NEW.source_ref IS NOT OLD.source_ref
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable — post a reversal instead'); END;

CREATE TRIGGER trg_journal_lines_immutable BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable — post a reversal instead'); END;

CREATE VIEW v_member_position AS
SELECT i.id                                  AS individual_id,
       i.name,
       i.employment_status,
       i.is_retired,
       COALESCE(l.active_loans, 0)           AS active_loans,
       COALESCE(l.principal_outstanding, 0)  AS principal_outstanding,
       COALESCE(l.interest_outstanding, 0)   AS interest_outstanding,
       COALESCE(s.savings_balance, 0)        AS savings_balance,
       COALESCE(c.christmas_balance, 0)      AS christmas_balance,
       COALESCE(b.benevolent_total, 0)       AS benevolent_total
FROM individuals i
LEFT JOIN (SELECT individual_id, COUNT(*) AS active_loans,
                  SUM(balance) AS principal_outstanding,
                  SUM(interest_balance) AS interest_outstanding
             FROM loans WHERE status = 'Active' GROUP BY individual_id) l
       ON l.individual_id = i.id
LEFT JOIN (SELECT individual_id,
                  SUM(CASE transaction_type WHEN 'Withdrawal' THEN -amount ELSE amount END) AS savings_balance
             FROM savings GROUP BY individual_id) s
       ON s.individual_id = i.id
LEFT JOIN (SELECT individual_id,
                  SUM(CASE transaction_type WHEN 'Withdrawal' THEN -amount ELSE amount END) AS christmas_balance
             FROM christmas_savings GROUP BY individual_id) c
       ON c.individual_id = i.id
LEFT JOIN (SELECT individual_id,
                  SUM(CASE transaction_type WHEN 'Withdrawal' THEN -amount ELSE amount END) AS benevolent_total
             FROM benevolent_ledger GROUP BY individual_id) b
       ON b.individual_id = i.id;

CREATE VIEW v_trial_balance AS
SELECT a.code, a.name, a.type, a.normal_balance,
       ROUND(COALESCE(SUM(l.debit), 0), 2)  AS total_debit,
       ROUND(COALESCE(SUM(l.credit), 0), 2) AS total_credit,
       ROUND(COALESCE(SUM(l.debit), 0) - COALESCE(SUM(l.credit), 0), 2) AS net_debit
FROM chart_of_accounts a
LEFT JOIN journal_lines l ON l.account_code = a.code
GROUP BY a.code, a.name, a.type, a.normal_balance;

CREATE VIEW v_unbalanced_entries AS
SELECT e.id, e.entry_date, e.source, e.source_ref,
       ROUND(SUM(l.debit), 2) AS debits, ROUND(SUM(l.credit), 2) AS credits
FROM journal_entries e
JOIN journal_lines l ON l.entry_id = e.id
GROUP BY e.id
HAVING ABS(SUM(l.debit) - SUM(l.credit)) > 0.005;

-- =============================================================================
-- PHASE 6 — POST-FLIGHT VERIFICATION
-- =============================================================================
-- foreign_key_check must return no rows before the transaction commits.

CREATE TEMP TABLE postflight (
    guard  TEXT PRIMARY KEY,
    result TEXT NOT NULL CHECK (result = 'OK')
);

INSERT INTO postflight
SELECT 'foreign_keys_clean',
       CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_check) = 0
            THEN 'OK' ELSE 'FAIL: foreign key violations after rebuild' END;

INSERT INTO postflight
SELECT 'ledger_loan_pk_backfilled',
       CASE WHEN (SELECT COUNT(*) FROM ledger WHERE loan_pk IS NULL) = 0
            THEN 'OK' ELSE 'FAIL: ledger rows left without loan_pk' END;

COMMIT;

PRAGMA foreign_keys = ON;

-- Reclaim the space freed by the rebuilds and refresh the query planner stats.
VACUUM;
ANALYZE;
