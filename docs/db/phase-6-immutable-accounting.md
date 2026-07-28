# Phase 6 — Immutable accounting

| Sub-phase | Status | What it does |
|---|---|---|
| **6a** | **done** | Migration 006: authored journal entries cannot be edited or deleted |
| **6b** | **done** | Migration 007: money rows get a before-image audit trail |

## What "immutable" can honestly mean here

The obvious reading — *no row in `journal_entries` is ever updated or deleted* —
is wrong for this system, and the reason is worth stating before the code.

The GL holds two kinds of entry:

* **authored** — `manual`, `migration`, `reversal`. Somebody decided these. They
  are the record, and the record does not get rewritten.
* **projected** — the six subledger sources. These are derived data: a
  restatement of `ledger`, `savings`, `christmas_savings` and
  `benevolent_ledger` in double-entry form. Deleting and re-deriving one is
  recomputing a view, not rewriting the books.

Freezing the projected slice would freeze the projection. Its integrity comes
from a different property — being reproducible from its source — which Phase 5
made cheap and `src.integrity` now checks on every run.

So immutability belongs to the authored record, and the *subledger* is where the
guarantee that actually protects the books has to live. That is the whole shape
of this phase: 6a freezes what was authored, 6b makes every change to what was
derived-from visible.

## 6a — authored entries are frozen

`reverse_entry` has always documented the rule ("Corrections never edit or
delete a posted entry; they post the inverse") and the Journal Register enforces
it in the dialog. It had never been enforced anywhere a statement could reach
the table.

Migration 006 adds four triggers rejecting `UPDATE` of accounting substance and
`DELETE` on `journal_entries` and `journal_lines` whose source is not projected.
Verified against the production journal — all five mutation routes blocked:

```
UPDATE memo    blocked
UPDATE date    blocked
UPDATE source  blocked
DELETE entry   blocked
UPDATE line    blocked
DELETE line    blocked
```

The `UPDATE` trigger is scoped with `UPDATE OF` and deliberately leaves `status`
and `reversal_of` writable. Recording that an entry has been reversed is the
audit trail being written, not the entry being changed, and `reverse_entry`
needs both.

### A bug the rule exposed

`reverse_entry` would happily reverse a **projected** entry. The reversal is
authored and survives; the original is re-derived on the next rebuild. The pair
stops netting to zero and the correction silently doubles. The Journal Register
already refused it — but the rule lived in a dialog, so any other caller could
do it. It now raises `ImmutableEntryError`, as does reversing an entry that has
already been reversed.

The triggers hard-code the projected sources, because a trigger cannot read a
Python constant. `test_projected_sources_match_the_trigger` fails if that list
and `GLService._AUTO_SOURCES` ever drift apart.

## 6b — the money tables get a before-image

On its own, 6a is theatre. The GL is a projection, so the books say whatever the
subledgers say; rewriting a ledger row silently restates the trial balance for
whatever period it falls in.

`audit_log` already existed, but covered only members and loans and only their
existence. Nothing about money, and no before-image — so "this row was changed"
could never be answered with "from what?".

Migration 007 widens it (a `CHECK` change, so the table is rebuilt and the five
existing triggers are dropped and recreated unchanged), adds `before_image`, and
installs eight triggers recording every edit and deletion on the four money
tables.

Triggers rather than a repository hook, for the same reason as migration 005:
subledger rows are updated from roughly twenty call sites across
`src/db/ledger.py` and `src/db/fund.py`, plus the balance recalculators and the
importer. A hook would have to be added at each and would still miss a repair
script.

**Deletion is the case that motivated this.** The Phase 1 review found
`delete_individual` destroying a member's ledger rows with nothing recording it.
Migration 004 made that a declarative cascade, and SQLite fires child `DELETE`
triggers on a cascade — so every row a cascade removes now leaves its full
before-image behind. Verified:

```
delete_individual  ->  individual DELETE 1, loan DELETE 1,
                       ledger DELETE 1 (+before-image), savings DELETE 1
```

`UPDATE` triggers are scoped with `UPDATE OF` to the columns that carry money —
the same set the `gl_outbox` triggers watch. A running-balance recalculation
rewrites `balance` on thousands of rows and changes nobody's position; it is
bookkeeping, not a restatement, and auditing it would bury the real events.

`AuditRepository.money_changes()` reads the trail back: every amount, date or
event type someone altered or removed after it had been posted, each with what
it said before.

### Applied to the production journal

```
migration 006 : trial balance unchanged, still balanced, sync 0.004s
migration 007 : 34 audit rows preserved, row counts unchanged
                13 audit triggers, 12 gl_outbox, 4 immutability
integrity_check ok | foreign_key_check clean
```

### Rollback

Both are additive to behaviour, not to data. For 006, `DROP` the four
`trg_journal_*_immutable_*` triggers — no columns or rows change. For 007,
restore `loan_master.pre-phase6b-20260728-160007.db.bak`; the table rebuild is
the only reason a code-only revert is not enough, and even then the extra
column and triggers are harmless if left in place.

### What Phase 6 deliberately does not do

An edit to a *prior period* is still permitted — it is now recorded, but nothing
refuses it. Refusing it needs a notion of a closed period, which is Phase 7. The
audit trail is the evidence that makes that phase's cut-off decisions
reviewable, which is why it comes first.

### Note for future migrations

SQLite's table rebuild drops triggers with the table. A later migration that
rebuilds `individuals`, `loans`, `ledger`, `savings`, `christmas_savings` or
`benevolent_ledger` must recreate the audit triggers as well as the `gl_outbox`
ones from 005. Phase 9 (integer money) will rebuild four of them.
