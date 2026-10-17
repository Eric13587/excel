-- =============================================================================
-- 13 · savings_suspensions
-- =============================================================================
-- Months a member's monthly savings contribution is paused, as
-- loan_suspensions pauses a loan's deductions. A span runs from the first day
-- of its first month up to, not including, the first day of the month the
-- contributions resume in. `suspend_until` is when they were meant to resume
-- and `resumed_date` when they do: the same date for a suspension that runs
-- its course, an earlier one for one ended early. A suspension "until further
-- notice" has neither, and is the one kind left 'active'.
--
-- LoanMaster 1.x does not know this table and leaves it alone, so a journal
-- at this version still opens in 1.x — which then takes savings deposits in
-- suspended months as it always did.
-- =============================================================================

CREATE TABLE savings_suspensions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_id INTEGER NOT NULL REFERENCES individuals(id)
                          ON UPDATE CASCADE ON DELETE CASCADE,
    start_date    TEXT    NOT NULL
                          CHECK (start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'),
    suspend_until TEXT    CHECK (suspend_until IS NULL OR suspend_until > start_date),
    resumed_date  TEXT    CHECK (resumed_date IS NULL OR resumed_date > start_date),
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','resumed')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK ((status = 'active'  AND resumed_date IS NULL)
        OR (status = 'resumed' AND resumed_date IS NOT NULL))
);

CREATE INDEX idx_savings_suspensions_individual ON savings_suspensions(individual_id, start_date);

-- At most one span per member is still open.
CREATE UNIQUE INDEX idx_savings_suspensions_open ON savings_suspensions(individual_id)
    WHERE resumed_date IS NULL AND status = 'active';
