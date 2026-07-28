-- pragma: foreign_keys=off
--
-- 004_strict_schema
--
-- Makes the database enforce what the application has been enforcing in Python:
-- NOT NULL, closed value domains, declarative ON DELETE CASCADE, and a real
-- foreign key from the ledger to the loan it belongs to.
--
-- Why it needs a rebuild: SQLite cannot add NOT NULL, CHECK, FOREIGN KEY or
-- UNIQUE to an existing table, so each one is migrated with the official
-- 12-step procedure — create *_new, copy, drop, rename. Row ids are copied
-- explicitly, so every foreign key and every GL source_ref like 'ledger:1234'
-- still resolves to the same row.
--
-- Prerequisites, all already in place:
--   * migration 001 — cascade triggers, unique indexes, the index set
--   * migration 003 — loans.duration_months / interest_rate
--   * Phase 4a — the importer normalises foreign journals, so an import can no
--     longer introduce a value these constraints would reject
--
-- The cascade triggers from 001 are dropped: they were the interim mechanism
-- for the same guarantee, and keeping both would delete children twice.
--
-- Rollback: restore the backup taken before the migration ran. This rewrites
-- every table, so there is no cheap undo — which is why the pre-flight guards
-- below abort before anything is touched.

-- ===========================================================================
-- PRE-FLIGHT
-- ===========================================================================
-- Each guard inserts 'OK' or a message; the CHECK turns a failure into an
-- abort, and the runner rolls the whole migration back.

CREATE TEMP TABLE preflight_004 (
    guard  TEXT PRIMARY KEY,
    result TEXT NOT NULL CHECK (result = 'OK')
);

INSERT INTO preflight_004 SELECT 'no_orphaned_children',
    CASE WHEN (SELECT COUNT(*) FROM (
        SELECT individual_id FROM ledger
        UNION ALL SELECT individual_id FROM loans
        UNION ALL SELECT individual_id FROM savings
        UNION ALL SELECT individual_id FROM christmas_savings
        UNION ALL SELECT individual_id FROM benevolent_ledger
        UNION ALL SELECT individual_id FROM benevolent_accounts
        UNION ALL SELECT individual_id FROM loan_suspensions) x
      WHERE x.individual_id IS NULL
         OR x.individual_id NOT IN (SELECT id FROM individuals)) = 0
    THEN 'OK' ELSE 'FAIL: orphaned child rows — run the Repair tool first' END;

INSERT INTO preflight_004 SELECT 'members_named',
    CASE WHEN (SELECT COUNT(*) FROM individuals
               WHERE name IS NULL OR TRIM(name) = '') = 0
    THEN 'OK' ELSE 'FAIL: members with no name' END;

INSERT INTO preflight_004 SELECT 'loan_start_dates',
    CASE WHEN (SELECT COUNT(*) FROM loans
               WHERE start_date IS NULL OR start_date NOT GLOB
                     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*') = 0
    THEN 'OK' ELSE 'FAIL: loans without a well-formed start date' END;

INSERT INTO preflight_004 SELECT 'loan_status_vocabulary',
    CASE WHEN (SELECT COUNT(*) FROM loans WHERE status NOT IN ('Active','Paid')) = 0
    THEN 'OK' ELSE 'FAIL: loans with an unknown status' END;

INSERT INTO preflight_004 SELECT 'ledger_event_vocabulary',
    CASE WHEN (SELECT COUNT(*) FROM ledger WHERE event_type NOT IN
        ('Loan Issued','Loan Top-Up','Interest Earned','Repayment',
         'Loan Buyoff','Loan Restructure')) = 0
    THEN 'OK' ELSE 'FAIL: unknown ledger event types' END;

INSERT INTO preflight_004 SELECT 'ledger_dates',
    CASE WHEN (SELECT COUNT(*) FROM ledger WHERE date NOT GLOB
               '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*') = 0
    THEN 'OK' ELSE 'FAIL: malformed ledger dates' END;

INSERT INTO preflight_004 SELECT 'ledger_one_sided',
    CASE WHEN (SELECT COUNT(*) FROM ledger WHERE added <> 0 AND deducted <> 0) = 0
    THEN 'OK' ELSE 'FAIL: ledger rows that both add and deduct' END;

INSERT INTO preflight_004 SELECT 'ledger_resolves_to_a_loan',
    CASE WHEN (SELECT COUNT(*) FROM ledger l WHERE NOT EXISTS (
        SELECT 1 FROM loans o WHERE o.individual_id = l.individual_id
                                AND o.ref = l.loan_id)) = 0
    THEN 'OK' ELSE 'FAIL: ledger rows with no matching loan' END;

INSERT INTO preflight_004 SELECT 'fund_vocabularies',
    CASE WHEN (SELECT COUNT(*) FROM savings
               WHERE transaction_type NOT IN ('Deposit','Withdrawal','Interest'))
            + (SELECT COUNT(*) FROM christmas_savings
               WHERE transaction_type NOT IN ('Deposit','Withdrawal','Interest'))
            + (SELECT COUNT(*) FROM benevolent_ledger
               WHERE transaction_type NOT IN
                     ('Contribution','Withdrawal','Deposit','Interest')) = 0
    THEN 'OK' ELSE 'FAIL: unknown fund transaction types' END;

INSERT INTO preflight_004 SELECT 'amounts_non_negative',
    CASE WHEN (SELECT COUNT(*) FROM ledger WHERE added < 0 OR deducted < 0)
            + (SELECT COUNT(*) FROM savings WHERE amount < 0)
            + (SELECT COUNT(*) FROM christmas_savings WHERE amount < 0)
            + (SELECT COUNT(*) FROM benevolent_ledger WHERE amount < 0)
            + (SELECT COUNT(*) FROM journal_lines WHERE debit < 0 OR credit < 0) = 0
    THEN 'OK' ELSE 'FAIL: negative amounts' END;

INSERT INTO preflight_004 SELECT 'journal_lines_one_sided',
    CASE WHEN (SELECT COUNT(*) FROM journal_lines WHERE (debit = 0) = (credit = 0)) = 0
    THEN 'OK' ELSE 'FAIL: journal lines with both or neither side' END;

-- ===========================================================================
-- NORMALISE IN PLACE (before the constraints bite)
-- ===========================================================================

UPDATE ledger SET is_edited = 0 WHERE is_edited IS NULL;

UPDATE individuals SET retired_date = date('now')
 WHERE is_retired = 1 AND (retired_date IS NULL OR retired_date = '');
UPDATE individuals SET retired_date = NULL
 WHERE (is_retired = 0 OR is_retired IS NULL) AND retired_date IS NOT NULL;

UPDATE loan_suspensions SET status = 'resumed' WHERE resumed_date IS NOT NULL;
UPDATE loan_suspensions SET status = 'active'  WHERE resumed_date IS NULL;

UPDATE journal_entries SET reversal_of = NULL WHERE status <> 'reversal';

-- ===========================================================================
-- DROP TRIGGERS AND VIEWS (recreated at the end)
-- ===========================================================================

DROP TRIGGER IF EXISTS trg_cascade_individual_delete;
DROP TRIGGER IF EXISTS trg_cascade_loan_delete;
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

-- ===========================================================================
-- REBUILDS
-- ===========================================================================

-- ---- import_history (parents first: others reference it) -------------------
CREATE TABLE import_history_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    source_file TEXT,
    details     TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO import_history_new (id, timestamp, source_file, details, item_count)
SELECT id, COALESCE(NULLIF(timestamp,''), datetime('now')), source_file, details,
       COALESCE(item_count, 0) FROM import_history;
DROP TABLE import_history;
ALTER TABLE import_history_new RENAME TO import_history;

-- ---- individuals -----------------------------------------------------------
CREATE TABLE individuals_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL CHECK (length(trim(name)) > 0),
    phone             TEXT    DEFAULT '',
    email             TEXT    DEFAULT '',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    default_deduction REAL    NOT NULL DEFAULT 0 CHECK (default_deduction >= 0),
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    is_retired        INTEGER NOT NULL DEFAULT 0 CHECK (is_retired IN (0,1)),
    retired_date      TEXT,
    employment_status TEXT    NOT NULL DEFAULT 'Active',
    pf_no             TEXT    NOT NULL DEFAULT '',
    id_no             TEXT    NOT NULL DEFAULT '',
    updated_at        TEXT,
    CHECK ((is_retired = 0 AND retired_date IS NULL)
        OR (is_retired = 1 AND retired_date IS NOT NULL))
);
INSERT INTO individuals_new
SELECT id, name, COALESCE(phone,''), COALESCE(email,''),
       COALESCE(NULLIF(created_at,''), datetime('now')),
       MAX(COALESCE(default_deduction,0), 0), import_id,
       COALESCE(is_retired,0), NULLIF(retired_date,''),
       COALESCE(NULLIF(TRIM(employment_status),''), 'Active'),
       COALESCE(pf_no,''), COALESCE(id_no,''), updated_at
FROM individuals;
DROP TABLE individuals;
ALTER TABLE individuals_new RENAME TO individuals;

-- ---- loans -----------------------------------------------------------------
CREATE TABLE loans_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id     INTEGER NOT NULL REFERENCES individuals(id)
                              ON UPDATE CASCADE ON DELETE CASCADE,
    ref               TEXT    NOT NULL CHECK (length(trim(ref)) > 0),
    principal         REAL    NOT NULL DEFAULT 0 CHECK (principal >= 0),
    total_amount      REAL    NOT NULL DEFAULT 0,
    balance           REAL    NOT NULL DEFAULT 0,
    installment       REAL    NOT NULL DEFAULT 0 CHECK (installment >= 0),
    start_date        TEXT    NOT NULL
                              CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    next_due_date     TEXT,
    status            TEXT    NOT NULL DEFAULT 'Active' CHECK (status IN ('Active','Paid')),
    monthly_interest  REAL    NOT NULL DEFAULT 0,
    unearned_interest REAL    NOT NULL DEFAULT 0,
    interest_balance  REAL    NOT NULL DEFAULT 0,
    import_id         INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    is_suspended      INTEGER NOT NULL DEFAULT 0 CHECK (is_suspended IN (0,1)),
    suspend_until     TEXT,
    created_at        TEXT,
    updated_at        TEXT,
    duration_months   INTEGER CHECK (duration_months IS NULL OR duration_months > 0),
    interest_rate     REAL    CHECK (interest_rate IS NULL OR interest_rate >= 0),
    UNIQUE (individual_id, ref)
);
INSERT INTO loans_new
SELECT id, individual_id, ref, MAX(COALESCE(principal,0),0), COALESCE(total_amount,0),
       COALESCE(balance,0), MAX(COALESCE(installment,0),0), start_date,
       NULLIF(next_due_date,''), status, COALESCE(monthly_interest,0),
       COALESCE(unearned_interest,0), COALESCE(interest_balance,0), import_id,
       COALESCE(is_suspended,0), NULLIF(suspend_until,''), created_at, updated_at,
       duration_months, interest_rate
FROM loans;
DROP TABLE loans;
ALTER TABLE loans_new RENAME TO loans;

-- ---- ledger (gains loan_pk, the real foreign key) --------------------------
CREATE TABLE ledger_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id      INTEGER NOT NULL REFERENCES individuals(id)
                               ON UPDATE CASCADE ON DELETE CASCADE,
    date               TEXT    NOT NULL
                               CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    event_type         TEXT    NOT NULL CHECK (event_type IN
                               ('Loan Issued','Loan Top-Up','Interest Earned',
                                'Repayment','Loan Buyoff','Loan Restructure')),
    loan_id            TEXT    NOT NULL,
    added              REAL    NOT NULL DEFAULT 0 CHECK (added >= 0),
    deducted           REAL    NOT NULL DEFAULT 0 CHECK (deducted >= 0),
    balance            REAL    NOT NULL DEFAULT 0,
    notes              TEXT    NOT NULL DEFAULT '',
    installment_amount REAL    NOT NULL DEFAULT 0,
    batch_id           TEXT,
    interest_amount    REAL    NOT NULL DEFAULT 0,
    principal_balance  REAL    NOT NULL DEFAULT 0,
    interest_balance   REAL    NOT NULL DEFAULT 0,
    principal_portion  REAL    NOT NULL DEFAULT 0,
    interest_portion   REAL    NOT NULL DEFAULT 0,
    -- NO CHECK (is_edited IN (0,1)) — deliberately. The column does two jobs:
    -- it is an edit lock, and when > 1 it also carries the payment amount the
    -- user intended, which BalanceRecalculator reads back as a replay anchor
    -- (balance_calculator.py, "Hysteresis Fix"). Constraining it to 0/1 would
    -- change how an edited repayment replays. It is also already
    -- self-defeating: create_tables normalises anything outside 0/1 back to 1
    -- on the next open, so the anchor survives only until the app restarts.
    -- Resolving that means splitting the two jobs into is_edited plus an
    -- edited_amount column — a schema change with a behavioural decision
    -- attached, so it is migration 005, not a rider on this one.
    is_edited          INTEGER NOT NULL DEFAULT 0,
    previous_state     TEXT,
    import_id          INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    gross_balance      REAL    NOT NULL DEFAULT 0,
    loan_pk            INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    CHECK (added = 0 OR deducted = 0)
);
INSERT INTO ledger_new
SELECT l.id, l.individual_id, l.date, l.event_type, l.loan_id,
       COALESCE(l.added,0), COALESCE(l.deducted,0), COALESCE(l.balance,0),
       COALESCE(l.notes,''), COALESCE(l.installment_amount,0), l.batch_id,
       COALESCE(l.interest_amount,0), COALESCE(l.principal_balance,0),
       COALESCE(l.interest_balance,0), COALESCE(l.principal_portion,0),
       COALESCE(l.interest_portion,0), COALESCE(l.is_edited,0), l.previous_state,
       l.import_id, COALESCE(l.gross_balance,0),
       (SELECT o.id FROM loans o
         WHERE o.individual_id = l.individual_id AND o.ref = l.loan_id)
FROM ledger l;
DROP TABLE ledger;
ALTER TABLE ledger_new RENAME TO ledger;

-- ---- savings ---------------------------------------------------------------
CREATE TABLE savings_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN
                             ('Deposit','Withdrawal','Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    import_id        INTEGER REFERENCES import_history(id) ON DELETE SET NULL,
    batch_id         TEXT,
    created_at       TEXT
);
INSERT INTO savings_new
SELECT id, individual_id, date, transaction_type, COALESCE(amount,0),
       COALESCE(balance,0), COALESCE(notes,''), import_id, batch_id, created_at
FROM savings;
DROP TABLE savings;
ALTER TABLE savings_new RENAME TO savings;

-- ---- christmas_savings -----------------------------------------------------
CREATE TABLE christmas_savings_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN
                             ('Deposit','Withdrawal','Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    created_at       TEXT
);
INSERT INTO christmas_savings_new
SELECT id, individual_id, date, transaction_type, COALESCE(amount,0),
       COALESCE(balance,0), COALESCE(notes,''), batch_id, created_at
FROM christmas_savings;
DROP TABLE christmas_savings;
ALTER TABLE christmas_savings_new RENAME TO christmas_savings;

-- ---- benevolent_ledger -----------------------------------------------------
CREATE TABLE benevolent_ledger_new (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id    INTEGER NOT NULL REFERENCES individuals(id)
                             ON UPDATE CASCADE ON DELETE CASCADE,
    date             TEXT    NOT NULL
                             CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    transaction_type TEXT    NOT NULL CHECK (transaction_type IN
                             ('Contribution','Withdrawal','Deposit','Interest')),
    amount           REAL    NOT NULL CHECK (amount >= 0),
    balance          REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT '',
    batch_id         TEXT,
    created_at       TEXT
);
INSERT INTO benevolent_ledger_new
SELECT id, individual_id, date, transaction_type, COALESCE(amount,0),
       COALESCE(balance,0), COALESCE(notes,''), batch_id, created_at
FROM benevolent_ledger;
DROP TABLE benevolent_ledger;
ALTER TABLE benevolent_ledger_new RENAME TO benevolent_ledger;

-- ---- benevolent_accounts ---------------------------------------------------
CREATE TABLE benevolent_accounts_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id  INTEGER NOT NULL UNIQUE REFERENCES individuals(id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    monthly_amount REAL    NOT NULL DEFAULT 0 CHECK (monthly_amount >= 0),
    start_date     TEXT,
    next_due_date  TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);
INSERT INTO benevolent_accounts_new
SELECT id, individual_id, MAX(COALESCE(monthly_amount,0),0), start_date,
       next_due_date, COALESCE(active,1)
FROM benevolent_accounts;
DROP TABLE benevolent_accounts;
ALTER TABLE benevolent_accounts_new RENAME TO benevolent_accounts;

-- ---- loan_suspensions ------------------------------------------------------
CREATE TABLE loan_suspensions_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id       INTEGER REFERENCES loans(id) ON UPDATE CASCADE ON DELETE CASCADE,
    individual_id INTEGER NOT NULL REFERENCES individuals(id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
    loan_ref      TEXT,
    start_date    TEXT NOT NULL,
    suspend_until TEXT,
    resumed_date  TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','resumed')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((status = 'active'  AND resumed_date IS NULL)
        OR (status = 'resumed' AND resumed_date IS NOT NULL))
);
INSERT INTO loan_suspensions_new
SELECT id, loan_id, individual_id, loan_ref,
       COALESCE(NULLIF(start_date,''), date('now')), NULLIF(suspend_until,''),
       NULLIF(resumed_date,''), status,
       COALESCE(NULLIF(created_at,''), datetime('now'))
FROM loan_suspensions;
DROP TABLE loan_suspensions;
ALTER TABLE loan_suspensions_new RENAME TO loan_suspensions;

-- ---- chart_of_accounts -----------------------------------------------------
CREATE TABLE chart_of_accounts_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT    NOT NULL UNIQUE CHECK (code GLOB '[0-9]*'),
    name           TEXT    NOT NULL,
    type           TEXT    NOT NULL CHECK (type IN
                           ('Asset','Liability','Equity','Income','Expense')),
    normal_balance TEXT    NOT NULL CHECK (normal_balance IN ('debit','credit')),
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    description    TEXT
);
INSERT INTO chart_of_accounts_new
SELECT id, code, name, type, normal_balance, COALESCE(is_active,1), description
FROM chart_of_accounts;
DROP TABLE chart_of_accounts;
ALTER TABLE chart_of_accounts_new RENAME TO chart_of_accounts;

-- ---- journal_entries -------------------------------------------------------
CREATE TABLE journal_entries_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date  TEXT    NOT NULL
                        CHECK (entry_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    memo        TEXT,
    source      TEXT    NOT NULL DEFAULT 'manual' CHECK (source IN
                        ('manual','loan_disbursement','repayment','interest','savings',
                         'christmas','benevolent','provision','reversal','migration','backfill')),
    source_ref  TEXT,
    status      TEXT    NOT NULL DEFAULT 'posted'
                        CHECK (status IN ('posted','reversed','reversal')),
    reversal_of INTEGER REFERENCES journal_entries(id) ON UPDATE CASCADE ON DELETE RESTRICT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,
    CHECK ((status = 'reversal' AND reversal_of IS NOT NULL)
        OR (status <> 'reversal' AND reversal_of IS NULL))
);
INSERT INTO journal_entries_new
SELECT id, entry_date, memo, COALESCE(NULLIF(source,''),'manual'), source_ref,
       COALESCE(NULLIF(status,''),'posted'), reversal_of,
       COALESCE(NULLIF(created_at,''), datetime('now')), created_by
FROM journal_entries;
DROP TABLE journal_entries;
ALTER TABLE journal_entries_new RENAME TO journal_entries;

-- ---- journal_lines ---------------------------------------------------------
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
INSERT INTO journal_lines_new
SELECT id, entry_id, account_code, COALESCE(debit,0), COALESCE(credit,0), line_memo
FROM journal_lines;
DROP TABLE journal_lines;
ALTER TABLE journal_lines_new RENAME TO journal_lines;

-- ---- audit_log -------------------------------------------------------------
UPDATE audit_log SET operation = 'UPDATE'
 WHERE operation IS NULL OR operation NOT IN ('INSERT','UPDATE','DELETE');
UPDATE audit_log SET entity = 'individual'
 WHERE entity IS NULL OR entity NOT IN ('individual','loan');

CREATE TABLE audit_log_new (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT    NOT NULL DEFAULT (datetime('now')),
    operation TEXT    NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    entity    TEXT    NOT NULL CHECK (entity IN ('individual','loan')),
    entity_id INTEGER NOT NULL,
    summary   TEXT
);
INSERT INTO audit_log_new
SELECT id, COALESCE(NULLIF(ts,''), datetime('now')), operation, entity,
       COALESCE(entity_id, 0), summary
FROM audit_log;
DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;

-- ===========================================================================
-- INDEXES (dropped with their tables)
-- ===========================================================================

CREATE UNIQUE INDEX idx_individuals_pf_no ON individuals(pf_no)
    WHERE pf_no IS NOT NULL AND pf_no != '';
CREATE UNIQUE INDEX idx_individuals_id_no ON individuals(id_no)
    WHERE id_no IS NOT NULL AND id_no != '';
CREATE INDEX idx_individuals_name_nocase ON individuals(name COLLATE NOCASE);
CREATE INDEX idx_individuals_import ON individuals(import_id);

CREATE UNIQUE INDEX idx_loans_member_ref ON loans(individual_id, ref);
CREATE INDEX idx_loans_individual ON loans(individual_id);
CREATE INDEX idx_loans_status_due ON loans(status, next_due_date);
CREATE INDEX idx_loans_import ON loans(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_ledger_individual_date ON ledger(individual_id, date, id);
CREATE INDEX idx_ledger_individual_loan ON ledger(individual_id, loan_id, date, id);
CREATE INDEX idx_ledger_date ON ledger(date, id);
CREATE INDEX idx_ledger_loan_pk ON ledger(loan_pk);
CREATE INDEX idx_ledger_batch ON ledger(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_ledger_import ON ledger(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_savings_individual_date ON savings(individual_id, date, id);
CREATE INDEX idx_savings_date ON savings(date, id);
CREATE INDEX idx_savings_batch ON savings(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_savings_import ON savings(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_christmas_individual_date ON christmas_savings(individual_id, date, id);
CREATE INDEX idx_christmas_batch ON christmas_savings(batch_id) WHERE batch_id IS NOT NULL;
CREATE INDEX idx_benevolent_individual_date ON benevolent_ledger(individual_id, date, id);
CREATE INDEX idx_benevolent_batch ON benevolent_ledger(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_suspensions_individual ON loan_suspensions(individual_id);
CREATE INDEX idx_suspensions_loan ON loan_suspensions(loan_id);
CREATE UNIQUE INDEX idx_suspensions_open ON loan_suspensions(loan_id)
    WHERE resumed_date IS NULL AND status = 'active';

CREATE INDEX idx_journal_entries_source ON journal_entries(source, source_ref);
CREATE UNIQUE INDEX idx_journal_entries_source_ref
    ON journal_entries(source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX idx_journal_entries_date ON journal_entries(entry_date, id);
CREATE INDEX idx_journal_lines_entry ON journal_lines(entry_id);
CREATE INDEX idx_journal_lines_account ON journal_lines(account_code);

CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX idx_audit_ts ON audit_log(ts);

-- ===========================================================================
-- TRIGGERS
-- ===========================================================================
-- The cascade triggers from migration 001 are NOT recreated: ON DELETE CASCADE
-- above now provides that guarantee declaratively, and having both would try to
-- delete each child twice.

-- ledger.loan_pk is the real foreign key to the loan a row belongs to; loan_id
-- holds only the reference string. The migration backfills it, but without this
-- the column would be correct once and then drift to NULL for every row written
-- afterwards. Filling it in a trigger rather than in the insert statement means
-- it holds for every code path, including bulk catch-up and the importer.
-- recursive_triggers is off, so the UPDATE below does not re-fire this trigger.
CREATE TRIGGER trg_ledger_loan_pk AFTER INSERT ON ledger
WHEN NEW.loan_pk IS NULL
BEGIN
    UPDATE ledger
       SET loan_pk = (SELECT o.id FROM loans o
                       WHERE o.individual_id = NEW.individual_id AND o.ref = NEW.loan_id)
     WHERE id = NEW.id;
END;

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

ANALYZE;
