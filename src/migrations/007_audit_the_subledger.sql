-- 007: the money tables get a before-image audit trail.
--
-- Migration 006 froze the authored journals. On its own that is theatre: the
-- general ledger is a projection, so the books say whatever `ledger`,
-- `savings`, `christmas_savings` and `benevolent_ledger` say. Rewriting a
-- ledger row silently restates the trial balance for any period it falls in,
-- and until now nothing recorded that it happened.
--
-- audit_log already existed but covered only members and loans, and only
-- their existence — INSERT and DELETE of a loan, edits to a member's details.
-- Nothing about money. It also stored no before-image, so "this row was
-- changed" could never be answered with "from what?".
--
-- WHY TRIGGERS RATHER THAN A REPOSITORY HOOK
--
-- The same argument as migration 005. Subledger rows are updated from roughly
-- twenty call sites across src/db/ledger.py and src/db/fund.py, plus the
-- balance recalculators and the importer. A Python-level hook would have to be
-- added at each and would still miss a repair script. A trigger sees every
-- write.
--
-- Deletion is the case that motivated this. The Phase 1 review found
-- delete_individual destroying a member's ledger rows with nothing recording
-- it; migration 004 made that a declarative cascade, and SQLite fires child
-- DELETE triggers on a cascade, so every row a cascade removes now leaves a
-- before-image behind.
--
-- WHAT COUNTS AS A CHANGE
--
-- UPDATE triggers are scoped with UPDATE OF to the columns that carry money,
-- the same set the gl_outbox triggers watch. A running-balance recalculation
-- rewrites `balance` on thousands of rows and changes nobody's position; it is
-- bookkeeping, not a restatement, and auditing it would bury the real events.

-- ------------------------------------------------------------------ --
-- audit_log gains a before-image and a wider entity vocabulary.
--
-- Widening a CHECK means rebuilding the table. The five existing audit
-- triggers name audit_log in their bodies, and since SQLite 3.25 ALTER TABLE
-- RENAME rewrites such references, so they are dropped first and recreated
-- unchanged at the end — the same dance migration 004 had to do.
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_audit_ind_ins;
DROP TRIGGER IF EXISTS trg_audit_ind_upd;
DROP TRIGGER IF EXISTS trg_audit_ind_del;
DROP TRIGGER IF EXISTS trg_audit_loan_ins;
DROP TRIGGER IF EXISTS trg_audit_loan_del;

CREATE TABLE audit_log_new (
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

INSERT INTO audit_log_new (id, ts, operation, entity, entity_id, summary)
    SELECT id, ts, operation, entity, entity_id, summary FROM audit_log;

DROP TABLE audit_log;
ALTER TABLE audit_log_new RENAME TO audit_log;

-- idx_audit_ts came from migration 001 and went with the dropped table; it is
-- recreated under its original name rather than a new one.
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);

-- Recreated exactly as they were.
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

-- ------------------------------------------------------------------ --
-- ledger
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_audit_ledger_upd;
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

DROP TRIGGER IF EXISTS trg_audit_ledger_del;
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

-- ------------------------------------------------------------------ --
-- savings / christmas_savings / benevolent_ledger
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_audit_savings_upd;
CREATE TRIGGER trg_audit_savings_upd AFTER UPDATE OF
    date, transaction_type, amount ON savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'savings', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

DROP TRIGGER IF EXISTS trg_audit_savings_del;
CREATE TRIGGER trg_audit_savings_del AFTER DELETE ON savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'savings', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

DROP TRIGGER IF EXISTS trg_audit_christmas_upd;
CREATE TRIGGER trg_audit_christmas_upd AFTER UPDATE OF
    date, transaction_type, amount ON christmas_savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'christmas_savings', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

DROP TRIGGER IF EXISTS trg_audit_christmas_del;
CREATE TRIGGER trg_audit_christmas_del AFTER DELETE ON christmas_savings
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'christmas_savings', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

DROP TRIGGER IF EXISTS trg_audit_benevolent_upd;
CREATE TRIGGER trg_audit_benevolent_upd AFTER UPDATE OF
    date, transaction_type, amount ON benevolent_ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'UPDATE', 'benevolent_ledger', NEW.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

DROP TRIGGER IF EXISTS trg_audit_benevolent_del;
CREATE TRIGGER trg_audit_benevolent_del AFTER DELETE ON benevolent_ledger
BEGIN
    INSERT INTO audit_log(ts, operation, entity, entity_id, summary, before_image)
    VALUES (datetime('now'), 'DELETE', 'benevolent_ledger', OLD.id,
            'member ' || OLD.individual_id || ' ' || OLD.transaction_type,
            json_object('date', OLD.date, 'transaction_type', OLD.transaction_type,
                        'amount', OLD.amount, 'individual_id', OLD.individual_id));
END;

-- NOTE for future migrations: SQLite's table-rebuild procedure drops triggers
-- with the table. A later migration that rebuilds any of these five tables must
-- recreate the audit triggers as well as the gl_outbox ones from 005.
