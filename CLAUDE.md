# LoanMaster — notes for Claude

PyQt6 + SQLite desktop app for SACCO loan/savings/fund management.
Entry point is `loan.py`; the package is imported as `src.*`.

## Commands

- Run app: `python loan.py`
- Tests: `pytest tests/` (headless via tests/conftest.py; 199 tests must stay green)
- Lint: `ruff check src tests loan.py build.py` (config in pyproject.toml; must pass — CI gates on it)

## Architecture in one breath

`src/database.py` (DatabaseManager) owns all SQL and schema migrations
(idempotent ALTERs in `create_tables`). `src/engine.py` (LoanEngine) is a
facade over `src/services/*` where business logic lives. `src/views/*` are
the PyQt6 screens; they call the engine, not the DB, for writes.
`GLService` auto-posts member activity to the double-entry general ledger
via `db.ledger_post_hook` / `db.savings_post_hook`.

## Rules of the road

- Money is floats (`REAL`) with `math.ceil` rounding in places — do not
  "fix" rounding piecemeal; it must match existing ledgers. A cents
  migration is a deliberate, separate project.
- Loan refs (e.g. `L-001`) are unique **per member only** — always scope
  loan queries by `individual_id` too.
- Schema changes go in `create_tables` as try/except ALTERs (the app
  upgrades old journals in place). Journal backups are taken on open
  before migrations run.
- Legacy journals may fail `PRAGMA foreign_key_check` (orphaned rows);
  FK enforcement is enabled per-connection only when the check is clean.
- Use `logging.getLogger(__name__)`, never `print()`, in `src/`.
- Document branding (org name/logo on statements & reports) goes through
  `src/branding.py` (settings keys `org_name`/`org_logo_path`); don't
  hardcode logos or organization names in HTML templates.
- Real member data must never be committed: no `.db`, `.xlsx`, `.csv`,
  or generated statements in git. `scratch/` is the gitignored dumping
  ground for one-off scripts and local data.
- Keep `__version__` in `src/__init__.py` in sync with `setup.iss`.
