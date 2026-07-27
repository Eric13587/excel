-- =============================================================================
-- LoanMaster — canonical persistent data model (SQLite)
-- =============================================================================
-- Reverse-engineered from application behaviour (src/database.py create_tables,
-- src/services/*, src/views/*) and validated against the production journal
-- loan_master.db (119 members / 2,192 ledger rows / 11,699 journal lines).
--
-- Relationship to the live schema:
--   * Every table and column below exists today (or is an explicitly-marked
--     ADDITION). Nothing here is invented without evidence.
--   * Constraints that the application enforces in Python (enum domains, FK
--     integrity, journal idempotency, money signs) are declared here so the
--     database enforces them too. Every constraint was verified to pass against
--     the current production data before being written.
--
-- Money is REAL (floats) with math.ceil rounding in places — deliberately
-- preserved; a cents/INTEGER migration is a separate project (see CLAUDE.md).
-- Dates are TEXT in 'YYYY-MM-DD' (timestamps 'YYYY-MM-DD HH:MM:SS').
--
-- Apply with:  sqlite3 new.db < schema.sql
-- Migrate an existing journal with: migrations/001_initial_schema.sql
-- =============================================================================

PRAGMA foreign_keys = ON;

-- Reusable date-shape guard: 'YYYY-MM-DD' optionally followed by a time part.
-- (SQLite has no domains; the GLOB is repeated inline in each CHECK.)

-- =============================================================================
-- 1. MEMBERSHIP
-- =============================================================================

-- The SACCO member. Root of every ownership chain in the journal: loans,
-- ledger, savings, both funds and all fund enrolments hang off this row.
CREATE TABLE individuals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    -- Nullable by necessity: DatabaseManager.add_individual passes the raw
    -- dialog value through, which is None when the field is left blank. See
    -- docs/db/data-model.md §"Nullable columns that shouldn't be nullable" —
    -- these become NOT NULL once that write path coalesces to ''.
    phone             TEXT    DEFAULT '',
    email             TEXT    DEFAULT '',
    -- Cached SUM(installment) of the member's active loans. Denormalised on
    -- purpose (payroll deduction schedules read it hot); maintained by
    -- LoanService._recalculate_default_deduction.
    default_deduction REAL    NOT NULL DEFAULT 0 CHECK (default_deduction >= 0),
    -- Employment status drives the retirement flag (see trg_individuals_retirement).
    employment_status TEXT    NOT NULL DEFAULT 'Active',
    is_retired        INTEGER NOT NULL DEFAULT 0 CHECK (is_retired IN (0, 1)),
    retired_date      TEXT    CHECK (retired_date IS NULL OR retired_date = ''
                                     OR retired_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    pf_no             TEXT    NOT NULL DEFAULT '',   -- provident-fund / payroll no.
    id_no             TEXT    NOT NULL DEFAULT '',   -- national ID
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT,
    -- Retirement flag and date must agree (enforced in Python by
    -- DatabaseManager._retirement_for_status; declared here as the invariant).
    CHECK ((is_retired = 0 AND (retired_date IS NULL OR retired_date = ''))
        OR (is_retired = 1 AND retired_date IS NOT NULL))
);

-- Uniqueness only when set: most legacy members carry no PF/ID number, and
-- blank must not collide. Mirrors the partial indexes already in production.
CREATE UNIQUE INDEX idx_individuals_pf_no ON individuals(pf_no)
    WHERE pf_no IS NOT NULL AND pf_no != '';
CREATE UNIQUE INDEX idx_individuals_id_no ON individuals(id_no)
    WHERE id_no IS NOT NULL AND id_no != '';
-- ADDITION: name duplicate detection (individual_name_exists, import merge-by-name)
-- does a LOWER(name) scan on every import row.
CREATE INDEX idx_individuals_name_nocase ON individuals(name COLLATE NOCASE);
-- ADDITION: undo_import() deletes by import_id.
CREATE INDEX idx_individuals_import ON individuals(import_id);

-- =============================================================================
-- 2. LENDING
-- =============================================================================

-- One loan account. `ref` ('L-001') is generated per member by
-- LoanService.add_loan_event as max(ref)+1 — it is UNIQUE PER MEMBER ONLY, and
-- every query in the codebase must scope by individual_id as well (CLAUDE.md).
--
-- Segregated principal/interest model:
--   balance           = outstanding PRINCIPAL only
--   interest_balance  = interest accrued but not yet repaid
--   unearned_interest = interest scheduled but not yet accrued
--   total_amount      = principal + total interest at issue/restructure
CREATE TABLE loans (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id     INTEGER NOT NULL REFERENCES individuals(id)
                              ON UPDATE CASCADE ON DELETE CASCADE,
    ref               TEXT    NOT NULL CHECK (length(trim(ref)) > 0),
    principal         REAL    NOT NULL DEFAULT 0 CHECK (principal >= 0),
    total_amount      REAL    NOT NULL DEFAULT 0,
    balance           REAL    NOT NULL DEFAULT 0,   -- may go slightly negative on legacy rounding
    installment       REAL    NOT NULL DEFAULT 0 CHECK (installment >= 0),
    monthly_interest  REAL    NOT NULL DEFAULT 0,
    unearned_interest REAL    NOT NULL DEFAULT 0,
    interest_balance  REAL    NOT NULL DEFAULT 0,
    start_date        TEXT    NOT NULL
                              CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    -- Cleared to '' when a loan is bought off (buyoff_loan), hence the '' branch.
    next_due_date     TEXT    CHECK (next_due_date IS NULL OR next_due_date = ''
                                     OR next_due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    status            TEXT    NOT NULL DEFAULT 'Active' CHECK (status IN ('Active', 'Paid')),
    -- Live suspension state; the full history lives in loan_suspensions.
    is_suspended      INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0, 1)),
    suspend_until     TEXT    CHECK (suspend_until IS NULL OR suspend_until = ''
                                     OR suspend_until GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at        TEXT,
    updated_at        TEXT,
    -- ADDITION: makes the "unique per member" rule structural instead of a
    -- convention the import path has to defend by hand.
    UNIQUE (individual_id, ref)
);

CREATE INDEX idx_loans_individual ON loans(individual_id);
-- get_all_active_loans / get_overdue_count scan status + next_due_date.
CREATE INDEX idx_loans_status_due ON loans(status, next_due_date);
CREATE INDEX idx_loans_import ON loans(import_id);

-- Audit trail of suspension spans. loans.is_suspended/suspend_until hold only
-- the CURRENT state; this table preserves every span (including backdated and
-- already-closed ones) so statements can annotate "no deductions for months
-- x, y, z" and so catch-up can skip suspended due dates after a resume.
CREATE TABLE loan_suspensions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id       INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    individual_id INTEGER NOT NULL REFERENCES individuals(id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
    loan_ref      TEXT,                 -- denormalised copy of loans.ref (import path)
    start_date    TEXT NOT NULL,        -- date the suspension took effect
    suspend_until TEXT,                 -- intended resume date (NULL = indefinite)
    resumed_date  TEXT,                 -- actual resume date (NULL = still open)
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'resumed')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- An open span is 'active' with no resumed_date; a closed one is 'resumed'
    -- with a date. suspend_loan/resume_loan maintain exactly this pairing.
    CHECK ((status = 'active'  AND resumed_date IS NULL)
        OR (status = 'resumed' AND resumed_date IS NOT NULL))
);

CREATE INDEX idx_suspensions_individual ON loan_suspensions(individual_id);
CREATE INDEX idx_suspensions_loan ON loan_suspensions(loan_id);
-- suspend_loan() looks up "the one open span for this loan" on every call.
CREATE UNIQUE INDEX idx_suspensions_open ON loan_suspensions(loan_id)
    WHERE resumed_date IS NULL AND status = 'active';

-- =============================================================================
-- 3. LOAN SUBLEDGER (member transaction history)
-- =============================================================================

-- Append-mostly per-member transaction history for lending activity. It is the
-- source of truth the balance recalculator replays; loans.* is a cached
-- projection of it, and the double-entry GL is a second projection.
--
-- NOTE ON loan_id: historically this column stores the loan REFERENCE STRING
-- ('L-001'), not loans.id — so it is only meaningful together with
-- individual_id. loan_pk below is the ADDITION that makes the relationship a
-- real foreign key; loan_id is retained because ~100 call sites read it.
CREATE TABLE ledger (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id      INTEGER NOT NULL REFERENCES individuals(id)
                               ON UPDATE CASCADE ON DELETE CASCADE,
    date               TEXT    NOT NULL
                               CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    event_type         TEXT    NOT NULL CHECK (event_type IN (
                                   'Loan Issued', 'Loan Top-Up', 'Interest Earned',
                                   'Repayment', 'Loan Buyoff', 'Loan Restructure')),
    loan_id            TEXT    NOT NULL,   -- loans.ref, scoped by individual_id
    -- ADDITION: the real FK. Backfilled by matching (individual_id, loan_id).
    loan_pk            INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    added              REAL    NOT NULL DEFAULT 0 CHECK (added >= 0),      -- debt increase
    deducted           REAL    NOT NULL DEFAULT 0 CHECK (deducted >= 0),   -- debt decrease
    balance            REAL    NOT NULL DEFAULT 0,   -- running total debt after this row
    notes              TEXT    NOT NULL DEFAULT '',
    installment_amount REAL    NOT NULL DEFAULT 0,   -- schedule in force at this row
    interest_amount    REAL    NOT NULL DEFAULT 0,   -- accrual amount / stored rate
    principal_balance  REAL    NOT NULL DEFAULT 0,   -- running principal component
    interest_balance   REAL    NOT NULL DEFAULT 0,   -- running interest component
    gross_balance      REAL    NOT NULL DEFAULT 0,   -- incl. unearned interest (statements)
    principal_portion  REAL    NOT NULL DEFAULT 0,   -- split of a repayment
    interest_portion   REAL    NOT NULL DEFAULT 0,
    -- Edit lock: 1 means a human set this row's amount, so the recalculator must
    -- not overwrite it. Legacy rows stored the payment AMOUNT here; create_tables
    -- normalises those back to 1, and the CHECK keeps the domain closed.
    is_edited          INTEGER NOT NULL DEFAULT 0 CHECK (is_edited IN (0, 1)),
    previous_state     TEXT,     -- JSON snapshot of loan terms before this row (undo)
    batch_id           TEXT,     -- groups one mass catch-up run (see note on batches)
    import_id          INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    -- No row both increases and decreases debt (holds for all 2,192 live rows).
    CHECK (added = 0 OR deducted = 0)
);

-- Every hot read path is (individual_id [, loan_id]) ORDER BY date, id.
CREATE INDEX idx_ledger_individual_date ON ledger(individual_id, date, id);
CREATE INDEX idx_ledger_individual_loan ON ledger(individual_id, loan_id, date, id);
CREATE INDEX idx_ledger_loan_pk ON ledger(loan_pk);
-- revert_batch_loans / delete_batch filter by batch_id; undo_import by import_id.
CREATE INDEX idx_ledger_batch ON ledger(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_ledger_import ON ledger(import_id) WHERE import_id IS NOT NULL;
-- GLService.backfill_from_subledgers scans the whole table ordered by date, id.
CREATE INDEX idx_ledger_date ON ledger(date, id);

-- =============================================================================
-- 4. SAVINGS & FUNDS
-- =============================================================================
-- savings, christmas_savings and benevolent_ledger are three structurally
-- identical member subledgers (DatabaseManager already drives the latter two
-- through one generic fund_* API over a whitelisted table name). They are kept
-- as separate tables here to match production; see docs/db/data-model.md
-- §"Normalisation opportunities" for the single-table fund_transactions design.

-- Ordinary member savings / shares.
CREATE TABLE savings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('Deposit', 'Withdrawal', 'Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),  -- always positive; type carries the sign
    balance          REAL    NOT NULL DEFAULT 0,            -- running balance after this row
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    import_id        INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    created_at       TEXT
);

CREATE INDEX idx_savings_individual_date ON savings(individual_id, date, id);
CREATE INDEX idx_savings_batch ON savings(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_savings_import ON savings(import_id) WHERE import_id IS NOT NULL;
CREATE INDEX idx_savings_date ON savings(date, id);

-- Christmas pot: same mechanics as savings, but withdrawals are refused outside
-- the unlock month (setting christmas_unlock_month, default 12) by
-- ChristmasService — a business rule, not a schema constraint.
CREATE TABLE christmas_savings (
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

CREATE INDEX idx_christmas_individual_date ON christmas_savings(individual_id, date, id);
CREATE INDEX idx_christmas_batch ON christmas_savings(batch_id) WHERE batch_id IS NOT NULL;

-- Benevolent (welfare) fund contributions. Perpetual — never "paid off";
-- 'Withdrawal' is a welfare payout against the pooled fund.
CREATE TABLE benevolent_ledger (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN ('Contribution', 'Withdrawal', 'Deposit', 'Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,   -- running total contributed
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    created_at       TEXT
);

CREATE INDEX idx_benevolent_individual_date ON benevolent_ledger(individual_id, date, id);
CREATE INDEX idx_benevolent_batch ON benevolent_ledger(batch_id) WHERE batch_id IS NOT NULL;

-- Benevolent enrolment: exactly zero or one per member (1:1), holding the
-- recurring-deduction schedule. Christmas has no equivalent — its increment is
-- inferred from deposit history plus the default_christmas_increment setting.
CREATE TABLE benevolent_accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id  INTEGER NOT NULL UNIQUE REFERENCES individuals(id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    monthly_amount REAL    NOT NULL DEFAULT 0 CHECK (monthly_amount >= 0),
    start_date     TEXT,
    next_due_date  TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- =============================================================================
-- 5. DOUBLE-ENTRY GENERAL LEDGER
-- =============================================================================

-- Account master. `code` is the natural key everything joins on; `id` exists
-- only for consistency with the rest of the schema.
CREATE TABLE chart_of_accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT    NOT NULL UNIQUE CHECK (code GLOB '[0-9]*'),
    name           TEXT    NOT NULL,
    type           TEXT    NOT NULL CHECK (type IN ('Asset', 'Liability', 'Equity', 'Income', 'Expense')),
    normal_balance TEXT    NOT NULL CHECK (normal_balance IN ('debit', 'credit')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    description    TEXT
);

-- Balanced journal header. (source, source_ref) is the idempotency key that
-- stops a subledger row being posted twice — today that is enforced only by a
-- SELECT in GLService.post_journal, which is racy; the partial unique index
-- below makes it structural.
CREATE TABLE journal_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date  TEXT    NOT NULL
                        CHECK (entry_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    memo        TEXT,
    -- 'manual' + provisioning/reversal + the auto-projection sources.
    source      TEXT    NOT NULL DEFAULT 'manual' CHECK (source IN (
                            'manual', 'loan_disbursement', 'repayment', 'interest',
                            'savings', 'christmas', 'benevolent', 'provision',
                            'reversal', 'migration', 'backfill')),
    source_ref  TEXT,   -- e.g. 'ledger:1234', 'savings:99', 'gl:7'
    status      TEXT    NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'reversed', 'reversal')),
    reversal_of INTEGER REFERENCES journal_entries(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,   -- actor; always NULL today (no user table — see §Security)
    CHECK ((status = 'reversal' AND reversal_of IS NOT NULL)
        OR (status <> 'reversal' AND reversal_of IS NULL))
);

CREATE INDEX idx_journal_entries_source ON journal_entries(source, source_ref);
-- ADDITION: enforce the idempotency contract post_journal relies on.
CREATE UNIQUE INDEX idx_journal_entries_source_ref
    ON journal_entries(source, source_ref) WHERE source_ref IS NOT NULL;
-- Trial balance / statements filter and order by entry_date.
CREATE INDEX idx_journal_entries_date ON journal_entries(entry_date, id);

-- Debit/credit detail. Every entry satisfies SUM(debit) = SUM(credit) within
-- 0.005 (asserted in GLService.post_journal and by trg_journal_lines_balanced).
CREATE TABLE journal_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id     INTEGER NOT NULL REFERENCES journal_entries(id)
                         ON UPDATE CASCADE ON DELETE CASCADE,
    account_code TEXT    NOT NULL REFERENCES chart_of_accounts(code)
                         ON UPDATE CASCADE ON DELETE RESTRICT,
    debit        REAL    NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit       REAL    NOT NULL DEFAULT 0 CHECK (credit >= 0),
    line_memo    TEXT,
    -- A line is one-sided and non-empty.
    CHECK ((debit = 0) <> (credit = 0))
);

CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
CREATE INDEX idx_journal_lines_account ON journal_lines(account_code);

-- Legacy single-entry treasury table — FROZEN. No code writes to it; retained
-- read-only so GLService.migrate_legacy_gl can convert historical rows into
-- balanced journals (idempotent via source_ref 'gl:<id>').
CREATE TABLE general_ledger (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    date     TEXT,
    category TEXT,
    type     TEXT CHECK (type IS NULL OR type IN ('Income', 'Expense', 'Asset', 'Liability/Equity')),
    amount   REAL,
    notes    TEXT,
    batch_id TEXT
);

-- =============================================================================
-- 6. OPERATIONS: SETTINGS, IMPORT, AUDIT
-- =============================================================================

-- Per-journal key/value configuration. Keys in use (all values stored as TEXT):
--   org_name, org_logo_path                  — document branding (src/branding.py)
--   app_theme                                — 'Light' | 'Dark'
--   fy_start_month                           — e.g. 'November'
--   deduct_same_month                        — 'true' | 'false'
--   christmas_unlock_month                   — '1'..'12'
--   default_savings_increment, default_christmas_increment
--   excel_header_bg, excel_total_bg          — '#RRGGBB' export palette
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per external-journal import run. Children (individuals, loans,
-- ledger, savings) carry import_id so undo_import can delete a whole run.
CREATE TABLE import_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    source_file TEXT,
    details     TEXT,    -- JSON: {"individuals":n,"loans":n,"ledger":n,"savings":n,...}
    item_count  INTEGER NOT NULL DEFAULT 0
);

-- Trigger-maintained CRUD trail. Deliberately narrow: all individual changes,
-- loan create/delete only — loan UPDATEs and ledger rows churn on every
-- recalculation and would flood the log (the ledger is its own dated history).
CREATE TABLE audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL DEFAULT (datetime('now')),
    operation TEXT    NOT NULL CHECK (operation IN ('INSERT', 'UPDATE', 'DELETE')),
    entity    TEXT    NOT NULL CHECK (entity IN ('individual', 'loan')),
    entity_id INTEGER NOT NULL,
    summary   TEXT
);

CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);
-- ADDITION: the UI reads the log most-recent-first, optionally by date.
CREATE INDEX idx_audit_ts ON audit_log(ts);

-- =============================================================================
-- 7. TRIGGERS
-- =============================================================================

-- --- Timestamps (populated only when the caller didn't supply one) ----------
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

-- --- Audit trail ------------------------------------------------------------
CREATE TRIGGER trg_audit_ind_ins AFTER INSERT ON individuals
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'INSERT', 'individual', NEW.id, NEW.name); END;

-- Only identity edits — not default_deduction/updated_at churn.
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

-- --- ADDITIONS: invariants currently living only in Python ------------------

-- Employment status drives the retirement flag (DatabaseManager
-- ._retirement_for_status). Keeps direct SQL writes from desynchronising them.
CREATE TRIGGER trg_individuals_retirement AFTER UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT OLD.employment_status
BEGIN
    UPDATE individuals
       SET is_retired   = CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END,
           retired_date = CASE WHEN NEW.employment_status = 'Active' THEN NULL
                               ELSE COALESCE(NULLIF(NEW.retired_date, ''), date('now')) END
     WHERE id = NEW.id;
END;

-- A posted journal entry is immutable: corrections are posted as reversals
-- (GLService.reverse_entry), never as edits. Only the status/reversal_of
-- bookkeeping columns may change.
CREATE TRIGGER trg_journal_entries_immutable BEFORE UPDATE ON journal_entries
WHEN NEW.entry_date IS NOT OLD.entry_date OR NEW.source IS NOT OLD.source
  OR NEW.source_ref IS NOT OLD.source_ref
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable — post a reversal instead'); END;

-- Journal lines may never be edited in place either.
CREATE TRIGGER trg_journal_lines_immutable BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable — post a reversal instead'); END;

-- =============================================================================
-- 8. VIEWS (read models the UI/reports assemble by hand today)
-- =============================================================================

-- Current member position in one row — dashboard, statements and provisioning
-- all recompute this from three separate queries today.
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

-- Trial balance, straight from the ledger (GLService.get_trial_balance in SQL).
CREATE VIEW v_trial_balance AS
SELECT a.code, a.name, a.type, a.normal_balance,
       ROUND(COALESCE(SUM(l.debit), 0), 2)  AS total_debit,
       ROUND(COALESCE(SUM(l.credit), 0), 2) AS total_credit,
       ROUND(COALESCE(SUM(l.debit), 0) - COALESCE(SUM(l.credit), 0), 2) AS net_debit
FROM chart_of_accounts a
LEFT JOIN journal_lines l ON l.account_code = a.code
GROUP BY a.code, a.name, a.type, a.normal_balance;

-- Any entry whose legs don't agree. Must always be empty; a cheap health probe
-- for the dashboard's integrity panel.
CREATE VIEW v_unbalanced_entries AS
SELECT e.id, e.entry_date, e.source, e.source_ref,
       ROUND(SUM(l.debit), 2) AS debits, ROUND(SUM(l.credit), 2) AS credits
FROM journal_entries e
JOIN journal_lines l ON l.entry_id = e.id
GROUP BY e.id
HAVING ABS(SUM(l.debit) - SUM(l.credit)) > 0.005;
