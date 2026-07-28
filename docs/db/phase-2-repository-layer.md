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
| 2c | next | `LoanRepository` + `LedgerRepository` | 113 |
| 2e | pending | `JournalRepository` (COA, entries, lines) | 42 |
| 2f | pending | Raw SQL out of views/engine/services; `DatabaseService` root | 36 |

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

So the default is **`'January'`**: the value `reports.py` already used, which
preserves every figure the application currently produces. The Settings dialog
now displays January when unset, which is a cosmetic change that makes the UI
agree with what the books were already doing.

The production journal has the key explicitly set to `November`, so it is
unaffected either way — verified after the change: stored `November`, FY index
`11`, unchanged.

> ### Open question for the institution
>
> Which month *should* start the financial year on a **new** journal? The code
> now says January; the Settings dialog used to suggest November, and the
> production journal is set to November. If November is the institution's real
> financial year, change `DEFAULTS[FY_START_MONTH]` in `src/db/settings.py` and
> the matching row in `seed.sql` — one line each, and it affects new journals
> only. This is deliberately left as a business decision rather than an
> assumption baked into a refactor.

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
