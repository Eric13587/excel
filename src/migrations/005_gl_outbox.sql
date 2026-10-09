-- 005: a change queue for the general-ledger projection.
--
-- The GL is a projection of the member subledgers, and until now the only way
-- to reflect an edit or an undo was to delete the whole projected slice and
-- re-derive it (GLService.rebuild_auto_journals). That is correct but costs the
-- entire projection on every sync — 1.48s for 5,344 entries on the production
-- journal, whether or not anything changed, and linear in journal size from
-- there.
--
-- Phase 5a made single-row re-projection correct. This is the other half: a
-- record of *which* rows need it. Python-level hooks cannot supply that — they
-- fire on insert only, and the balance recalculators, the importer and
-- fund_add_transaction all write through paths that never reach them. A trigger
-- on the table itself sees every write regardless of the code path, which is
-- the same reason migration 004 uses triggers for loan_pk and the audit trail.
--
-- The queue records only (table, row_id). It deliberately does not record
-- whether the change was an insert, an update or a delete: project_rows decides
-- that by looking at whether the row still exists, so a row queued several
-- times collapses to one re-projection and a queue entry can never disagree
-- with the data.
--
-- UPDATE triggers are scoped with UPDATE OF to the columns the projection
-- actually reads. A running-balance recalculation rewrites `balance` on
-- thousands of rows without changing a single journal, and must not queue them.
--
-- NOTE for future migrations: SQLite's table-rebuild procedure drops triggers
-- along with the table. Any later migration that rebuilds one of these four
-- tables must recreate these triggers, exactly as 004 had to recreate the ones
-- 001 installed.

CREATE TABLE IF NOT EXISTS gl_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT    NOT NULL,
    row_id       INTEGER NOT NULL,
    queued_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- The drain reads in id order and groups by table; the lookup index keeps the
-- de-duplication cheap when one row is touched repeatedly in a session.
CREATE INDEX IF NOT EXISTS idx_gl_outbox_table_row ON gl_outbox(source_table, row_id);

-- ------------------------------------------------------------------ --
-- ledger — projection reads date, event_type, added, deducted,
--          principal_portion, interest_portion, interest_amount
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_outbox_ledger_ins;
CREATE TRIGGER trg_outbox_ledger_ins AFTER INSERT ON ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('ledger', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_ledger_upd;
CREATE TRIGGER trg_outbox_ledger_upd AFTER UPDATE OF
    date, event_type, added, deducted, principal_portion, interest_portion,
    interest_amount ON ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('ledger', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_ledger_del;
CREATE TRIGGER trg_outbox_ledger_del AFTER DELETE ON ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('ledger', OLD.id);
END;

-- ------------------------------------------------------------------ --
-- savings / christmas_savings / benevolent_ledger —
--          projection reads date, transaction_type, amount
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_outbox_savings_ins;
CREATE TRIGGER trg_outbox_savings_ins AFTER INSERT ON savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('savings', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_savings_upd;
CREATE TRIGGER trg_outbox_savings_upd AFTER UPDATE OF
    date, transaction_type, amount ON savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('savings', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_savings_del;
CREATE TRIGGER trg_outbox_savings_del AFTER DELETE ON savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('savings', OLD.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_christmas_ins;
CREATE TRIGGER trg_outbox_christmas_ins AFTER INSERT ON christmas_savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('christmas_savings', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_christmas_upd;
CREATE TRIGGER trg_outbox_christmas_upd AFTER UPDATE OF
    date, transaction_type, amount ON christmas_savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('christmas_savings', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_christmas_del;
CREATE TRIGGER trg_outbox_christmas_del AFTER DELETE ON christmas_savings
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('christmas_savings', OLD.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_benevolent_ins;
CREATE TRIGGER trg_outbox_benevolent_ins AFTER INSERT ON benevolent_ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('benevolent_ledger', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_benevolent_upd;
CREATE TRIGGER trg_outbox_benevolent_upd AFTER UPDATE OF
    date, transaction_type, amount ON benevolent_ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('benevolent_ledger', NEW.id);
END;

DROP TRIGGER IF EXISTS trg_outbox_benevolent_del;
CREATE TRIGGER trg_outbox_benevolent_del AFTER DELETE ON benevolent_ledger
BEGIN
    INSERT INTO gl_outbox (source_table, row_id) VALUES ('benevolent_ledger', OLD.id);
END;

-- This migration deliberately does not mark the projection as established.
-- A journal arriving here may have a GL that is complete, partial or empty, and
-- an empty queue cannot tell those apart. The first sync after this migration
-- therefore does one full rebuild to set the baseline, and every sync after
-- that drains the queue. The same rule makes the system self-healing: clearing
-- the marker is all it takes to force a rebuild.
