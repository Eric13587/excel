# Outstanding after Phases 1–8

Phase 9 (integer money) was dropped deliberately — see the end of this file.

Everything below is either a decision only the SACCO can make, or work that was
found and measured but not acted on because acting on it without that decision
would have been guessing.

---

## A. Money questions needing a human answer

These show as warnings in `python -m src.integrity loan_master.db`. None is a
code defect; each is a fact about the journal that someone has to rule on.

### A1. Two `Paid` loans carrying a balance

Two loans are marked `Paid` but hold a non-zero balance, totalling **−113,778**
(negative — they are over-repaid on the books).

Either the status is right and the balance should be written off, or the balance
is right and the status is wrong. The two answers produce different figures in
the loan book, so the code cannot pick.

### A2. Three loans with no recoverable duration

Migration 003 backfilled `duration_months` and `interest_rate` by parsing the
issue note. Three loans have notes that do not parse; **one is still Active** —
loan id 221, ROBERT MUTISYA NDOLO.

The other two are settled and matter less. For 221, the schedule cannot be
projected forward until someone supplies the original term.

### A3. Twelve members with stale accrued interest

146 ledger rows across 12 members disagree with what `BalanceRecalculator`
computes today. **Some of the corrected values are negative accrued interest**,
which is not a thing that should exist — so applying the recalculation blindly
would replace one wrong number with another.

This needs a person to look at a handful of the members and decide what the
correct accrual actually was. Measured with the application's own recalculator,
not a reimplementation: an earlier estimate of "1,131 rows across 36 members"
came from replaying `added − deducted` per member, which is not how the
recalculator groups.

---

## B. Schema work identified but not built

### B1. Split `is_edited` into a flag and an amount

`ledger.is_edited` does two jobs. It is an edit lock (0/1), and when above 1 it
*also* carries the payment amount the user intended, which `BalanceRecalculator`
reads back as a replay anchor (`balance_calculator.py`, "Hysteresis Fix").

The two disagree: `create_tables` normalises anything outside 0/1 back to 1 on
every open, so the anchor survives only until the application restarts. This is
why migration 004 had to drop its `CHECK (is_edited IN (0,1))`.

The fix is an `edited_amount` column and a rule about which wins. That is a
behavioural decision about how an edited repayment replays, not a schema tidy-up,
which is why it was documented rather than half-built. The integrity report
counts the affected rows so the change can be sized before it is attempted.

### B2. Build new journals from the migrations — **done**

A new journal is now created directly from `src/migrations/schema_bootstrap.sql`
instead of building the legacy shapes and having migration 004 rebuild all
fourteen. Journal creation went 36.3 ms → 7.9 ms and the suite 20.3 s → 8.6 s.

The claim above that it carried "no behavioural risk" was wrong, and the work
proved it: the bootstrap file is dumped from `sqlite_master`, so it is DDL only,
and the first version recorded migration 002 as applied without ever inserting
its default settings — right shape, wrong contents. Three tests caught it. There
are now two guards, and the second is the one that matters:

* `test_a_new_journal_matches_a_migrated_one_exactly` — schema equality;
* `test_a_new_journal_has_the_same_reference_data_as_a_migrated_one` — row
  equality in `settings` and `chart_of_accounts`.

Any future migration that carries data must be added to
`DatabaseManager._SEED_MIGRATIONS`; the second test fails if it is not.

---

## C. Operational, not code

### C1. Nothing is on `main`

**Merged.** `main` carries the whole project as of the merge commit
"Merge: database architecture migration, Phases 1-8". Not pushed — see C3.

### C2. No fiscal period has been closed

Phases 7 and 8 are installed and inert. The period register is empty, so the
lock never fires and no snapshot exists. That was deliberate — closing a period
is a statement about published figures, not something a migration should decide.

Until a period is closed: the lock protects nothing, and statements still
aggregate the whole journal (which is fine at 11,728 lines and will not stay
fine). Treasury → Fiscal Periods → *Add This Year's Quarters* starts it.

### C3. Real member data in git history

Recorded separately: the repository's history contains member PII that was
pushed to GitHub. A history rewrite was proposed and is waiting on a decision.
`.gitignore` now covers `*.db` and `*.bak`, so nothing new is being added, but
the existing history is unchanged.

### C4. Backup files accumulating in the working directory

**All removed** (2026-07-28, at the user's request). The eighteen per-phase
`.bak` files, the four older loose ones, and the ten rotating journals in
`backups/` are all gone — 39 MB freed, project down to 15 MB.

**There is no restore point for the migration work.** The `.bak` paths named in
`docs/db/phase-*.md` and in the phase commit messages no longer resolve. Each
phase's code-level rollback still applies (dropping the triggers a migration
installed, reverting the commit), but the "restore the backup" route does not.

`backups/` is the application's own rotation — `_backup_on_open` writes one on
every open and keeps ten. It repopulates the next time the app runs, but only
with journals taken *after* this deletion.

A related gap was found while merging: `.gitignore` covered `*.db` and `*.bak`
but not SQLite's sidecars, which are named `<database><suffix>` — so
`loan_master.pre-phase8-….db.bak-shm` matched neither, and eighteen were
committed. They held no member data (the `-wal` files were empty; the `-shm`
files are page indexes, scanned against all 119 names with no hits). They are
untracked now and the ignore rules cover `*-wal`, `*-shm` and `*.db-journal`.

---

## D. Why Phase 9 was dropped

The plan's ninth phase was converting money from `REAL` floats to integer cents.
It was dropped, and the reasons are worth keeping:

* `CLAUDE.md` already calls it out: money is floats with `math.ceil` rounding in
  places, *"do not 'fix' rounding piecemeal; it must match existing ledgers. A
  cents migration is a deliberate, separate project."*
* It touches every money column in every table, so it rebuilds all four
  subledgers — which drops and must recreate the 40+ triggers installed by
  migrations 005, 007 and 009.
* It changes rounding behaviour that existing member statements depend on.
  Every historical figure would need re-verifying against what was published.

None of that makes it wrong to do. It makes it a project with its own
verification plan, not the last item on somebody else's list.
