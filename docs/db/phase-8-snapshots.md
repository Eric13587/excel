# Phase 8 — Period-end snapshots

| Sub-phase | Status | What it does |
|---|---|---|
| **8a** | **done** | Migration 010: snapshots written on close, discarded on reopen |
| **8b** | **done** | Balances and the trial balance read through them |

## The measurement first

Every statement figure is an aggregate over `journal_lines` from the beginning
of time. On the production journal's 11,728 lines that is 7–17 ms — invisible.
It does not stay invisible. On a synthetic journal of 2,000,000 lines:

| Query | Time |
|---|---|
| trial balance, all time | 861 ms |
| trial balance as at a date | 1,790 ms |
| one account as at a date | 125 ms |

A balance sheet runs several of those, and the cost is linear, so ten times the
data is ten times the wait.

## Why this is a record, not a cache

A cache over mutable data is an invalidation problem, and building one here
before Phase 7 would have been the wrong move — every snapshot would have
needed rechecking against a subledger that could change underneath it.

It is not one now. **A snapshot is only ever taken of a closed period, and a
closed period cannot change** — migration 009 refuses every write into one. What
is stored is settled by definition.

That is precisely why Phase 8 follows Phase 7 rather than preceding it.

Open periods deliberately get no snapshot. Their figures can still move, so
anything stored for them would need invalidating, which is the problem this
design avoids rather than solves.

## What is stored, and why cumulative

`account_snapshots` holds per-account totals **from the beginning of the journal
to the period end**, not the period's own movement. A reader asks "what was the
balance as at this date", and a cumulative figure answers it with one lookup,
whereas per-period movements would have to be summed all the way back.

`as_of` duplicates the period's `end_date` so a read never joins to
`fiscal_periods`. The two cannot drift: a closed period's range is frozen by
migration 008.

## Reading

For a date at or after a snapshot: take the snapshot as the baseline and sum
only the movement since. Otherwise fall back to the full aggregate — which is
every journal until a period is closed, including the production one today.

Measured on a 43,200-line journal with five years closed, reading as at
2024-12-31:

| | before | after | |
|---|---|---|---|
| trial balance | 75.20 ms | 4.69 ms | **16.0×** |
| one account balance | 4.46 ms | 4.06 ms | 1.1× |

Results identical in both cases — that equivalence is what
`test_reading_through_a_snapshot_matches_recomputation` pins.

The single-account figure barely moves, and that is worth stating plainly: that
lookup was already served by `idx_journal_lines_account`, and the delta still
spans a year of movement. Snapshots pay off where the work is proportional to
the whole journal — the trial balance, and the balance sheet built on it.

## Reopening

Reopening a period discards its snapshot **and every later one**. The totals are
cumulative, so a change inside the reopened period moves every figure that
includes it. Reads then fall back to computing, which is correct but slower for
the affected periods.

`GLService.rebuild_snapshots()` is the counterpart to `sync(full=True)`: it
re-derives the snapshot of every closed period. It is needed after a reopen and
re-close, because closing one period does not restore the later snapshots that
the reopen discarded.

## The guarantee

Reading stored figures instead of computing them is only sound if the stored
figures still match. `src.integrity` now recomputes every snapshot row and
compares — and reports a mismatch as a **failure**, not a warning. A stored
figure that disagrees with the journal is not a nuance.

### Applied to the production journal

```
migration 010 : table created, empty
closed periods: 0 -> no snapshots -> every read still computes
trial balance : balanced; 1100 = 8,507,484.18, 2000 = 18,758,789.00
integrity_check ok | foreign_key_check clean | 35 checks, 0 failed
```

Zero behaviour change, because nothing is closed. The speed arrives the first
time a period is closed.

**Note on the 1100 figure.** Earlier phases recorded 8,513,734.18. The
difference is two ledger rows added by ordinary use of the application between
sessions — a monthly interest accrual of 938.00 and its repayment of 7,188.00
(6,250.00 principal + 938.00 interest) on one member's loan L-002, both tagged
"(Auto)". `8,513,734.18 − 6,250.00 = 8,507,484.18`. GL and `loans.balance` moved
together throughout, which is incidental evidence that the Phase 5 write-path
posting keeps the books in step under real usage.

### Rollback

> **The migration backups have been deleted** (2026-07-28, at the user's
> request). The `.bak` file named below no longer exists, so the restore
> route described here is gone. What remains is the code-level rollback in
> the same section, plus the app's own `backups/` rotation, which holds only
> journals taken *after* this point.


`DROP TABLE account_snapshots`, or restore
`loan_master.pre-phase8-20260728-162132.db.bak`. Reverting the code alone is
safe: with no snapshots stored, every read already takes the fallback path.
