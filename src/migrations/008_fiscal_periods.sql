-- 008: a register of fiscal periods.
--
-- The application has always had a financial year — `fy_start_month` decides
-- where quarters fall, which report covers what, and when a retired member
-- drops off. It has never had anywhere to record that a period is *finished*:
-- that the figures published for it are the figures, and nothing should move
-- underneath them any more.
--
-- This migration adds the register only. Nothing is enforced by it and no
-- period is closed by it, so a journal that applies this behaves exactly as it
-- did before. Migration 009 turns a closed period into a lock; keeping the two
-- apart means the vocabulary can be reviewed against real dates before anything
-- starts refusing writes.
--
-- Periods are *declared*, not derived. `fy_start_month` can be changed in
-- Settings, and deriving period boundaries from it would silently redraw the
-- boundaries of periods that had already been closed. What a period covered
-- when it was closed is a fact about the past, so it is stored.

CREATE TABLE IF NOT EXISTS fiscal_periods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT    NOT NULL UNIQUE,
    start_date TEXT    NOT NULL,
    end_date   TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    closed_at  TEXT,
    closed_by  TEXT,
    note       TEXT,
    CHECK (end_date >= start_date),
    CHECK (date(start_date) IS NOT NULL AND date(end_date) IS NOT NULL),
    -- A closed period records when it was closed; an open one has not been.
    CHECK ((status = 'closed' AND closed_at IS NOT NULL)
        OR (status = 'open'   AND closed_at IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_fiscal_periods_range ON fiscal_periods(start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_fiscal_periods_status ON fiscal_periods(status);

-- Overlapping periods would make "is this date in a closed period?" ambiguous,
-- and that question is the whole point of the table. SQL cannot express
-- non-overlap as a CHECK, so it is a trigger on both write paths.
DROP TRIGGER IF EXISTS trg_fiscal_periods_no_overlap_ins;
CREATE TRIGGER trg_fiscal_periods_no_overlap_ins
BEFORE INSERT ON fiscal_periods
WHEN EXISTS (SELECT 1 FROM fiscal_periods p
              WHERE p.start_date <= NEW.end_date
                AND p.end_date   >= NEW.start_date)
BEGIN
    SELECT RAISE(ABORT, 'fiscal periods may not overlap');
END;

DROP TRIGGER IF EXISTS trg_fiscal_periods_no_overlap_upd;
CREATE TRIGGER trg_fiscal_periods_no_overlap_upd
BEFORE UPDATE OF start_date, end_date ON fiscal_periods
WHEN EXISTS (SELECT 1 FROM fiscal_periods p
              WHERE p.id <> OLD.id
                AND p.start_date <= NEW.end_date
                AND p.end_date   >= NEW.start_date)
BEGIN
    SELECT RAISE(ABORT, 'fiscal periods may not overlap');
END;

-- A closed period's dates are a statement about which figures were published.
-- Reopening it is a deliberate act with its own audit trail (see
-- FiscalPeriodService.reopen); silently sliding its boundaries is not.
DROP TRIGGER IF EXISTS trg_fiscal_periods_closed_range_frozen;
CREATE TRIGGER trg_fiscal_periods_closed_range_frozen
BEFORE UPDATE OF start_date, end_date, label ON fiscal_periods
WHEN OLD.status = 'closed'
BEGIN
    SELECT RAISE(ABORT, 'a closed period cannot be redrawn — reopen it first');
END;
