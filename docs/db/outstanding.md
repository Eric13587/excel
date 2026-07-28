# Outstanding after Phases 1–8

Phase 9 (integer money) was dropped deliberately — see the end of this file.

Everything below is either a decision only the SACCO can make, or work that was
found and measured but not acted on because acting on it without that decision
would have been guessing.

---

## A. Money questions needing a human answer

These show as warnings in `python -m src.integrity loan_master.db`. None is a
code defect; each is a fact about the journal that someone has to rule on.

### A1. Two `Paid` loans carrying a balance — **diagnosed**

Two loans are marked `Paid` but hold a non-zero balance, totalling **−113,778**.
They have *different* causes, found while testing the suspension work:

**Loan 193, BRIAN ODHIAMBO, L-001 — balance −11,898.** Properly issued at
100,000. The final row is a 95,833 settlement entered as a `Repayment` with
`principal_portion = 95,233`, but only 83,335 of principal was still
outstanding. The over-allocation is 11,898 — exactly the negative balance. It
looks like a buy-off recorded as an ordinary repayment, so the split was
computed against the installment rather than the payoff figure. Correcting the
split on that one row clears it.

**Loan 483, NANCY GATHOGO, L-002 — balance −101,880. This is the serious one.**
There is **no `Loan Issued` row at all**. Ten deductions of 10,188 principal
each total 101,880 against a recorded disbursement of zero, which is precisely
the negative balance. The loan record says `principal = 529,770`.

So either the disbursement was never entered (and this member has roughly
**427,890 genuinely outstanding while the system says `Paid`**), or the loan
record is a stub that should not exist. Only the SACCO can say which — but this
is not cosmetic, and it is the one to look at first.

The general failure mode is worth naming: a loan whose ledger has no issue row
has its balance driven negative by its own repayments and is then flipped to
`Paid`. `src.integrity` now checks for both causes directly, not just the
symptom — **loans record their disbursement** and **principal repaid stays
within what was lent** — so each names the loan and the member, and a
recurrence is caught rather than discovered.

Swept across the whole journal when those checks were written: **exactly one
loan each**, and both are the two above. No other loan is affected.

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

### C5. UI/UX review — first pass done

A review of the interface found the gaps below. The first five are fixed; the
rest are recorded rather than done.

**Fixed:**

* Recorded suspensions had no route out of the application, and the schedule
  rebuild understated what it deletes. See the commit "Give recorded suspensions
  a way out".
* The 35 integrity checks are now a screen — Treasury → **Health Check** —
  instead of a message box telling a clerk to run `python -m src.integrity`.
  The startup warning offers to open it.
* Member and loan deletion state what will be removed, count it, and take a
  snapshot first, aborting if the snapshot cannot be written.
* The Activity Log shows the before-image recorded by migration 007, so it
  answers "changed from what?" rather than only "changed".

**Still open:**

* ~~**Four swallowed exceptions.**~~ Fixed. Two were the "open the exported
  file" path; the earlier note that "a failed export looks like a successful
  one" was **wrong** — the success message fires before the attempt and the
  file really is written, so only the *opening* failed silently. Treasury's
  copy of the same block had no handling at all and would have raised into the
  GUI. All three now go through `src/views/file_actions.py`, which reports the
  path and says it could not be opened. The other two now log.

* **Undo remains narrow**, though less so. It covers mass catch-ups and
  imports. Deleting a member or loan is now recoverable *and* restorable via
  Mass Operations → Restore From Backup, as are the repair tools, whose
  backups previously landed beside the journal as `.bak` files where the
  restore screen could not see them. `retire_individual` needs nothing —
  `reinstate_individual` is reachable in the UI — and `rebuild_loan_schedule`
  is only reachable from the suspend dialog, which already snapshots. What is
  still missing is a true in-app *undo* (a step back without replacing the
  whole journal) for anything outside mass operations.
* ~~**No in-app restore.**~~ Done. Mass Operations → **Restore From Backup**
  lists every backup beside the journal with what is *in* each one (members,
  loans, ledger entries), refuses a file that is damaged or is not a journal,
  and snapshots the current state before restoring so the restore is itself
  reversible. Mass Operations also gained **Journal Health Check**, so the
  report is reachable without opening Treasury.

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
