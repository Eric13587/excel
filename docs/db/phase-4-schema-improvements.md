# Phase 4 — Schema Improvements

Bringing the journal to the strict schema in
[`proposed/004_strict_schema.sql`](proposed/004_strict_schema.sql): `NOT NULL`,
`CHECK` domains, declarative `ON DELETE CASCADE`, and `ledger.loan_pk`.

| Sub-phase | Status | Scope |
|---|---|---|
| **4a** | **done** | Import normalisation — the prerequisite for any constraint |
| **4b** | **done** | `loans.duration_months` / `interest_rate`, replacing the regex parse |
| **4c** | **done** | Migration 004: the strict-schema table rebuild |

---

## 4a — import normalisation

### Why this had to come first

Phase 1 deliberately left `NOT NULL` and `CHECK` out, because
`import_selected_data` copies values verbatim from a journal the application
did not write. That was a prediction; here is the measurement. A legacy source
carrying nine classes of bad value imported with **`status: success` and no
errors at all**:

| Value in the source | What landed |
|---|---|
| member with a blank name | imported |
| loan with `start_date` NULL | imported |
| `status = 'active'` (lower case) | imported |
| `event_type = 'Loan Disbursed'` | imported |
| `date = '01/02/2026'` | imported |
| ledger row with both `added` and `deducted` | imported |
| `is_edited = 5857` | imported |
| `transaction_type = 'Contribution'` on savings | imported |
| savings `amount = -50` | imported |

Every one would be rejected by migration 004. More to the point, every one is
already wrong: a two-sided ledger row is counted twice by the balance rules, an
unknown `event_type` has no branch in the recalculator, and `'01/02/2026'`
sorts before every ISO date in the same column.

### Two policies

`src/db/import_rules.py` applies one of two treatments per field.

**Repair** where the intent is unambiguous — trim a name, title-case a status,
map a known synonym (`Loan Disbursed` → `Loan Issued`, `Contribution` →
`Deposit` on savings), collapse a nonzero edit-lock to `1`, recover a missing
loan start date from that loan's earliest ledger row. Every repair is reported.

**Reject** where it is not, with a reason:

* an unknown `event_type` — it cannot be replayed into a balance, so importing
  it would silently skew the member's position;
* an ambiguous date — `01/02/2026` is 1 February or 2 January depending on who
  wrote it, and guessing puts a transaction in the wrong month;
* a two-sided ledger row, a negative amount, a nameless member, a loan with no
  recoverable start date.

Rejections are counted in a new `rejected` stat and listed in the import's
errors. **Nothing is dropped silently** — that is the whole point.

### Result

The same legacy source now imports as:

```
stats  : {... 'individuals': 1, 'loans': 1, 'ledger': 1, 'savings': 1, 'rejected': 4}
errors :
   - name '  Legacy Member  ' trimmed to 'Legacy Member'
   - Skipped a member: member has no name
   - loan L-001: start date None -> 2026-01-01
   - event type 'Loan Disbursed' -> 'Loan Issued'
   - Skipped a transaction: ledger row has an unreadable date ('01/02/2026')
   - Skipped a transaction: Repayment row on 2026-03-01 both adds (50.0) and deducts (100.0)
   - savings type 'Contribution' -> 'Deposit'
   - Skipped a savings row: savings row on 2026-02-01 has a negative amount (-50.0)

violations the strict schema would reject: 0
```

Rejection is not over-eager: the good and the fixable still arrive, including
the loan whose start date was recovered from its own ledger.

One subtlety found while building it: the fallback originally took
`MIN(date)` over the loan's ledger rows, which returns the *malformed* date
when a column mixes `2026-01-01` and `01/02/2026`, because the comparison is
lexicographic. It is now restricted to ISO-shaped values.

### Verification

* 402 → 425 tests (23 added), `ruff` clean.
* The well-formed import differential against the previous commit is identical
  apart from the new `rejected: 0` key in the stats dict.
* An end-to-end test asserts a legacy import leaves **zero** rows that
  migration 004 would reject, and a second asserts the repairable rows still
  land.

### Rollback

`git revert` the 4a commit. No schema or data change; imports return to copying
values verbatim.

---

## 4c — the strict schema

Migration 004 rebuilds fourteen tables to add `NOT NULL`, `CHECK` domains,
declarative `ON DELETE CASCADE`, and `ledger.loan_pk` as a real foreign key.
The cascade triggers from migration 001 are dropped — they were the interim
mechanism for the same guarantee, and keeping both would delete children twice.

The runner gained a header directive, `-- pragma: foreign_keys=off`, because
SQLite's table-rebuild procedure needs it and `PRAGMA foreign_keys` is a silent
no-op inside a transaction. Such a migration is verified with
`PRAGMA foreign_key_check` **before** it commits, so a rebuild that would leave
dangling references is rolled back rather than persisted.

### Applied to the production journal

```
migration : applied 004_strict_schema | 0.161s
row counts identical : True   (all 15 tables)
integrity_check      : ok
foreign_key_check    : clean
loan_pk backfilled   : 0 nulls
cascade triggers     : dropped
control accounts     : all four reconcile, unchanged
```

The only content change is `pf_no`/`id_no` moving from `NULL` to `''` on 85
members, forced by the new `NOT NULL`. Nothing distinguishes the two: the
partial unique indexes exclude both, the owner lookup returns early on a blank,
and the application already writes `''` for every member it creates.

### Two things the constraints caught

**A gap in the migration itself.** The first rehearsal backfilled `loan_pk` for
existing rows, but new rows written afterwards left it `NULL` — the foreign key
would have been correct once and then rotted. It is now filled by a trigger
rather than by the insert statement, so it holds for every code path including
bulk catch-up and the importer. Caught by the smoke test, not by the row counts.

**A contradiction in the application.** `CHECK (is_edited IN (0,1))` was in the
draft and had to be removed. The column does two jobs: it is an edit lock, and
when above 1 it also carries the payment amount the user intended, which
`BalanceRecalculator` reads back as a replay anchor (`balance_calculator.py`,
"Hysteresis Fix"). Constraining it would change how an edited repayment
replays.

It is also already self-defeating: `create_tables` normalises anything outside
0/1 back to 1 on every open, so the anchor survives only until the application
restarts. Two parts of the codebase disagree about what the column means.
Resolving it means splitting the two jobs into `is_edited` plus an
`edited_amount` column — a schema change with a behavioural decision attached,
so it is **a migration of its own**, not a rider on this one. (It was
originally earmarked as 005; that number went to the GL change queue, and the
split is still unbuilt — see docs/db/outstanding.md.) The integrity report now
counts the rows carrying an anchor so the change can be sized.

### Test fallout

Sixteen tests failed, and every one was a fixture relying on the absence of a
constraint — minimal loan inserts with no `start_date`, an `event_type` of
`'Loan Disbursed'`, a savings row with a `NULL` member. They are fixed rather
than relaxed, and the two that model a *pre-004* journal (orphan repair, and
the atomicity of a blocked delete) now reconstruct the older table shape
explicitly instead of pretending the current one allows it.

The suite runs in 13 s, up from 5 s: every test journal now performs the full
14-table rebuild on creation. Building a new journal directly from `schema.sql`
instead of creating the legacy shape and migrating it would remove that, and is
the natural next step for `create_tables`.

### Rollback

Restore the backup named in the commit message. This rewrites every table, so
there is no cheap undo — which is why the pre-flight guards abort before
anything is touched.
