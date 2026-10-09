# Outstanding after Phases 1–8

Phase 9 (integer money) was dropped deliberately — see the end of this file.

Everything below is either a decision only the SACCO can make, or work that was
found and measured but not acted on because acting on it without that decision
would have been guessing.

---

## A. Money questions needing a human answer

These show as warnings in `python -m src.integrity loan_master.db`. None is a
code defect; each is a fact about the journal that someone has to rule on.

### A1. Two `Paid` loans carrying a balance — **fixed 2026-07-28**

Both were resolved after the replay harness diagnosed them. Neither was what
the warning suggested.

**Loan 483, NANCY GATHOGO, L-002 — a duplicate, not a defective loan.** She had
*two* loan records with identical principal (529,770), total (609,235.50),
start date and next due date. L-001 carried the disbursement and ten payments
of 11,496; L-002 had **no disbursement at all** and ten payments of 11,717
across the very same months. It was a phantom record whose payments were never
really taken — confirmed by the SACCO against payroll. Deleted (20 rows). Her
real loan, L-001, is untouched at 429,810 outstanding.

**Loan 193, BRIAN ODHIAMBO, L-001 — a mis-entered settlement.** The loan was
closed with a single row recording 95,833, but only 83,935 was owed at that
point (83,335 principal + 600 interest). The 11,898 excess is exactly the
negative balance. The figure was wrong, not the payment: corrected to 83,935
and the loan now clears to exactly 0.00.

Also removed: a member literally named `test` (id 143) with a loan and 23
ledger rows, created during testing.

After all three, and a GL sync: **39 checks, 0 failures, 2 warnings**, GL 1100
equals `loans.balance` exactly at 8,621,262.18, and
`tools/replay_loans.py` reproduces **111 of 111 loans (100%)**.

Every change is in the audit trail with before-images (51 ledger deletions, 45
ledger updates), visible under Mass Operations → Activity Log, and the journal
was snapshotted first to
`backups/loan_master.pre-fix-defective-loans-20260728-210245.db`.

### A2. Loans without a recoverable duration — **fixed 2026-07-28**

Both were derived from each loan's own issue note rather than guessed:
`(principal + total interest) / first installment`. Both landed on exact whole
months, which is what makes the method credible rather than a fit:

```
loan 193  BRIAN ODHIAMBO        (100,000 + 15,000.00) / 6,389 = 18.000 -> 18 months
loan 221  ROBERT MUTISYA NDOLO  ( 62,504 +  9,375.60) / 2,995 = 24.000 -> 24 months
```

Both imply a 15% rate, consistent with each other and with the rest of the book.

### A3. Deduction gaps — **recorded 2026-07-28**

Three loans had months with no deduction and no suspension on record. Because
accrual happens inside the deduction loop, those were also months with no
interest — about 8,181 never charged.

Suspensions were recorded retrospectively for each gap:

```
loan 573  GRACE MBURU       2026-01-28 -> 2026-04-28  (3 months)
loan 546  JOYCE MORAA       2025-08-28 -> 2025-09-28  (1 month)
loan 547  JOSPHINE KIONGI   2025-10-28 -> 2025-11-28  (1 month)
```

**This records that collection paused; it does not assert the pause was
authorised**, because that is not knowable from the journal. Each carries a note
saying so. If any of these months should in fact be collected, remove the
suspension from the loan's **Suspensions** button and run catch-up — which will
then charge the interest that was missed.

### A4. Eighteen loans have `total_amount = 0` — **open, needs a decision**

Found while deriving the terms above. Eighteen loans carry a total repayable of
zero, eleven of them Active with substantial balances:

```
  loan 675  COLLINS MUGITA     principal 375,065   balance 387,495
  loan 637  PETER MWORIA       principal 330,017   balance 276,676
  loan 520  SIMON KIAMA        principal 236,137   balance 196,184
  loan 300  JOHN MARANGA       principal  50,000   balance 144,904
  ... 14 more
```

On the same loans `principal` is often *below* `balance`, which cannot be true
of a loan that has only been repaid — so both fields look stale on loans that
were topped up, while `balance` (the field the GL reconciles against) is
correct.

**No money is wrong**: the books balance, every control account reconciles, and
all 111 loans replay exactly. What is broken is the *stated terms* — a
statement showing "total repayable" would print 0, and any paid-so-far
progress figure is meaningless.

Fixing it means deciding what `total_amount` should mean after a top-up:
the original total plus the top-up's, or the outstanding at top-up plus the new
interest. The two differ, and the answer is a lending-policy question rather
than a data question. `principal` needs the same decision.

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

### C2. Fiscal periods — removed with the general ledger

Superseded: migration 011 withdrew the GL, and fiscal periods and snapshots
went with it. The original note follows for the record.

#### (historical) No fiscal period has been closed

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
