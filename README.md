# LoanMaster

Desktop loan, savings and fund management for SACCOs (savings and credit
cooperatives). Built with PyQt6 and SQLite; runs fully offline with a
single-file "journal" database that can live on a shared drive.

## Features

- **Members** — roster with PF/ID numbers, employment status, retirement
  tracking, and a Members List export (Excel / PDF / CSV).
- **Loans** — issuance, monthly deductions, top-ups, restructuring, buyoff,
  suspensions and catch-up runs.
- **Savings** — deposits, withdrawals, catch-up.
- **Christmas & Benevolent funds** — separate ledgers with welfare payouts.
- **Ledger Grid** — every member and month in one table for bulk work: type,
  paste, operations (`@W=500`, `@T=20,000/12`), Fill Forward and month
  deletion. It replaces the old mass operations.
- **Reports** — quarterly interest reports, fund reports, financial
  statements (income statement / balance sheet / cash flow), member
  statements (PDF/Excel), SASRA loan-loss provisioning.
- **Excel import** — roster and fund contributions, with duplicate
  detection, preview, and undo.
- **Organization branding** — set your organization's name and logo once
  (Settings → Organization); every statement, report and export carries it.
- **Undo/redo** — Ctrl+Alt+Z / Ctrl+Alt+Y for most ledger operations; the
  Ledger Grid has its own Undo Last Save.
- **Audit log** — CRUD audit trail with created/updated timestamps.

## Getting started (development)

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate   # optional
pip install -r requirements.txt
python loan.py
```

On first launch, create a new journal (`.db` file) or open an existing one.

## Tests and lint

```bash
pip install pytest ruff        # or: pip install -e ".[dev]"
pytest tests/                  # 199 tests; run headless automatically
ruff check src tests loan.py build.py
```

CI (GitHub Actions) runs ruff and the test suite on Python 3.10 and 3.13,
and only builds the Windows installer when both pass.

## Building the Windows app

See [README_BUILD.md](README_BUILD.md). Short version: `python build.py` on
Windows (Nuitka), then `iscc setup.iss` for the installer. CI produces both
artifacts on every push to `main`.

## Data safety

- **Automatic backups** — every time the app opens a journal it snapshots it
  to a `backups/` folder next to the journal file (last 10 kept), *before*
  any schema migration runs.
- **Integrity check** — SQLite `quick_check` runs on open; problems are
  logged and surfaced in the UI without blocking data export.
- **Foreign keys** — enforced on journals whose data passes a consistency
  check; a journal with orphaned rows gets a logged warning and must be
  sanitized by hand.
- **Logs** — rotating logs are written to the per-user state directory
  (`%LOCALAPPDATA%\LoanMaster\logs` on Windows,
  `~/.local/state/loanmaster/logs` on Linux). Uncaught errors show a dialog
  pointing at the log file instead of silently killing the app.
- **Never commit member data.** Databases, spreadsheets, CSVs and generated
  statements are gitignored; keep it that way.

## Repository layout

```
loan.py                  entry point
build.py, setup.iss      Windows build (Nuitka + Inno Setup)
src/
  main.py                QApplication bootstrap, main window
  database.py            SQLite schema, migrations, all DB access
  engine.py              business-logic facade over src/services/
  services/              loan/savings/funds/GL/import/undo services
  views/                 dashboard, ledger, treasury (PyQt6 widgets)
  reports.py             quarterly/fund/financial reports
  statement_generator.py member statements (PDF/Excel)
  logging_setup.py       logging, crash dialog, Qt message routing
  config.py              business constants, chart of accounts, SASRA bands
tests/                   pytest suite (headless)
scratch/                 local one-off scripts and data (gitignored)
```

## Known limitations / roadmap

- Monetary values are stored as SQLite `REAL` (floats) with ceiling-based
  rounding in places; a migration to integer cents with `decimal` arithmetic
  is the long-term fix.
- No user accounts/roles — anyone who can open the journal file has full
  access. Restrict access at the file-share level.
