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
| 2b | pending | `MemberRepository` (`individuals`) | 53 |
| 2c | pending | `LoanRepository` + `LedgerRepository` | 113 |
| 2d | pending | `FundRepository` (`savings`, both funds) | 55 |
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
