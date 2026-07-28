# Phase 2 — Repository Layer

Moving 302 production SQL statements out of services, views and the 2,557-line
`DatabaseManager` into one repository per aggregate, so every statement touching
a table lives in exactly one place.

**No behavioural changes. No schema changes.** The suite must stay green at
every step, so `DatabaseManager` keeps its public API and delegates to each
repository as it lands.

| Sub-phase | Status | Scope | SQL statements |
|---|---|---|---|
| **2a** | **done** | `Repository` base, `SettingsRepository`, wiring | 3 |
| **2b** | **done** | `MemberRepository` (`individuals`) | 21 of 53 |
| **2d** | **done** | `FundRepository` (`savings`, both funds, enrolment) | 32 of 55 |
| **2c-i** | **done** | `LoanRepository` (`loans`, `loan_suspensions`) | 40 of 56 |
| **2c-ii** | **done** | `LedgerRepository` (`ledger`) | 45 of 67 |
| **2e** | **done** | `JournalRepository` (COA, entries, lines) | 30 of 30 |
| **2f-i** | **done** | Repair, import history, `SourceJournal` | 24 |
| **2f-ii** | **done** | Import path, audit read, composition root | 33 |

Sequencing rationale is in [repository-layer.md](repository-layer.md).

---

## 2a — foundation and settings

Settings first: smallest surface (3 statements), the largest number of call
sites (21), and it carried a real defect to fix.

### What was wrong

Every value in `settings` is TEXT, so each of the 21 readers also did its own
parsing and carried its own inline default:

```python
self.db.get_setting("deduct_same_month", "false").lower() == "true"   # x4, three files
int(self.db.get_setting("christmas_unlock_month", "12"))              # with its own try/except
float(self.db.get_setting("default_savings_increment", "2500"))       # x2
```

A schema with no schema — and it had already diverged. `fy_start_month`
defaulted to `'November'` in the Settings dialog and `'January'` in three places
in `reports.py`, so on a journal where the user never opened Settings the UI
displayed November while the books were computed on a January financial year
(finding **S1** in [inconsistent-sql.md](inconsistent-sql.md)).

### What changed

* `src/db/base.py` — `Repository`: cursor handling and row mapping, so
  subclasses contain SQL and nothing else. Identifiers that must be
  interpolated go through `_checked_identifier(name, allowed)` against a fixed
  whitelist.
* `src/db/settings.py` — `SettingsRepository`: the three `settings` statements,
  the key names, one `DEFAULTS` table, and typed accessors
  (`db.settings.deduct_same_month` → `bool`).
* `DatabaseManager.get_setting/set_setting` now delegate. Every existing caller
  is unaffected; an explicit `default=` argument still wins, so the old contract
  holds exactly.
* All 21 call sites across `branding`, `theme`, `reports`, `dialogs`,
  `balance_calculator`, `loan_service`, `savings_service`, `christmas_service`
  and `dashboard` now read typed properties. `grep -rn "get_setting("` over
  `src/` returns nothing outside the repository and the delegating shim.

### The one judgement call — and why it went the conservative way

Unifying `fy_start_month` requires picking a value, and the choice moves money.
The first attempt unified on `'November'` (the UI's value, and what `seed.sql`
had). **The existing test suite caught it**:

```
tests/test_fund_reports.py::test_custom_column_ordering_quarterly_savings_report
E   AssertionError: Invalid quarter start month (January).
```

A quarterly report starting 2026-01-01 is valid on a January financial year and
invalid on a November one. Unifying on November would silently shift the
financial year — and therefore every quarterly report — on any journal where
the key was never written.

At the time the default was set to **`'January'`** — the value `reports.py`
already used — so that the refactor changed no figure. It was changed to
`'November'` afterwards as an approved behavioural fix (below). The Settings dialog
now displays January when unset, which is a cosmetic change that makes the UI
agree with what the books were already doing.

The production journal has the key explicitly set to `November`, so it is
unaffected either way — verified after the change: stored `November`, FY index
`11`, unchanged.

> ### Resolved
>
> The default is now **November**, the institution's actual financial year and
> what the production journal stores. See §"Three approved fixes" below.

### Test doubles

`test_theme.py` and `test_branding.py` hand-rolled a `FakeDB` whose `settings`
attribute was a plain dict — which collided with the real `settings` repository
and would have had to reimplement the parsing to stay accurate. They now use
`tests/helpers.py::make_settings`, which builds a **real** `SettingsRepository`
over a throwaway in-memory table. Four lines, and the double cannot drift from
the class it stands in for.

### Verification

* 263 → 283 tests passing (20 added). `ruff` clean.
* Live journal re-read after the change: `fy_start_month` November, FY index 11,
  `deduct_same_month` False, unlock month 12, Excel palette and org name all
  unchanged.

### Rollback

`git revert` the 2a commit. No schema or data change; `DatabaseManager`'s public
API is identical before and after, so nothing outside `src/db/` depends on the
new code.

---

## 2b — MemberRepository

All `individuals` SQL lived in `database.py`; no view or service touched the
table, which made this self-contained. 21 of the 53 statements move — the
member CRUD block plus the retirement operations. The remaining 32 are the
import path (one 456-line method with nested savepoints), the orphan-repair
join over dynamic table names, and trigger DDL. Those are 2f and later.

### Behaviour preserved deliberately

| Kept as-is | Why |
|---|---|
| `get_individuals()` returns raw `SELECT *` tuples | Ten call sites index them positionally (`m[0]`). Dataclasses are a 2f/3 change, once those callers move. |
| `get_individual_name()` falls back to `"Individual {id}"` | A display decision, so it stays on the manager; `members.name_of()` reports absence honestly as `None`. |
| Blank PF/ID numbers are never duplicates | Matches the partial unique indexes on those columns. |
| `update_individual`'s partial semantics | Optional fields are only written when supplied, so a caller passing core details cannot clobber a status or PF number. |

### One strengthened guarantee

`update_individual` issues up to four statements and previously relied on a
single trailing `commit()`. It now runs inside `transaction()`, so a failure
part-way — a PF-number collision against the unique index added in migration
001, say — rolls back the earlier statements instead of leaving a
half-applied edit. `test_update_is_atomic_across_its_statements` pins this.

### Commit convention, now consistent

Repositories **never commit**; the caller owns the transaction boundary. That
is what lets `update_individual` be atomic across four statements. Phase 2a's
`SettingsRepository.set` committed internally (extracted verbatim from
`set_setting`); it no longer does, and `DatabaseManager.set_setting` wraps it.

### Verification

* 283 → 303 tests (20 added). `ruff` clean.
* Read back against a copy of the live journal: `get_individuals()` still a
  `list` of `tuple` (119 rows, first `(6, 'GRACE MBURU')`), `get_individual(23)`
  still a 13-key dict, the `"Individual {id}"` fallback intact, case-insensitive
  name matching and PF-owner lookup unchanged.
* Audit triggers still fire on insert/update/delete through the repository —
  the SQL is byte-identical, only its home changed.

### Rollback

`git revert` the 2b commit. No schema or data change; `DatabaseManager`'s public
API is identical either side.

---

## 2d — FundRepository

Taken before 2c on the reasoning that the `fund_*` methods were already a
generic API over a whitelisted table name — most of a repository already — so
this is the low-risk step, and Phase 3's balance consolidation depends on it
existing.

32 of the 55 statements move. The rest are the GL service's subledger scans
(2e), the import path, and the earliest-record-date queries (2f).

### What changed

* `src/db/fund.py` — one repository for `savings`, `christmas_savings`,
  `benevolent_ledger` and `benevolent_accounts`. `savings` joins the whitelist
  the other two already used, so all three are reached identically.
* **Raw SQL removed from the view layer** (finding D6). `views/ledger.py` — a
  Qt widget — was executing three `savings` statements directly, one of them a
  duplicate of `DatabaseManager.update_savings_transaction`. Because it
  bypassed the manager, the savings GL post-hook never fired for edits made
  through that path.
* Raw SQL also removed from `savings_service`, `christmas_service` and
  `benevolent_service` (batch-revert lookups and a balance write).

### Two defects preserved on purpose

Phase 2 is a refactor, so both survive — but they are now visible in one file
with tests pinning them, which is what the next phases need.

1. **Three running-balance semantics** (finding D1) are now three named methods
   sitting next to each other: `recalc_savings_by_date` (Interest *increases*,
   ordered by date), `recalc_savings_by_id` (Interest *decreases*, ordered by
   id), `recalc_fund` (only Withdrawal decreases, rounded to 2dp). Two tests
   assert the contradictory answers — 900 versus 700 on the same three rows —
   so Phase 3 has an explicit baseline to change.

2. **Only `savings` writes fire the GL post hook.** `fund_add_transaction`
   fires nothing, which is why 12 Christmas deposits sat outside the books
   until a sync was run. `test_only_savings_fires_the_gl_post_hook` pins it for
   Phase 5.

### Verification

* 306 → 324 tests (18 added). `ruff` clean.
* Fund lifecycle against a production copy: savings deposits and both
  recalculators, Christmas deposits with the unlock month, benevolent enrolment
  and catch-up, mass catch-up plus revert for all three funds, GL sync
  (balanced), member delete (FK clean).

### Rollback

`git revert` the 2d commit. No schema or data change.

---

## 2c-i — LoanRepository

40 of the 56 `loans`/`loan_suspensions` statements move. The rest are the
import path and the `create_tables` back-fill, both 2f.

Raw SQL removed from three more modules: `engine.py` (the loan-heal probe and
term refresh), `loan_service.py` (five schedule/state updates) and
`undo_manager.py` (the nine-field loan snapshot restore).

### Member scoping is now in the signatures

`ref` (`L-001`) is unique **per member only**. Every repository method that
takes a ref also takes the member id — `by_ref(member_id, ref)`,
`terms(member_id, ref)`, `set_schedule_by_ref(member_id, ref, ...)` — so the
rule in `CLAUDE.md` is enforced by the type signature rather than by
remembering. `test_set_schedule_by_ref_touches_only_that_member` is a
regression guard for the original bug, where a query keyed on `ref` alone
updated other members' loans.

### Three update keys, now named rather than scattered

Finding **D5**: loans are updated by `id`, by `(id, individual_id)` and by
`(ref, individual_id)` depending on the caller. All three survive, because
changing which rows a statement can reach is a behavioural change. They are now
distinct, documented methods — `set_status`, `set_catch_up_state`,
`set_schedule_by_ref` — so the divergence is visible in one file and can be
consolidated deliberately later.

### The optional-column contract

`set_status` and `set_details` build their SET list dynamically: a `None`
argument means "leave that column alone", while `0` is a real value that gets
written. Three tests pin this, because it is exactly the kind of contract a
refactor silently inverts.

### Verification

* 324 → 342 tests (18 added). `ruff` clean.
* **Differential state comparison.** A 30-operation sequence — member create /
  rename / retire / reinstate, two loans, catch-up, suspend, extend, resume,
  historical suspension, top-up, single deduct, buyoff, savings, Christmas,
  benevolent enrolment and catch-up, both savings recalculators, three mass
  runs with reverts, GL sync — was run against a copy of the production journal
  on the Phase 2d commit and on this one. All twelve tables hash **identically**.
* That comparison also settled a question the smoke test raised: a
  restructure-then-buyoff leaves the loan `Active` with a 0.00 buyoff. It does
  so on both commits, so it is pre-existing, not a regression — see below.

### A latent bug this surfaced (not fixed here)

`restructure_loan` followed by `buyoff_loan` returns 0.00 and leaves the loan
`Active` with `balance` 40,000. The restructure leaves the ledger's
`principal_balance` at 0 while `loans.balance` still carries the amount, so
`buyoff_loan` — which reads the ledger — concludes there is nothing to settle.
Another instance of the cache-versus-ledger divergence that Phase 3 addresses;
recorded here rather than changed, because fixing it changes numbers.

### Rollback

`git revert` the 2c-i commit. No schema or data change.

---

## 2c-ii — LedgerRepository

45 of the 67 `ledger` statements move. The remainder are the import path and
the `is_edited` normalisation in `create_tables` (migrations), plus two
deliberate exclusions below.

Raw SQL removed from six more modules: `balance_calculator` (six writes inside
the smart-replay loop), `loan_service` (six probes and a scoped delete),
`engine` (four heal probes and the unbounded lock clear),
`transaction_manager` (the rate-change rewrite), `undo_manager` (delete and
restore) and `views/ledger.py` (a date edit).

### The interest edit-lock now has one owner (finding D4)

Three statements with three different scopes became one method:

| Caller | Scope | Now |
|---|---|---|
| `unlock_future_interest` | clear, `date > ?` | `set_interest_lock(..., locked=False, from_date=d)` |
| loan-heal | clear, **unbounded** | `set_interest_lock(..., locked=False)` |
| rate change | set, `date >= ?` | `set_accrual_from_date(...)` |

Each caller keeps its own scope — changing them changes which rows a
recalculation may overwrite — but there is now one statement to reason about,
and four tests pin the boundaries, including that an unbounded clear discards
*every* lock (safe only on loans with no legitimate interest edits, which is
why the heal tool checks that first).

### Two things deliberately left outside the repository

* **`create_tables`'s `is_edited` normalisation** belongs to migrations, not
  data access.
* **`DeleteTransactionCommand`** in `undo_manager.py` still holds raw SQL. It
  is dead — exported but never instantiated — and its `INSERT` targets
  `ledger.principal_amount` and `ledger.linked_trans_id`, columns that have
  never existed in any schema generation, so the path raises
  `OperationalError` if reached. Migrating it would make the repository
  advertise columns that do not exist. It should be **deleted**, which is a
  one-line change to `src/services/__init__.py` plus the class — held back
  because removing a public name is not a refactor-phase decision.

### Preserved quirks, now pinned by tests

* `update_core`'s `mark_edited` writes the payment **amount** into `is_edited`
  rather than 1 — the "hysteresis fix" that keeps the user's intended figure
  across a recalculation. `create_tables` normalises it back to 0/1 on the next
  open. `count_edited` therefore tests for *nonzero*, not equality with 1.
* The principal/interest portions are written as a pair or not at all; a single
  portion is silently ignored.
* `earliest_date_*` covers only `ledger` and `savings`, so a member whose whole
  history is Christmas or benevolent has no earliest date (finding D7).

### A coverage gap this exposed

Removing a cursor left a dangling `row[0]` in `ReportGenerator.get_recent_quarters`,
and **the whole suite still passed** — the method has two dashboard call sites
and no test. `ruff` caught it, and there is now a regression test.

### Verification

* 342 → 365 tests (23 added). `ruff` clean.
* **Two differential comparisons** against the 2c-i commit, via a git worktree,
  both on a copy of the production journal:
  1. the 30-operation lifecycle sequence — all twelve tables hash identically;
  2. a sequence targeting exactly what this phase touched — catch-up, a manual
     repayment edit (rate rewrite + lock), smart replay, date-bounded unlock,
     delete/undo/redo, the heal probe, and a full schedule rebuild — identical
     line for line, including the resulting `ledger` and `loans` hashes.

  The second run initially reported "IDENTICAL" while both sides were throwing
  the same `TypeError`. The harness now fails loudly on a traceback; a
  differential that compares two failures is worthless.

### Rollback

`git revert` the 2c-ii commit. No schema or data change.

---

## 2e — JournalRepository

All 30 of `gl_service.py`'s statements move. `GLService` keeps the accounting
logic — posting rules, the balance assertion, statement assembly, the
cash-flow categorisation — and now contains **no SQL at all**. It was the
cleanest split available, because the two concerns were already separable:
every posting helper builds a list of `{account, debit|credit}` dicts and hands
it to one insert.

The four subledger scans the GL projects from went to the repositories that own
those tables, not into `JournalRepository`:
`ledger.gl_projection_rows()` and `funds.gl_projection_rows(table)`. Each
table's SQL stays in one place, which is the whole point of the phase.

### A reporting bug found and preserved

`get_trial_balance(as_of=…)` **silently ignores its date**:

```sql
FROM chart_of_accounts a
LEFT JOIN journal_lines   l ON l.account_code = a.code
LEFT JOIN journal_entries e ON l.entry_id = e.id
  AND e.entry_date <= ?        -- a join condition, not a filter
```

A line whose entry falls after the date simply fails to match `e`, but the line
itself survives the outer join and still contributes to `SUM(l.debit)`.
Measured on the production journal: an as-at-2021 trial balance returns the
same 42,453,293.19 of debits as the all-time one.

`views/treasury.py` passes a date from a date picker, so **the Treasury trial
balance shows all-time figures whatever date the user selects**. By contrast
`get_account_balance`, the income statement, the balance sheet and the cash
flow all put their dates in a `WHERE` and filter correctly — verified in the
same run.

Preserved verbatim, because Phase 2 must not change behaviour, and pinned by
`test_trial_balance_as_of_does_not_filter` so the fix is a deliberate, reviewed
change. **This is a one-line fix worth scheduling**: the predicate needs to
exclude the *line*, so the second join has to become an inner join (or the
filter has to move to a subquery over `journal_lines`). It changes what the
Treasury screen reports, which is why it is not bundled into a refactor.

### Verification

* 365 → 380 tests (15 added). `ruff` clean.
* **GL output differential** against the 2c-ii commit on a copy of the
  production journal: trial balance (all 8 accounts, debits and credits), the
  income statement all-time and for H1, the balance sheet, the cash flow with
  all three sections, account listings, recent entries, the 1,159-row account
  ledger for 1100 with its running balance, and four account balances — all
  identical.
* The 30-operation lifecycle differential also still matches.

### Rollback

`git revert` the 2e commit. No schema or data change.

---

## Three approved fixes

Behavioural changes, made deliberately after the refactor phases had shown what
they were, each with the evidence that justified it.

### 1. The dated trial balance now filters

`get_trial_balance(as_of=…)` ignored its date because the predicate sat on a
`LEFT JOIN`'s `ON` clause. The filter moved into a subquery over
`journal_lines`, which drops the line rather than just failing to match the
header, while the outer `LEFT JOIN` still lists every account.

On the production journal, as at 2024-12-31:

| | Debits | Credits | Balanced |
|---|---|---|---|
| Before (all-time, whatever date) | 42,453,293.19 | 42,453,293.19 | yes |
| After, as at 2024-12-31 | 38,502.90 | 38,502.90 | yes |

`views/treasury.py` passes a date from a picker, so the Treasury trial balance
now answers the question the user actually asked. `test_trial_balance_as_of_filters`
is the regression guard, and a second test confirms a dated trial balance still
balances.

### 2. `DeleteTransactionCommand` deleted

90 lines of dead code. It was exported from `src/services/__init__.py` but never
instantiated anywhere, and its `INSERT` targeted `ledger.principal_amount` and
`ledger.linked_trans_id` — columns that have never existed in any schema
generation — so the path raised `OperationalError` if reached. The live undo
path is `UndoTransactionCommand`, which is unaffected.

### 3. `fy_start_month` defaults to November

The institution's actual financial year, and what the production journal
stores. This closes finding **S1**, where the Settings dialog displayed
November when the key was unset while `reports.py` computed on January.

The default now only ever applies to a **brand-new** journal: migration 002
writes every settings key into the journal, so an existing one has an explicit
row and is unaffected — confirmed against the production journal, still
November, FY index 11.

Two consequences worth recording:

* **Migration 002 was edited in place**, which is normally wrong. It is safe
  here and only here: 002 has never been applied to anything but the production
  journal — where `fy_start_month` was already November, so its
  `INSERT OR IGNORE` was a no-op — and throwaway test databases. The file says
  so. Any future correction to a seeded value must be a new migration.
* `tests/test_fund_reports.py` failed on the change, correctly: it asserts
  column ordering using calendar quarters and had been relying on the January
  default. Its fixture now pins `fy_start_month` explicitly, which it should
  have done regardless.

381 tests, `ruff` clean.

---

## 2f — the import path, and the composition root

### 2f-i — repair, import history, and the source reader

`RepairRepository` (cross-table orphan queries) and `ImportHistoryRepository`
(`import_history` and the undo that keys off `import_id`) went into
`src/db/repair.py`; neither belongs to a single-table repository.

`src/db/source.py` — **`SourceJournal`** — closes a finding from the
architecture review. The import path used to interpolate table names taken from
the *source's own* `sqlite_master` into statements, so a crafted file could
steer a read at a table the importer never intended to touch. Reads now come
from a fixed allowlist, and the connection opens `mode=ro` so a source journal
cannot be modified even by accident. Both are asserted by tests.

Finding **D8** is documented rather than fixed: the conflict scan matches names
with `LIKE` (which treats `%` and `_` in the supplied name as wildcards) while
`name_exists` uses `LOWER(name) = LOWER(?)`. Converging them changes which rows
the importer flags as conflicts, so it is not a refactor-phase change. Both now
sit in `MemberRepository` with the divergence noted.

### 2f-ii — `import_selected_data`

The 456-line method keeps its structure and loses its SQL. Every statement now
goes through a repository or `SourceJournal`; the savepoint choreography,
progress callbacks and error strings are untouched.

**The savepoint policy is preserved, not invented.** Each phase takes a
savepoint on success, and phase 2 rolls back to `individuals_imported` on
failure so members survive a loan failure. Phase 3 does **not** roll back: a
savings failure leaves whatever rows it wrote and relies on the user undoing
the whole run. The original code says as much in a comment (*"This is tricky
without explicit savepoint management… Let's skip complex rollback logic"*).
Deciding what a partial savings import should do is a business call, so the gap
is now stated in the method's docstring instead of buried in a comment, and
left for a later phase.

**One latent bug fixed.** `original_isolation` was assigned *inside* the `try`,
but the outer handler restores it — so a failure while opening the source
raised `NameError` from the handler and masked the real error. It is now bound
before the `try`.

### Why there is no separate `DatabaseService` class

The Phase 2 design proposed one. Building it would have been ceremony:
`DatabaseManager` already owns the connection, already provides the transaction
boundary, and now exposes exactly one repository per aggregate. A second class
beside it would duplicate that for a name, and renaming this one would touch
every call site for no functional gain — against the brief's own "avoid
unnecessary renaming". The class docstring now states the role explicitly and
lists the repositories.

### Where the SQL ended up

| Location | Statements |
|---|---|
| `src/db/` repositories | 158 |
| `src/database.py` — schema DDL in `create_tables` | 52 |
| `src/database.py` — data statements | **3** |
| `src/integrity.py` — read-only verification tooling | 26 |
| `src/migrations/` | 3 |
| services, views, engine, reports | **0** |

The three data statements left in `database.py` are all inside
`create_tables`: the loan-suspension back-fill, the chart-of-accounts seed, and
the `is_edited` normalisation. They are schema-migration logic, and they leave
when `schema.sql` plus the migration runner take over table creation.

### Verification

* 396 tests, `ruff` clean.
* **Two import differentials** against the previous commit, each on freshly
  built source and destination journals:
  1. validation of good/missing/garbage files, preview, conflict scan, full
     import, history, orphan repair, undo;
  2. a harder run — loan-ref collisions (`L-001-Import`, `L-001-Import-1`),
     a PF number preserved on merge, a date range excluding an out-of-window
     ledger row, a `skip` decision, funds, benevolent enrolments, a suspension
     span, then undo — compared by per-table content hash.

  Both identical. The 30-operation lifecycle differential also still matches.

### Rollback

`git revert` the 2f-i and 2f-ii commits. No schema or data change.
