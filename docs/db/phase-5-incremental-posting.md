# Phase 5 — Incremental journal posting

| Sub-phase | Status | What it does |
|---|---|---|
| **5a** | **done** | `project_rows` — re-derive one subledger row's journal correctly |
| **5b** | **done** | Migration 005: a change queue, so `sync` costs what changed |

## The problem

The general ledger is a projection. Six sources — `loan_disbursement`,
`repayment`, `interest`, `savings`, `christmas`, `benevolent` — are derived
from the member subledgers and carry a `source_ref` back to the row they came
from. Manual, migration and reversal entries are authored, not derived, and are
never touched.

Keeping a projection current means handling three events. Posting handled one:

| Event | Before Phase 5 |
|---|---|
| a row is added | posted by the write-path hook, or by the next backfill |
| a row is **edited** | `post_journal` sees the `source_ref` and returns the *existing* entry — the stale journal stands |
| a row is **deleted** | nothing looks for entries without a source — the journal stands forever |

The answer was `rebuild_auto_journals`: delete all six sources and re-derive
everything. Correct from any starting state, and the reason `sync()` cost
**1.48 s on the production journal** — 5,344 entries re-derived from 2,192
ledger rows — on every Treasury open and every statement, whether or not
anything had changed. That figure is linear in journal size, so it is a wall,
not an inconvenience.

## 5a — make one row projectable

`project_rows(table, row_ids)` withdraws each row's existing entry and
re-derives it from the row's current state. One path covers all three events: a
new row has nothing to withdraw, an edited row is replaced rather than kept, and
a deleted row leaves the withdrawal standing.

The withdrawal matches on `source_ref` alone within the projected sources,
deliberately **not** on `(source, source_ref)`. A ledger row's source is decided
by its event type, so editing `Repayment` into `Interest Earned` moves its entry
from the `repayment` source to `interest`. A lookup pinned to the old source
misses the old entry and the re-post leaves a duplicate behind — reachable today
through the write-path hooks, and masked only because the wipe runs first.
`test_project_rows_follows_a_row_that_changes_source` covers it.

The write-path hooks now delegate here, so re-firing one for a row that has
since been edited corrects its entry instead of leaving the stale one in place.

## 5b — record what changed

`project_rows` needs the list of changed rows, and the Python hooks cannot
supply it. They fire on insert only, and three things write subledger rows
without passing through them at all: the balance recalculators, the importer,
and `fund_add_transaction` — whose docstring has said "fixed in Phase 5" since
Phase 1.

Migration 005 puts the record in the database instead. `gl_outbox` is a queue of
`(source_table, row_id)`, filled by `AFTER INSERT / UPDATE / DELETE` triggers on
`ledger`, `savings`, `christmas_savings` and `benevolent_ledger`. A trigger sees
every write regardless of the code path — the same argument that made 004 use
one for `loan_pk`.

Three decisions worth stating:

**The queue does not record the kind of change.** `project_rows` decides insert
vs update vs delete by looking at whether the row still exists, so a row touched
five times collapses to one re-projection, and a queue entry can never disagree
with the data it describes.

**`UPDATE OF` names the columns the projection reads.** A running-balance
recalculation rewrites `balance` on thousands of rows and changes no journal at
all; scoping the trigger keeps that out of the queue entirely. Verified:
`UPDATE savings SET balance = balance + 1` queues nothing, `SET amount = 250`
queues one.

**An empty queue is not evidence.** It cannot distinguish "nothing changed"
from "never projected". A `gl_projection_established` setting, written only by a
full rebuild, is what makes the queue trustworthy: until it is set, `sync()`
rebuilds. So the first sync after this migration establishes the baseline, and
every sync after it drains. The same rule is the recovery mechanism — clearing
the marker forces a rebuild.

Outbox semantics are the standard ones. The queue is emptied only up to the id
that was read and only once the projection commits, so a change arriving
mid-drain keeps its place, and a failed drain is retried rather than silently
dropped.

### A capability that had to be replaced

Opening Treasury used to re-derive everything, which doubled as the answer to
"the books look wrong". Draining removes that, so the screen now has a **Rebuild
Ledger** button calling `sync(full=True)`. Leaving the escape hatch reachable
only from a settings row would have been a real loss of recoverability.

### Results

On the production journal:

```
baseline sync (rebuild)     1.48s
next sync (nothing changed) 0.000s
sync after one new row      0.002s
forced rebuild              1.47s
trial balance               unchanged; incremental == full rebuild, exactly
integrity_check ok | foreign_key_check clean | queue drained
```

`integrity` gained a `projection` category reporting the three states that
matter: whether the baseline is established, whether the queue is drained, and
whether any projected entry has outlived its source row. The opposite drift — a
subledger row with no journal — is what the control-account comparisons already
measure, and checking it here would mean duplicating the event vocabulary that
decides which rows post at all.

### Note for future migrations

SQLite's table-rebuild procedure drops triggers with the table. Any later
migration that rebuilds `ledger`, `savings`, `christmas_savings` or
`benevolent_ledger` must recreate these twelve triggers, exactly as 004 had to
recreate the ones 001 installed. Phase 9 (integer money) will rebuild all four.

### Rollback

5a is code only — revert the commit.

For 5b, restore the backup named in the commit message. Reverting the code
alone is also safe and needs no data change: the `gl_outbox` table and its
triggers are additive and simply go unread, and the old `sync()` rebuilds
unconditionally. Dropping the twelve `trg_outbox_*` triggers stops the queue
growing if the revert is meant to be permanent.
