# From scattered inline SQL to a repository layer

## Where the SQL actually is today

302 SQL statements in production code, across 12 files:

| File | Statements | What it should be doing |
|---|---|---|
| `src/database.py` | 226 | 98 methods in one 2,557-line class: DDL, migrations, CRUD, import/merge, orphan repair, statement assembly |
| `src/services/gl_service.py` | 30 | Accounting logic — but also every GL query |
| `src/services/loan_service.py` | 11 | Loan lifecycle — reaches past `DatabaseManager` for `UPDATE loans`, `DELETE FROM ledger` |
| `src/engine.py` | 9 | Facade — but runs its own `SELECT`/`UPDATE` for the heal tool |
| `src/services/undo_manager.py` | 7 | Command objects with hand-written INSERT/DELETE |
| `src/services/balance_calculator.py` | 6 | Recalculation, updating `ledger` directly |
| `src/views/ledger.py` | 4 | **A Qt widget executing SQL** |
| everything else | 25 | |

The architecture in `CLAUDE.md` says *"views call the engine, not the DB, for
writes"*. That holds for most of the app; the exceptions are exactly where the
bugs in [`inconsistent-sql.md`](inconsistent-sql.md) live.

## The problem isn't "SQL in Python" — it's that there's no single owner

Three specific costs, all visible in this codebase:

1. **No place to put an invariant.** "Loan refs are unique per member" is a rule
   with no home, so it is re-implemented at every call site and forgotten at
   one. The same is true of "recalculate after editing a fund row" and "clear
   the edit-lock only for future rows".
2. **Write-path hooks get bypassed.** `DatabaseManager.add_savings_transaction`
   fires `savings_post_hook` so the GL posts live. `views/ledger.py` updates
   `savings` with raw SQL — no hook, no GL entry, and the discrepancy only
   surfaces on the next full sync.
3. **Reads can't be optimised centrally.** `get_ledger` is called once per
   member inside recalculation loops. There is nowhere to add caching, batching
   or an index-aware rewrite, because every caller has its own query.

## Proposed shape

Three layers, each with one job. This is an incremental refactor of the existing
structure, not a rewrite — `LoanEngine` and the services keep their public
APIs.

```
src/db/
├── connection.py        # DatabaseConnection: open, pragmas, backup, transaction()
├── schema.py            # bootstrap from schema.sql + migration runner
├── repositories/
│   ├── base.py          # Repository: cursor helpers, row→dataclass mapping
│   ├── members.py       # MemberRepository
│   ├── loans.py         # LoanRepository        (+ suspensions)
│   ├── ledger.py        # LedgerRepository
│   ├── funds.py         # FundRepository        (savings/christmas/benevolent)
│   ├── journals.py      # JournalRepository     (COA, entries, lines)
│   ├── settings.py      # SettingsRepository
│   └── admin.py         # ImportRepository, AuditRepository, RepairRepository
└── service.py           # DatabaseService: composition root
```

### `DatabaseService` — the composition root

```python
class DatabaseService:
    """Owns the connection and exposes one repository per aggregate.

    This is the only object that knows a database exists. Services take the
    repositories they need; views take services.
    """

    def __init__(self, path: str, *, auto_backup: bool = False):
        self._conn = DatabaseConnection(path, auto_backup=auto_backup)
        bootstrap_schema(self._conn)          # schema.sql + pending migrations

        self.members  = MemberRepository(self._conn)
        self.loans    = LoanRepository(self._conn)
        self.ledger   = LedgerRepository(self._conn)
        self.funds    = FundRepository(self._conn)
        self.journals = JournalRepository(self._conn)
        self.settings = SettingsRepository(self._conn)
        self.imports  = ImportRepository(self._conn)
        self.audit    = AuditRepository(self._conn)

    @contextmanager
    def transaction(self):
        """One transaction spanning any number of repositories."""
        with self._conn.transaction():
            yield self
```

The transaction boundary sits on the service, not inside each repository — so
`mass_catch_up_loans` still gets one atomic run across `ledger` and `loans`,
which is exactly what it needs today.

### A repository, concretely

```python
@dataclass(frozen=True)
class LedgerEntry:
    id: int
    individual_id: int
    date: str
    event_type: str
    loan_ref: str
    loan_pk: int | None
    added: float
    deducted: float
    balance: float
    principal_portion: float
    interest_portion: float
    is_edited: int
    # … one field per column; no more `if "principal_balance" in df`


class LedgerRepository(Repository):
    """Every statement that touches `ledger` lives here. No exceptions."""

    COLUMNS = "id, individual_id, date, event_type, loan_id AS loan_ref, loan_pk, …"

    # ---- reads ----
    def for_member(self, member_id: int, start=None, end=None) -> list[LedgerEntry]:
        sql = f"SELECT {self.COLUMNS} FROM ledger WHERE individual_id = ?"
        params = [member_id]
        if start:
            sql += " AND date >= ?"; params.append(start)
        if end:
            sql += " AND date <= ?"; params.append(end)
        return self._all(LedgerEntry, sql + " ORDER BY date, id", params)

    def for_loan(self, member_id: int, loan_ref: str) -> list[LedgerEntry]:
        """Loan refs are unique PER MEMBER — this signature makes the
        member_id argument impossible to forget."""
        return self._all(LedgerEntry,
                         f"SELECT {self.COLUMNS} FROM ledger "
                         "WHERE individual_id = ? AND loan_id = ? ORDER BY date, id",
                         [member_id, loan_ref])

    def has_restructure_events(self, member_id: int, loan_ref: str) -> bool:
        ...   # the single owner of D3

    # ---- writes (the only paths that fire the GL hook) ----
    def add(self, entry: NewLedgerEntry) -> int: ...
    def add_many(self, entries: Sequence[NewLedgerEntry]) -> list[int]: ...
    def set_interest_lock(self, member_id, loan_ref, *, locked: bool,
                          from_date: str | None = None) -> int: ...   # single owner of D4
    def delete_batch(self, batch_id: str) -> int: ...
```

Two design points worth stating explicitly, because they are what fix the bugs
found in the audit:

* **Member scoping is in the type signature.** `for_loan(member_id, loan_ref)`
  cannot be called without a member. The `CLAUDE.md` rule stops being a rule
  people have to remember.
* **Post-hooks live in the repository**, so there is no way to write a row
  without the GL projection being offered the chance to post it. That closes
  D6.

### Mapping the current API

`DatabaseManager` becomes a thin deprecation shim during the transition, so
nothing breaks in one step:

| Today | Becomes |
|---|---|
| `db.get_ledger(id)` → DataFrame | `db.ledger.for_member(id)` → `list[LedgerEntry]` (a `.to_frame()` helper keeps the pandas-based statement/report code working) |
| `db.get_loan_by_ref(ind, ref)` | `db.loans.by_ref(ind, ref)` |
| `db.add_transaction(...)` (16 positional args) | `db.ledger.add(NewLedgerEntry(...))` |
| `db.fund_add_transaction("christmas_savings", …)` | `db.funds.add(Fund.CHRISTMAS, …)` — the whitelist becomes an enum |
| `db.get_setting("deduct_same_month", "false")` | `db.settings.deduct_same_month` — typed property, default in one place (fixes S1) |
| `db.find_orphaned_rows()` / `delete_orphaned_rows()` | `db.repair.*` — and mostly obsolete once `ON DELETE CASCADE` exists |
| `db.import_selected_data(...)` (456 lines) | `ImportService` orchestrating repositories; the SQL moves to whichever repository owns each table |

### Sequencing (each step ships independently, suite green throughout)

1. **Extract `DatabaseConnection`** — connection setup, pragmas, backup,
   `transaction()`. No SQL moves. Lowest risk.
2. **Adopt `schema.sql` + a migration runner** (Stage 2 of the migration plan).
   Deletes the ~40 silent `try: ALTER … except: pass` blocks.
3. **`SettingsRepository` first.** Smallest surface (3 statements, 21 call
   sites), immediately fixes the divergent-default bug, and proves the pattern.
4. **`FundRepository`.** The generic `fund_*` API is already 80 % of a
   repository; adding `savings` to it collapses the five recalculation
   implementations (D1) into one.
5. **`LedgerRepository` + `LoanRepository`.** The big one. Move the raw SQL out
   of `views/ledger.py`, `engine.py`, `loan_service.py` and
   `balance_calculator.py` as part of this step — that is where D3–D6 get
   fixed.
6. **`JournalRepository`.** `GLService` keeps the accounting logic (posting
   rules, balance assertions, statement assembly) and loses the SQL. This is the
   cleanest split in the codebase because the two concerns are already
   separable.
7. **`MemberRepository` + `ImportRepository`.** Break up
   `import_selected_data`; it is currently one 456-line method with four nested
   savepoints and its own inline `SAVEPOINT`/`ROLLBACK TO` management.
8. **Delete the `DatabaseManager` shim.**

### What this buys, measured against the audit

| Finding | Fixed by |
|---|---|
| D1 five balance recalculations | Step 4 — one `FundRepository.recalculate` |
| D2 duplicate table definition, DDL on read paths | Step 2 — schema owned by `schema.sql` |
| D3 duplicated restructure probe | Step 5 |
| D4 inconsistent edit-lock mutations | Step 5 — `set_interest_lock` |
| D5 three update keys for `loans` | Step 5 — writes go through the PK |
| D6 raw SQL in views | Step 5 — and hooks stop being bypassable |
| D7 coverage gap in "earliest date" | Step 5 — one query over all four subledgers |
| D8 two case-insensitivity mechanisms | Step 7 |
| D9 stale balance snapshots | Step 4 — recalculation is part of the write path |
| D10 `SELECT *` + positional access | Steps 3–7 — dataclasses with explicit columns |
| S1 divergent setting defaults | Step 3 |

### Testing

The current 220 tests build real SQLite databases through `DatabaseManager`,
which is the right instinct — keep it. Two additions the layering makes
possible:

* **A schema contract test**: build a journal from `schema.sql`, build another
  by applying `migrations/*.sql` to a legacy fixture, and assert the two
  `sqlite_master` dumps are identical. This is the test that stops the two
  drifting apart, which is precisely how the app arrived at two different
  `savings` definitions.
* **Repository-level invariant tests** that assert the constraints now in the
  schema actually reject bad writes (duplicate `(individual_id, ref)`, an
  unknown `event_type`, a double-posted `source_ref`). Today those invariants
  have no test because they have no implementation.
