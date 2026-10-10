# LoanMaster

Desktop loan, savings and fund management for SACCOs (savings and credit
cooperatives). Written in Rust with egui and SQLite; runs fully offline with a
single-file "journal" database that can live on a shared drive.

LoanMaster 2.0 opens the same journals as 1.x. How the port was checked, and
every place 2.0 behaves differently on purpose, is in
[docs/rust-migration.md](docs/rust-migration.md).

## Features

- **Members** — roster with PF/ID numbers and membership status. A member who
  retires, resigns, is terminated or dies is paid out their savings and
  Christmas savings; they can be reinstated. Members List export (Excel /
  PDF / CSV).
- **Loans** — issue, monthly deductions, catch-up, top-ups, buy-offs,
  suspensions, and corrections to any entry.
- **Savings** — deposits, withdrawals, catch-up.
- **Christmas & Benevolent funds** — separate ledgers with enrolment,
  contributions and welfare payouts.
- **Ledger Grid** — every member and month in one table for bulk work: type,
  paste, operations (`@W=500`, `@T=20,000/12`), Fill Forward and month
  deletion. It replaces the old mass operations.
- **Reports** — the quarterly loan interest report, savings by quarter or
  period, the Christmas and benevolent reports, loans in arrears, the members
  list, and member statements in batches (PDF / Excel).
- **Imports** — members and their history from another journal, and rosters
  or fund contributions from Excel; each is previewed first and can be undone
  from Import History.
- **Organization branding** — set the organization's name and logo once
  (Tools › Settings); every statement, report and export carries it.
- **Undo/redo** — Ctrl+Alt+Z / Ctrl+Alt+Y for ledger operations; the Ledger
  Grid has its own Undo Last Save.
- **Activity log** — the audit trail, with what a changed or deleted money row
  said before.

## Getting started (development)

Requires Rust 1.95 or newer.

```bash
cargo run --release -p loanmaster                       # choose a journal
cargo run --release -p loanmaster -- path/to/journal.db
```

On first launch, create a new journal (`.db` file) or open an existing one.
A 1.x journal must be at schema version 12: if 2.0 refuses one, open it once
in LoanMaster 1.x, which brings it up to date.

`--software` (or `LOANMASTER_SOFTWARE_RENDERING=1`) draws without the graphics
card, for machines whose drivers misbehave. LoanMaster falls back to it by
itself if the window cannot start.

## Tests and lint

```bash
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
```

The application's tests drive the window headlessly. Set `LOANMASTER_SHOTS`
to a folder to have them save screenshots there (this needs a Vulkan driver,
such as Mesa's lavapipe).

CI (GitHub Actions) runs clippy and the tests, and builds the Windows
installer only when both pass.

## Building the Windows app

On Windows, with Rust and [Inno Setup 6](https://jrsoftware.org/isinfo.php)
installed:

```bat
cargo build --release -p loanmaster
iscc setup.iss
```

The installer is written to `build\LoanMasterSetup.exe` and upgrades a 1.x
installation in place. `target\release\loanmaster.exe` on its own is the
portable copy: fonts, icon and the default logo are built in. CI produces both
on every push to `main`.

## Data safety

- **Automatic backups** — every time the app opens a journal it snapshots it
  to a `backups/` folder next to the journal file (last 10 kept). Imports,
  restores and other changes that cannot be undone take a named copy first,
  and Tools › Restore From Backup brings any of them back.
- **Integrity check** — SQLite `quick_check` runs on open; a damaged journal
  is reported but still opens, so its data can be exported.
- **Foreign keys** — always enforced. Rows that refer to a member or loan that
  no longer exists are counted in the status bar; sanitize them by hand.
- **Logs** — rotating logs are written to the per-user state directory
  (`%LOCALAPPDATA%\LoanMaster\logs` on Windows,
  `~/.local/state/loanmaster/logs` on Linux). An unexpected error shows a
  dialog pointing at the log file instead of silently killing the app.
- **Never commit member data.** Databases, spreadsheets, CSVs and generated
  statements are gitignored; keep it that way.

## Repository layout

```
crates/core/      the journal: schema, loans, funds, Ledger Grid, policy,
                  undo, report and statement data, imports (no UI)
crates/reports/   writing documents: workbooks, CSV, PDFs typeset with Typst
crates/app/       the egui desktop application
resources/        the icon and the bundled default logo
docs/             design notes; rust-migration.md compares 2.0 with 1.x
setup.iss         Windows installer (Inno Setup)
```

## Known limitations

- Money is stored as SQLite `REAL` (floats) and rounded to cents as 1.x
  rounded it; integer cents would be the long-term fix.
- No user accounts or roles — anyone who can open the journal file has full
  access. Restrict access at the file-share level.
