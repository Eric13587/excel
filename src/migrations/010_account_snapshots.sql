-- 010: period-end account snapshots.
--
-- Every statement figure is an aggregate over journal_lines from the beginning
-- of time. That is correct and, on the production journal's 11,728 lines,
-- instant. It does not stay instant. Measured on a synthetic journal of
-- 2,000,000 lines:
--
--     trial balance, all time                     861 ms
--     trial balance as at a date                1,790 ms
--     one account as at a date                    125 ms
--
-- A balance sheet runs several of those, and the cost is linear in the journal,
-- so ten times the data is ten times the wait.
--
-- WHY THIS IS NOT A CACHE
--
-- A cache over mutable data is an invalidation problem, and inventing one here
-- would have been the wrong move before Phase 7. It is not one now: a snapshot
-- is only ever taken of a *closed* period, and a closed period cannot change —
-- migration 009 refuses every write into it. The snapshot is a record of a
-- fact, not a guess that has to be rechecked.
--
-- That is the whole reason Phase 8 follows Phase 7 rather than preceding it.
--
-- Open periods deliberately get no snapshot. Their figures can still move, so
-- anything stored for them would need invalidating, which is exactly what this
-- design avoids.
--
-- WHAT IS STORED
--
-- Cumulative totals from the beginning of the journal to the period end, per
-- account — not the period's own activity. A reader wants "the balance as at
-- this date", and a cumulative figure answers that with one lookup, whereas
-- per-period movements would have to be summed back to the start.
--
-- `as_of` duplicates the period's end_date so a read never has to join to
-- fiscal_periods. It is written from the period and never edited; the period's
-- range is frozen while closed (migration 008), so the two cannot drift.

CREATE TABLE IF NOT EXISTS account_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period_id       INTEGER NOT NULL REFERENCES fiscal_periods(id) ON DELETE CASCADE,
    account_code    TEXT    NOT NULL REFERENCES chart_of_accounts(code),
    as_of           TEXT    NOT NULL,
    debit_total     REAL    NOT NULL,
    credit_total    REAL    NOT NULL,
    closing_balance REAL    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period_id, account_code)
);

-- The read path: "latest snapshot at or before this date, for this account".
CREATE INDEX IF NOT EXISTS idx_account_snapshots_as_of
    ON account_snapshots(as_of, account_code);
