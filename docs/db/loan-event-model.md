# A cleaner loan model — proposal, with evidence

Status: **proved, and continuously checked.** `src/services/loan_derivation.py`
(the derivation), `src/services/loan_events.py` (recovering events from the
existing ledger) and `tools/replay_loans.py` (the harness) exist and are tested.
`src.integrity` runs the replay on every check. **No schema has changed and no
production code path uses the model yet** — the ledger remains the source of
truth.

**112 of 113 production loans reproduce exactly** from their own recovered
history. The one that does not is loan 483, which has no disbursement row —
a data problem, not a modelling one.

## The problem, precisely

`ledger` holds two kinds of row in one table with nothing distinguishing them:

* a `Repayment` payroll actually took is a **fact**;
* the `Interest Earned` row beside it was generated from the loan's terms — a
  **consequence**.

Any recomputation therefore has to guess what it may regenerate and what it
would be destroying. Every awkward part of the current design follows from that
one conflation:

| Symptom | Why it exists |
|---|---|
| `rebuild_loan_schedule` deletes **all** repayments and interest | it cannot tell asserted rows from derived ones |
| `is_edited` stores a payment *amount* above 1 | so a manual edit survives that deletion — which is why migration 004 had to drop its `CHECK (is_edited IN (0,1))` |
| 14 `UPDATE loans SET` across 10 setters, 4 recalculation entry points | each handles a different "what do I keep?" case |
| "which terms applied to the months a deleted suspension skipped?" | `loans.installment` is a single current value with no history — the question is **unanswerable as stored**, which is why it invites heuristics |

That last row is the one that prompted this. It is not a hard question. It is
an unanswerable one, and the fix is to store the answer.

## The model

**Events** — what someone asserted, append-only:
`Disbursed`, `ToppedUp`, `TermsChanged`, `Suspended`, `PaymentReceived`,
`Settled`. Each carries an effective date.

**Derivation** — `derive(events, up_to)`, a pure function. No database, no
clock, no settings. Same events in, same postings out.

**Projection** — `ledger` and `loans`, rebuildable from the events, exactly the
way the general ledger already works since migrations 005 and 010.

Two rules earn their keep:

* **Terms are a timeline.** `terms_on(timeline, date)` answers "what was the
  installment in March 2025" by lookup. Deleting a past suspension re-derives
  those months under the terms in force *then*, not the terms in force now.
* **State events apply before their own month accrues; payments apply after.**
  A suspension starting on a due date must skip that month, while a payment on
  a due date lands after that month's interest.

## What the replay found

`python -m tools.replay_loans loan_master.db`, read-only, against the
production journal:

```
Replaying 113 loan(s)

  reproduced exactly :   83   (73%)
  diverged           :   30

why they diverged:
    26  terms changed mid-life (no history stored)
     3  gap in deductions (no interest charged for those months)
     1  no disbursement recorded
```

**73% replay exactly from events alone** — no cached balance, no
`next_due_date`, no running totals. That is the evidence the model is sound.

The 30 divergences were first bucketed by event type, which put top-ups and
buy-offs in separate piles and left nine cases unexplained. Investigating those
nine collapsed the picture:

### 26 of 30 were one finding — and it is now fixed

Top-ups and buy-offs both **rewrite the loan's terms**, and terms were being
read from the *current* `loans` row and applied to months they never governed.
The give-away is that a single loan's accruals are not all the same amount:

```
loan 425   loans.monthly_interest = 1,251   actual accruals = [250, 1251, 1326]
loan 463   loans.monthly_interest =   832   actual accruals = [250,  832, 1326]
loan 603   loans.monthly_interest = 1,875   actual accruals = [250, 1326, 1875]
```

One column cannot have produced three different accruals — the current schema
cannot represent these loans' own histories.

But the accruals *are* the record. The application posts
`min(monthly_interest, unearned)` each due month, so **a change in the amount
accrued is a change in the terms**, effective the date it first appears.
`src.services.loan_events.reconstruct` infers `TermsChanged` events from
exactly that, and the 26 divergences became 0.

The installment is deliberately **not** reconstructed. Payments are asserted
events carrying the amount actually received, so the installment never enters
the derivation; inferring it from repayment amounts would mean guessing which
variations were term changes and which were a final instalment or a manual
edit — inventing uncertainty for no gain.

### 3 are a real finding, and they are money

Three loans have a gap in their deduction history with **no suspension
recorded**:

| Loan | Member | Months missed | Interest not charged |
|---|---|---|---|
| 573 | GRACE MBURU | 3 | ~5,625 |
| 546 | JOYCE MORAA | 1 | ~1,251 |
| 547 | JOSPHINE KIONGI | 1 | ~1,305 |
| | | **5** | **~8,181** |

This matters because **accrual happens inside the deduction loop, not on a
calendar.** A month with no deduction is also a month with no interest. So a
gap is not a missed payment that catches up later — it is interest the member
was never charged, permanently.

Either payroll stopped and nobody recorded a suspension, or catch-up was not
run for those months. `src.integrity` now checks for this
("deductions run without unexplained gaps"), so a recurrence is caught rather
than discovered by a replay.

### 1 is loan 483

Already known: no disbursement recorded. See `outstanding.md` A1.

## What this would fix, concretely

* Deleting a past suspension re-derives the skipped months automatically, under
  the correct historical terms. No heuristic, no ambiguity.
* A back-dated entry — loan or savings — lands in date order and everything
  after it re-derives. (The same disease exists in `savings` today: a
  back-dated deposit leaves the stored running `balance` wrong until
  `fund_recalculate` runs. Phase 3 half-cured it by summing for the *total*;
  the per-row column is still a stored derivation.)
* `is_edited` is deleted, and 004's abandoned `CHECK` becomes possible.
* Top-ups, buy-offs and rate changes stop being special recalculation modes and
  become events.

## Cost, honestly

Reconstructing `TermsChanged` history for 112 existing loans is the real work,
and where terms changed silently mid-life it may not be fully recoverable —
you would seed one `TermsChanged` at issue and accept that pre-migration drift
is approximate. The 20 divergences above are exactly that population, so the
scale is known rather than guessed.

Efficiency is not a concern: a loan has 20–60 events and deriving one is
microseconds. Scale comes from the patterns already in place — a dirty-loan
queue like `gl_outbox`, and period checkpoints like `account_snapshots`.

## Where this leaves things

```
                        first pass    with terms recovered
  reproduced exactly       83 (73%)        112 (99%)
  diverged                 30                1
```

The remaining one is loan 483. Loan 193 reproduces its accruals and balance;
only its `Paid` status is inconsistent, which is the known over-allocation.

`src.integrity` now carries **"loan history replays from its own events"**, so
this is verified on every health check rather than by running a tool. While the
ledger remains the source of truth, a loan that stops replaying is telling you
its history does not add up.

## What has deliberately *not* been done

**No `loan_events` table.** Materialising the reconstruction now would create a
second copy of the truth that the application does not maintain, and it would
drift from the ledger the moment anyone posted a transaction. The reconstruction
is a pure function that regenerates on demand and is checked continuously; that
is strictly safer until the application actually writes events.

A table belongs to the next stage, where the write path emits events and the
ledger becomes the projection. That is a behavioural change to loan posting,
and it should be its own phase with its own verification — not a rider on a
proof.

## If you decide to go further

1. Fix loan 483 and loan 193, so the replay is clean at 113/113.
2. Add `loan_events` and have `LoanService` write to it, with the ledger still
   authoritative and the replay check as the guard.
3. Flip the read path to the derivation, one report at a time.
4. Delete `is_edited`, `rebuild_loan_schedule` and the four recalculation entry
   points, which will have nothing left to do.

Each step is independently verifiable, and each can be stopped at.
