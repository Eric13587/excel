-- 009: a closed fiscal period becomes a lock.
--
-- Migration 008 declared what a period is. This makes closing one mean
-- something: no money may be written into, changed inside, or removed from a
-- range whose figures have been published.
--
-- WHERE THE LOCK HAS TO SIT
--
-- Not on journal_entries, or not only there. The general ledger is a
-- *projection* derived from the subledgers by date, so a back-dated edit to a
-- ledger row walks straight into a closed period the next time the change queue
-- drains — the journal it produces is new, but the restatement already
-- happened. The subledger is where the lock belongs.
--
-- THE CONSEQUENCE FOR THE PROJECTION
--
-- sync(full=True) deletes every projected journal and re-posts it, including
-- entries dated in closed periods. A blanket guard on journal_entries would
-- make a full rebuild impossible on any journal with a closed period — the
-- recovery path would be the first casualty of the safety feature.
--
-- So the journal guard covers *authored* entries only, exactly the split
-- migration 006 uses. A projected entry inside a closed period is safe because
-- its source row is frozen: re-deriving it can only reproduce what was already
-- there. The subledger lock is the real control; the journal guard just stops
-- someone hand-posting into a settled quarter.
--
-- WHAT IS STILL ALLOWED
--
-- * Running-balance recalculation. The UPDATE triggers are scoped with
--   UPDATE OF to the columns that carry money, matching gl_outbox (005) and the
--   audit trail (007). Rewriting `balance` restates nobody's position.
-- * Reversals and corrections, provided they are dated in an open period. That
--   is the accounting answer to a mistake found after a close, and it is why
--   the lock is on the date rather than on the act.
-- * Everything, on a journal with no closed periods — which is how 008 ships.
--   These triggers are inert until somebody closes something.

-- ------------------------------------------------------------------ --
-- ledger
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_period_lock_ledger_ins;
CREATE TRIGGER trg_period_lock_ledger_ins BEFORE INSERT ON ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND NEW.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that date falls in a closed fiscal period');
END;

-- Both sides are checked: moving a row *out* of a closed period changes that
-- period's figures just as surely as moving one in.
DROP TRIGGER IF EXISTS trg_period_lock_ledger_upd;
CREATE TRIGGER trg_period_lock_ledger_upd BEFORE UPDATE OF
    date, event_type, added, deducted, principal_portion, interest_portion,
    interest_amount ON ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND (OLD.date BETWEEN p.start_date AND p.end_date
                OR NEW.date BETWEEN p.start_date AND p.end_date))
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_ledger_del;
CREATE TRIGGER trg_period_lock_ledger_del BEFORE DELETE ON ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND OLD.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

-- ------------------------------------------------------------------ --
-- savings
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_period_lock_savings_ins;
CREATE TRIGGER trg_period_lock_savings_ins BEFORE INSERT ON savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND NEW.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that date falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_savings_upd;
CREATE TRIGGER trg_period_lock_savings_upd BEFORE UPDATE OF
    date, transaction_type, amount ON savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND (OLD.date BETWEEN p.start_date AND p.end_date
                OR NEW.date BETWEEN p.start_date AND p.end_date))
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_savings_del;
CREATE TRIGGER trg_period_lock_savings_del BEFORE DELETE ON savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND OLD.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

-- ------------------------------------------------------------------ --
-- christmas_savings
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_period_lock_christmas_ins;
CREATE TRIGGER trg_period_lock_christmas_ins BEFORE INSERT ON christmas_savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND NEW.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that date falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_christmas_upd;
CREATE TRIGGER trg_period_lock_christmas_upd BEFORE UPDATE OF
    date, transaction_type, amount ON christmas_savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND (OLD.date BETWEEN p.start_date AND p.end_date
                OR NEW.date BETWEEN p.start_date AND p.end_date))
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_christmas_del;
CREATE TRIGGER trg_period_lock_christmas_del BEFORE DELETE ON christmas_savings
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND OLD.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

-- ------------------------------------------------------------------ --
-- benevolent_ledger
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_period_lock_benevolent_ins;
CREATE TRIGGER trg_period_lock_benevolent_ins BEFORE INSERT ON benevolent_ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND NEW.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that date falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_benevolent_upd;
CREATE TRIGGER trg_period_lock_benevolent_upd BEFORE UPDATE OF
    date, transaction_type, amount ON benevolent_ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND (OLD.date BETWEEN p.start_date AND p.end_date
                OR NEW.date BETWEEN p.start_date AND p.end_date))
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

DROP TRIGGER IF EXISTS trg_period_lock_benevolent_del;
CREATE TRIGGER trg_period_lock_benevolent_del BEFORE DELETE ON benevolent_ledger
WHEN EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND OLD.date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that row falls in a closed fiscal period');
END;

-- ------------------------------------------------------------------ --
-- journal_entries — authored postings only.
--
-- Projected sources are exempt: their source rows are frozen by the triggers
-- above, so re-deriving one can only reproduce what was already there, and
-- without the exemption sync(full=True) could never run again.
-- ------------------------------------------------------------------ --
DROP TRIGGER IF EXISTS trg_period_lock_journal_ins;
CREATE TRIGGER trg_period_lock_journal_ins BEFORE INSERT ON journal_entries
WHEN NEW.source NOT IN ('loan_disbursement','repayment','interest',
                        'savings','christmas','benevolent')
 AND EXISTS (SELECT 1 FROM fiscal_periods p WHERE p.status='closed'
              AND NEW.entry_date BETWEEN p.start_date AND p.end_date)
BEGIN
    SELECT RAISE(ABORT, 'that date falls in a closed fiscal period');
END;

-- NOTE for future migrations: SQLite's table rebuild drops triggers with the
-- table. A later migration that rebuilds any of these five tables must recreate
-- these thirteen alongside the gl_outbox triggers (005) and the audit ones
-- (007). Phase 9 will rebuild four of them.
