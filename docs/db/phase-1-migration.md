# Phase 1 — Safety and Integrity

Status: **complete**. Three commits on `phase1-safety-and-integrity`.

| Sub-phase | Commit | Scope |
|---|---|---|
| 1a | Harden connection, transactions and backups | Pragmas, `BEGIN IMMEDIATE`, WAL, atomic member delete, WAL-safe backups |
| 1b | Versioned migrations + integrity constraints | Migration runner, cascading deletes, unique constraints, indexes |
| 1c | Integrity verification | `src/integrity.py` + `python -m src.integrity` |

No business logic changed. No schema *shape* changed — migration 001 is additive
DDL only (triggers and indexes); not one table was rebuilt and not one row was
read or rewritten.

---

## 1. What was wrong, and the evidence

### 1a — a live data-loss path

`delete_individual` issued two statements without a transaction:

```python
cursor.execute("DELETE FROM ledger WHERE individual_id=?", (id,))
cursor.execute("DELETE FROM individuals WHERE id=?", (id,))
self.conn.commit()
```

A member also owns rows in `savings`, `christmas_savings`, `benevolent_ledger`,
`benevolent_accounts`, `loan_suspensions` and `loans`, which nothing removed.
`_configure_connection` turns foreign-key enforcement on as soon as a journal
has no orphaned rows — which became true for the production journal once the
orphan repair was run. From that moment the second statement failed **after the
first had succeeded**, and the exception propagated with the transaction still
open, so the next unrelated write committed the half-delete.

Reproduced against a copy of `loan_master.db`:

```
member 6: ledger rows before = 15
delete_individual raised: IntegrityError
transaction still open: True
after an unrelated later write -> in_transaction: False
ledger rows after = 0   member still exists = True
>>> SILENT DATA LOSS
```

The user sees an error, assumes nothing happened, and the member's entire loan
history is gone.

### 1a — locking

* `transaction()` used pysqlite's deferred `BEGIN`. A deferred transaction takes
  a read lock and upgrades at the first write; if another connection holds the
  write lock then, SQLite returns `SQLITE_BUSY` **without invoking the busy
  handler**, because waiting there would deadlock. So `busy_timeout` never
  applied to read-then-write transactions — the shape of every recalculation
  path. Now `BEGIN IMMEDIATE`.
* `busy_timeout` was 5 s while mass catch-up holds the write lock for the length
  of the whole run. Now 30 s.
* `ReportWorker` (`src/views/dashboard.py`) opens a *second connection* on a
  background thread. Under a rollback journal that reader blocks the writer;
  under WAL it does not.

### 1b — no index on any transactional table

The journal had six indexes, all on `audit_log`, `individuals(pf_no/id_no)` and
the journal tables. There was **no index at all** on `ledger`, `loans`,
`savings`, the fund tables or `loan_suspensions`, so every read of a member's
history was a full table scan — inside loops that run once per member.

### 1b — invariants defended only in Python

* Loan references are generated as `max(ref)+1` in Python and de-duplicated by
  hand in the import path (`-Import`, `-Import-1` suffixes).
* `GLService.post_journal` prevents double-posting with a SELECT-then-INSERT on
  `(source, source_ref)` — racy, and bypassed by any other writer.

Both are now unique indexes.

---

## 2. What changed

### 1a — `src/database.py`, `src/views/dashboard.py`

| Change | Rationale |
|---|---|
| `delete_individual` wrapped in `transaction()` | All-or-nothing on every schema generation |
| `transaction()` uses `BEGIN IMMEDIATE`, re-entrant | Takes the write lock up front; nesting joins the outer transaction |
| `journal_mode` = WAL on local storage | Concurrent reader + writer; faster commits and recovery |
| `busy_timeout` 5 s → 30 s | Contention becomes latency, not a mid-operation failure |
| `synchronous = FULL` | A financial journal must not lose committed transactions |
| 64 MiB cache, `temp_store=MEMORY`, WAL size cap | Recalculation loops stay in cache; sidecar cannot grow unbounded |
| `PRAGMA optimize` on close | Planner works from current statistics as the journal grows |
| `backup_to()` replaces `shutil.copy` in both repair dialogs | Under WAL a file copy silently drops uncheckpointed transactions |

**Storage detection.** WAL needs a shared-memory mapping that network
filesystems do not provide, and their advisory locking is unreliable. Only a
*positively identified* local filesystem gets WAL (`/proc/mounts` on Linux,
`GetDriveTypeW` + UNC check on Windows); anything unrecognised keeps the
rollback journal this application already shipped with. Override with
`LOANMASTER_JOURNAL_MODE=wal|delete|auto`.

### 1b — `src/migrations/`, `src/database.py`, `build.py`

**The runner** (`src/migrations/__init__.py`) applies `NNN_*.sql` in numeric
order, once each, tracked in `schema_migrations`.

* Each migration runs in one `BEGIN IMMEDIATE` transaction.
  `Connection.executescript` **cannot** be used: it commits any pending
  transaction and runs every statement in autocommit, so a later failure is not
  rolled back (verified). Statements are split with
  `sqlite3.complete_statement`, which keeps `CREATE TRIGGER … BEGIN … END;`
  bodies intact.
* **Failure is never fatal.** A journal that cannot be migrated must still open
  so its data can be exported or repaired. The migration is rolled back, the
  version is not recorded, and the failure is reported via `migration_report`.

That non-fatal design has a trap, and it caught us during development: the first
run of migration 001 failed with `no such column: batch_id` and **the entire
test suite still passed**. `tests/test_migrations.py::test_migrations_actually_applied`
now asserts the migration applied, so this cannot go unnoticed again.

The underlying defect was real: `savings.batch_id` was only ever added by
`create_savings_table()`, which runs lazily from the savings helpers, so a newly
created journal had a `savings` table without it until the first savings
operation. The two definitions of that table disagreed (finding D2 in
[inconsistent-sql.md](inconsistent-sql.md)). `create_tables` now adds the column.

**Migration 001** (`001_integrity_constraints.sql`):

1. **Cascading deletes** via `BEFORE DELETE` triggers on `individuals` and
   `loans`. `BEFORE`, not `AFTER`: SQLite raises the foreign-key violation as
   the parent row is deleted, before an `AFTER` trigger would run. Clearing the
   children first means the parent delete finds nothing referencing it.
   `recursive_triggers` is off by default, so the loan trigger does not fire for
   loans deleted inside the member trigger — the member trigger therefore clears
   `loan_suspensions` itself, and does so *before* deleting `loans`, because
   `loan_suspensions.loan_id` references `loans(id)`.
2. **Unique indexes** on `loans(individual_id, ref)` and, partially, on
   `journal_entries(source, source_ref) WHERE source_ref IS NOT NULL`.
3. **22 indexes** across `ledger`, `loans`, `savings`, both funds,
   `loan_suspensions`, `journal_entries`, `individuals` and `audit_log`.
4. `ANALYZE`.

`build.py` gained `--include-data-dir=src/migrations=src/migrations`: Nuitka
compiles `.py` and would not have shipped the `.sql`, so the frozen build would
have opened every journal unmigrated.

---

## 3. Why CHECK constraints and NOT NULL are **not** in Phase 1

The brief listed them. The evidence says they are not safe to apply yet.

`import_selected_data` copies `event_type`, `status`, `start_date` and
`individual_id` **verbatim** out of arbitrary external journals. Those journals
demonstrably carry values outside the current code's vocabulary — the repository's
own fixtures, which model real legacy journals, contain `event_type =
'Loan Disbursed'`, loans with no `start_date`, and savings rows with a `NULL`
individual_id. Enforcing those domains today would reject legitimate imports.

They also require a full table rebuild. Doing that now and again in Phase 4
means rebuilding every table twice.

So: **Phase 4** adds an import normalisation layer, the flagged rows are cleaned
using the report from `python -m src.integrity`, and then
[`docs/db/proposed/004_strict_schema.sql`](proposed/004_strict_schema.sql) — already
rehearsed against a production copy — is applied as migration 004. One rebuild,
after the data can satisfy it. This is the "document the better design rather
than partially implement it" case.

---

## 4. Verification

Against a copy of the production journal (119 members, 2,192 ledger rows,
11,699 journal lines):

```
=== MIGRATION REPORT ===
  result        : applied 001_integrity_constraints
  open+migrate  : 0.034 s
  schema version: 1
  journal_mode  : wal | fk_enforced: True

=== DATA PRESERVATION ===
  row counts identical: True     (all 15 tables)

=== DATABASE VALIDATION ===
  integrity_check  : ok
  foreign_key_check: clean
  quick_check      : ok
  unbalanced journals: 0
  content identical: ledger         True
  content identical: loans          True
  content identical: journal_lines  True
```

**Application behaviour** on the migrated journal: create member, issue loan,
catch-up, savings deposit, both fund contributions, benevolent enrolment,
suspend/resume, top-up, buyoff, GL sync (trial balance and balance sheet both
balanced), mass catch-up over 20 loans and revert, then delete the member —
all seven child tables cascaded to zero, FK check clean, no orphans.

**Performance**, identical queries pre- vs post-migration:

| Query | Before | After | Speedup |
|---|---|---|---|
| `get_ledger` per member (×119) | 19.15 ms | 7.69 ms | 2.5× |
| Loan-scoped probes (×113) | 10.97 ms | 0.83 ms | 13.2× |
| Savings balance per member (×119) | 7.51 ms | 0.46 ms | 16.2× |
| Active loans + overdue scan | 0.27 ms | 0.27 ms | 1.0× |

These are scans becoming seeks, so the ratio grows with table size. The
unchanged row is a small table read in full — correctly unaffected.

**Tests**: 220 → 247 passing (27 added). `ruff` clean.

---

## 5. Rollback

### 1a — code only

```bash
git revert <1a-commit>
```

No schema or data change. SQLite removes the `-wal`/`-shm` sidecars when the
last connection closes; to be certain before downgrading, check the journal in:

```bash
sqlite3 loan_master.db "PRAGMA journal_mode=delete;"
```

### 1b — code and schema

The migration is additive, so the rollback is a drop, not a restore:

```sql
DROP TRIGGER IF EXISTS trg_cascade_individual_delete;
DROP TRIGGER IF EXISTS trg_cascade_loan_delete;

DROP INDEX IF EXISTS idx_loans_member_ref;
DROP INDEX IF EXISTS idx_journal_entries_source_ref;
DROP INDEX IF EXISTS idx_ledger_individual_date;
DROP INDEX IF EXISTS idx_ledger_individual_loan;
DROP INDEX IF EXISTS idx_ledger_date;
DROP INDEX IF EXISTS idx_ledger_batch;
DROP INDEX IF EXISTS idx_ledger_import;
DROP INDEX IF EXISTS idx_loans_individual;
DROP INDEX IF EXISTS idx_loans_status_due;
DROP INDEX IF EXISTS idx_loans_import;
DROP INDEX IF EXISTS idx_savings_individual_date;
DROP INDEX IF EXISTS idx_savings_date;
DROP INDEX IF EXISTS idx_savings_batch;
DROP INDEX IF EXISTS idx_savings_import;
DROP INDEX IF EXISTS idx_christmas_individual_date;
DROP INDEX IF EXISTS idx_christmas_batch;
DROP INDEX IF EXISTS idx_benevolent_individual_date;
DROP INDEX IF EXISTS idx_benevolent_batch;
DROP INDEX IF EXISTS idx_suspensions_individual;
DROP INDEX IF EXISTS idx_suspensions_loan;
DROP INDEX IF EXISTS idx_journal_entries_date;
DROP INDEX IF EXISTS idx_individuals_name_nocase;
DROP INDEX IF EXISTS idx_individuals_import;
DROP INDEX IF EXISTS idx_audit_ts;

DELETE FROM schema_migrations WHERE version = 1;
```

No data is touched, so no restore is required. Every journal is still backed up
into `backups/` on open regardless.

Note that rolling back the cascade triggers reinstates the data-loss path
described in §1 unless the 1a commit is reverted too — revert 1b **only**
together with 1a, or not at all.

---

## 6. Files changed

| File | Sub-phase | Change |
|---|---|---|
| `src/database.py` | 1a, 1b | Pragmas, storage detection, `BEGIN IMMEDIATE`, `backup_to`, atomic delete, `_evaluate_foreign_keys`, runner wiring, `savings.batch_id` |
| `src/views/dashboard.py` | 1a | Both repair backups via `backup_to`; unused `shutil` imports removed |
| `src/migrations/__init__.py` | 1b | **new** — migration runner |
| `src/migrations/001_integrity_constraints.sql` | 1b | **new** — cascade triggers, unique indexes, 22 indexes |
| `build.py` | 1b | Ship `src/migrations` with the frozen build |
| `tests/test_connection_hardening.py` | 1a | **new** — 13 tests |
| `tests/test_migrations.py` | 1b | **new** — 13 tests |
| `tests/test_orphan_repair.py` | 1b | Fixture drops cascade triggers to model a pre-001 journal; new no-orphans regression test |
| `docs/db/proposed/004_strict_schema.sql` | 1b | Moved from `migrations/`, annotated as a Phase 4 proposal |
| `docs/db/phase-1-migration.md` | — | **new** — this document |
