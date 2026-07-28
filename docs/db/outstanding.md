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

### B2. Build new journals from the migrations, not the legacy bootstrap

`create_tables` still creates the pre-migration table shapes, which migration 004
then rebuilds. Every new journal — including every test journal — therefore
performs a full 14-table rebuild on creation. That is why the suite takes ~20 s
instead of ~5 s.

Creating new journals directly from the final shape would remove that. It is
contained work with a clear payoff and no behavioural risk.

---

## C. Operational, not code

### C1. Nothing is on `main`

Phases 1–8 are 26+ commits on `phase1-safety-and-integrity`. `main` has not
moved. Nothing described in `docs/db/phase-*.md` is on the default branch.

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

Eighteen `loan_master.pre-*.db.bak` files, one per migration step, plus older
ones from June. They are gitignored and each is a full copy of the journal. Safe
to prune once the phases are accepted; the most recent per phase is the rollback
point named in that phase's commit message.

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
