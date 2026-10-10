-- =============================================================================
-- LoanMaster — the schema a new journal is created with.
-- =============================================================================
-- Dumped from a journal created by LoanMaster 1.x (schema version 12), which is
-- byte-for-byte the schema of a production journal migrated through 001–012.
-- Keeping it identical is what lets a journal move between the two versions.
--
-- A new journal gets this whole script. An existing journal is never altered:
-- it must already be at version 12 (see Journal::open).
-- =============================================================================


-- ---------- tables ----------
CREATE TABLE "audit_log" (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL DEFAULT (datetime('now')),
    operation    TEXT    NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    entity       TEXT    NOT NULL CHECK (entity IN (
                     'individual','loan',
                     'ledger','savings','christmas_savings','benevolent_ledger')),
    entity_id    INTEGER NOT NULL,
    summary      TEXT,
    -- JSON snapshot of the row as it stood *before* the change. NULL for an
    -- INSERT, where there was nothing before.
    before_image TEXT
);

CREATE TABLE "benevolent_accounts" (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id  INTEGER NOT NULL UNIQUE REFERENCES individuals(id)
                           ON UPDATE CASCADE ON DELETE CASCADE,
    monthly_amount REAL    NOT NULL DEFAULT 0 CHECK (monthly_amount >= 0),
    start_date     TEXT,
    next_due_date  TEXT,
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
);

CREATE TABLE "benevolent_ledger" (
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

CREATE TABLE "christmas_savings" (
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

CREATE TABLE grid_participation (
                individual_id INTEGER NOT NULL,
                scope TEXT NOT NULL,        -- savings | christmas_savings |
                                            -- benevolent_ledger | loans
                excluded INTEGER NOT NULL DEFAULT 1,
                note TEXT,
                created_at TEXT,
                PRIMARY KEY (individual_id, scope)
            );

CREATE TABLE "import_history" (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
    source_file TEXT,
    details     TEXT,
    item_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE "individuals" (
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

CREATE TABLE "ledger" (
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

CREATE TABLE "loan_suspensions" (
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

CREATE TABLE "loans" (
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

CREATE TABLE "savings" (
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

CREATE TABLE schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT    NOT NULL,
                applied_at TEXT    NOT NULL,
                statements INTEGER NOT NULL
            );

CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );


-- ---------- indexs ----------
CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id);

CREATE INDEX idx_audit_ts ON audit_log(ts);

CREATE INDEX idx_benevolent_batch ON benevolent_ledger(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_benevolent_individual_date ON benevolent_ledger(individual_id, date, id);

CREATE INDEX idx_christmas_batch ON christmas_savings(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_christmas_individual_date ON christmas_savings(individual_id, date, id);

CREATE UNIQUE INDEX idx_individuals_id_no ON individuals(id_no)
    WHERE id_no IS NOT NULL AND id_no != '';

CREATE INDEX idx_individuals_import ON individuals(import_id);

CREATE INDEX idx_individuals_name_nocase ON individuals(name COLLATE NOCASE);

CREATE UNIQUE INDEX idx_individuals_pf_no ON individuals(pf_no)
    WHERE pf_no IS NOT NULL AND pf_no != '';

CREATE INDEX idx_individuals_status ON individuals(employment_status);

CREATE INDEX idx_ledger_batch ON ledger(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_ledger_date ON ledger(date, id);

CREATE INDEX idx_ledger_import ON ledger(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_ledger_individual_date ON ledger(individual_id, date, id);

CREATE INDEX idx_ledger_individual_loan ON ledger(individual_id, loan_id, date, id);

CREATE INDEX idx_ledger_loan_pk ON ledger(loan_pk);

CREATE INDEX idx_loans_import ON loans(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_loans_individual ON loans(individual_id);

CREATE UNIQUE INDEX idx_loans_member_ref ON loans(individual_id, ref);

CREATE INDEX idx_loans_status_due ON loans(status, next_due_date);

CREATE INDEX idx_savings_batch ON savings(batch_id) WHERE batch_id IS NOT NULL;

CREATE INDEX idx_savings_date ON savings(date, id);

CREATE INDEX idx_savings_import ON savings(import_id) WHERE import_id IS NOT NULL;

CREATE INDEX idx_savings_individual_date ON savings(individual_id, date, id);

CREATE INDEX idx_suspensions_individual ON loan_suspensions(individual_id);

CREATE INDEX idx_suspensions_loan ON loan_suspensions(loan_id);

CREATE UNIQUE INDEX idx_suspensions_open ON loan_suspensions(loan_id)
    WHERE resumed_date IS NULL AND status = 'active';


-- ---------- triggers ----------
CREATE TRIGGER trg_audit_benevolent_del AFTER DELETE ON benevolent_ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'benevolent_ledger', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_audit_benevolent_upd AFTER UPDATE OF
    date, transaction_type, amount ON benevolent_ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'benevolent_ledger', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_audit_christmas_del AFTER DELETE ON christmas_savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'christmas_savings', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_audit_christmas_upd AFTER UPDATE OF
    date, transaction_type, amount ON christmas_savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'christmas_savings', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_audit_ind_del AFTER DELETE ON individuals
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'DELETE', 'individual', OLD.id, OLD.name); END;

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

CREATE TRIGGER trg_audit_ledger_del AFTER DELETE ON ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'ledger', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.loan_id || ' ' || OLD.event_type,
            json_object('date', OLD.date, 'event_type', OLD.event_type,
                        'added', OLD.added, 'deducted', OLD.deducted,
                        'principal_portion', OLD.principal_portion,
                        'interest_portion', OLD.interest_portion,
                        'interest_amount', OLD.interest_amount,
                        'individual_id', OLD.individual_id, 'loan_id', OLD.loan_id));
END;

CREATE TRIGGER trg_audit_ledger_upd AFTER UPDATE OF
    date, event_type, added, deducted, principal_portion, interest_portion,
    interest_amount ON ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'ledger', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.loan_id || ' ' || OLD.event_type,
            json_object('date', OLD.date, 'event_type', OLD.event_type,
                        'added', OLD.added, 'deducted', OLD.deducted,
                        'principal_portion', OLD.principal_portion,
                        'interest_portion', OLD.interest_portion,
                        'interest_amount', OLD.interest_amount,
                        'individual_id', OLD.individual_id, 'loan_id', OLD.loan_id));
END;

CREATE TRIGGER trg_audit_loan_del AFTER DELETE ON loans
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'DELETE', 'loan', OLD.id, OLD.ref); END;

CREATE TRIGGER trg_audit_loan_ins AFTER INSERT ON loans
BEGIN INSERT INTO audit_log(ts, operation, entity, entity_id, summary)
      VALUES (datetime('now'), 'INSERT', 'loan', NEW.id, NEW.ref); END;

CREATE TRIGGER trg_audit_savings_del AFTER DELETE ON savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'savings', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_audit_savings_upd AFTER UPDATE OF
    date, transaction_type, amount ON savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'savings', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

CREATE TRIGGER trg_benev_created AFTER INSERT ON benevolent_ledger
WHEN NEW.created_at IS NULL
BEGIN UPDATE benevolent_ledger SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_individuals_flag_mirrors_status
AFTER UPDATE OF is_retired ON individuals
WHEN (NEW.is_retired = 1 AND NEW.employment_status = 'Active')
  OR (NEW.is_retired = 0 AND NEW.employment_status <> 'Active')
BEGIN
    -- Only the generic word is inferable from the flag; a specific reason
    -- (Resigned, Deceased) has to be set through employment_status.
    UPDATE individuals
       SET employment_status = CASE WHEN NEW.is_retired = 1 THEN 'Retired' ELSE 'Active' END
     WHERE id = NEW.id;
END;

CREATE TRIGGER trg_individuals_status_ins
BEFORE INSERT ON individuals
WHEN NEW.employment_status IS NOT NULL
 AND NEW.employment_status NOT IN
     ('Active','Retired','Resigned','Terminated','Deceased','Suspended')
BEGIN
    SELECT RAISE(ABORT, 'unknown employment_status');
END;

CREATE TRIGGER trg_individuals_status_mirrors_flag
AFTER UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT NULL
BEGIN
    UPDATE individuals
       SET is_retired = CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END
     WHERE id = NEW.id
       AND is_retired IS NOT (CASE WHEN NEW.employment_status = 'Active' THEN 0 ELSE 1 END);
END;

CREATE TRIGGER trg_individuals_status_upd
BEFORE UPDATE OF employment_status ON individuals
WHEN NEW.employment_status IS NOT NULL
 AND NEW.employment_status NOT IN
     ('Active','Retired','Resigned','Terminated','Deceased','Suspended')
BEGIN
    SELECT RAISE(ABORT, 'unknown employment_status');
END;

CREATE TRIGGER trg_individuals_updated AFTER UPDATE ON individuals
BEGIN UPDATE individuals SET updated_at = datetime('now') WHERE id = NEW.id; END;

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

CREATE TRIGGER trg_loans_updated AFTER UPDATE ON loans
BEGIN UPDATE loans SET updated_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_savings_created AFTER INSERT ON savings
WHEN NEW.created_at IS NULL
BEGIN UPDATE savings SET created_at = datetime('now') WHERE id = NEW.id; END;

CREATE TRIGGER trg_xmas_created AFTER INSERT ON christmas_savings
WHEN NEW.created_at IS NULL
BEGIN UPDATE christmas_savings SET created_at = datetime('now') WHERE id = NEW.id; END;


-- ---------- reference data ----------
INSERT INTO settings (key, value) VALUES
    ('org_name', ''),
    ('org_logo_path', ''),
    ('app_theme', 'Light'),
    ('fy_start_month', 'November'),
    ('deduct_same_month', 'false'),
    ('christmas_unlock_month', '12'),
    ('default_savings_increment', '2500'),
    ('default_christmas_increment', '0'),
    ('default_interest_rate', '0.15'),
    ('excel_header_bg', '#E2E8F0'),
    ('excel_total_bg', '#F1F5F9');

INSERT INTO schema_migrations (version, name, applied_at, statements) VALUES
    (1, 'integrity_constraints', datetime('now'), 0),
    (2, 'seed_default_settings', datetime('now'), 0),
    (3, 'loan_terms', datetime('now'), 0),
    (4, 'strict_schema', datetime('now'), 0),
    (5, 'gl_outbox', datetime('now'), 0),
    (6, 'immutable_journals', datetime('now'), 0),
    (7, 'audit_the_subledger', datetime('now'), 0),
    (8, 'fiscal_periods', datetime('now'), 0),
    (9, 'period_lock', datetime('now'), 0),
    (10, 'account_snapshots', datetime('now'), 0),
    (11, 'remove_general_ledger', datetime('now'), 0),
    (12, 'membership_status', datetime('now'), 0);
