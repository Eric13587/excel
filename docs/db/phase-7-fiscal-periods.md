# Phase 7 — Fiscal periods

| Sub-phase | Status | What it does |
|---|---|---|
| **7a** | **done** | Migration 008: the period register, declaring and closing |
| **7b** | **done** | Migration 009: a closed period becomes a lock |
| **7c** | **done** | Treasury gains a Fiscal Periods tab |

## Where the lock has to sit

Phase 6 ended with an edit to a prior period being *recorded* but not *refused*.
Refusing it is what a closed period means — and the obvious implementation,
guarding `journal_entries`, is the wrong one.

The GL is a projection derived from the subledgers **by date**. A back-dated
edit to a `ledger` row walks straight into a closed period the next time the
change queue drains: the journal it produces is new, but the restatement already
happened. Guarding the journals alone would be theatre in exactly the way 6a
would have been without 6b.

So the lock is on the subledger. Thirteen triggers cover insert, update and
delete on `ledger`, `savings`, `christmas_savings` and `benevolent_ledger`, plus
one on `journal_entries`.

### The exemption that makes it survivable

`sync(full=True)` deletes every projected journal and re-posts it, including
entries dated inside closed periods. A blanket guard on `journal_entries` would
make a full rebuild impossible on any journal with a closed period — **the
recovery path would be the first casualty of the safety feature.**

The journal guard therefore covers *authored* entries only, the same split
migration 006 uses. A projected entry inside a closed period is safe because its
source row is frozen: re-deriving it can only reproduce what was already there.
`test_a_full_rebuild_still_works_with_a_closed_period` holds that line.

## 7a — the register

Periods are **declared, not derived**. `fy_start_month` is a setting and can be
changed; deriving boundaries from it would silently redraw periods that had
already been closed. `suggest_periods()` proposes ranges from the current
setting and the user commits them — after that the stored dates are the truth.

Non-overlap is a trigger rather than a `CHECK`, because SQL cannot express it and
"is this date in a closed period?" must have exactly one answer. A closed
period's dates and label are frozen too: reopening is a deliberate act with its
own trail, silently sliding boundaries is not.

**Closing is refused, not warned about**, when the figures are still moving or
still wrong:

* an earlier period is still open,
* the change queue has undrained subledger changes,
* the trial balance does not balance as at the period end.

`closing_blockers()` lists the reasons before the attempt. `close(force=True)`
exists for a break that is known and accepted, and writes the accepted breaks
into the period's note — never silent. `reopen()` requires a reason and keeps
the original closing timestamp on the record.

Verified against the production setting (November): FY 2025/26 runs 2025-11-01
to 2026-10-31 and the four suggested quarters tile it exactly.

## 7b — the lock

Both sides of a date change are checked. Moving a row *out* of a closed period
changes its figures just as surely as moving one in.

`UPDATE` triggers are scoped with `UPDATE OF` to the columns that carry money,
matching `gl_outbox` (005) and the audit trail (007) — so the balance
recalculators keep rewriting `balance` across every row untouched.

Verified on a seeded journal with Q1 closed:

| Attempt | Result |
|---|---|
| insert a ledger row inside Q1 | blocked |
| edit an amount inside Q1 | blocked |
| delete a row inside Q1 | blocked |
| move a row out of Q1 | blocked |
| hand-post a journal into Q1 | blocked |
| recalculate balances (`balance` only) | allowed |
| post activity in an open period | allowed |
| `sync(full=True)` | allowed, trial balance identical |

`DatabaseManager.transaction` translates a trigger abort into
`PeriodClosedError` in one place, so every write path yields an exception that
says what to do rather than a generic transaction failure. `post_journal` does
the same for itself, since posting does not go through that wrapper. The
contract between migration and code is the marker phrase `PERIOD_LOCK_MARKER`.

**The triggers are inert until somebody closes something**, which is how 008
ships and how the production journal stands today: 0 closed periods, nothing
frozen, no behaviour change from applying either migration.

## 7c — reachable

An accounting control nobody can operate is not a control. Treasury gains a
**Fiscal Periods** tab: the register, *Add This Year's Quarters*, *Close
Selected Period* and *Reopen Selected Period*. Closing shows the blockers and
asks before force-closing; reopening demands a reason.

Smoke-tested offscreen against a copy of the production journal — declare four
quarters, close Q1, confirm a write into it is refused with the actionable
message, reopen, confirm the lock lifts.

### Applied to the production journal

```
migration 008 : register created, empty
migration 009 : 13 lock triggers, 0 closed periods — inert
trial balance : unchanged, still balanced
integrity_check ok | foreign_key_check clean
```

No period has been closed. That is a decision about published figures, not one
a migration should make.

### Rollback

> **The migration backups have been deleted** (2026-07-28, at the user's
> request). The `.bak` file named below no longer exists, so the restore
> route described here is gone. What remains is the code-level rollback in
> the same section, plus the app's own `backups/` rotation, which holds only
> journals taken *after* this point.


`DROP` the 13 `trg_period_lock_*` triggers to disable the lock, and
`DROP TABLE fiscal_periods` plus its three triggers to remove the register. With
no closed periods, a code-only revert changes nothing observable. Backups:
`loan_master.pre-phase7a-20260728-160713.db.bak`,
`loan_master.pre-phase7b-20260728-161120.db.bak`.

### Note for future migrations

SQLite's table rebuild drops triggers with the table. A later migration that
rebuilds any of the four money tables must recreate these thirteen alongside the
`gl_outbox` triggers (005) and the audit ones (007). Phase 9 will rebuild all
four.
