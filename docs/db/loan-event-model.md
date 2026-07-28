# A cleaner loan model — proposal, with evidence

Status: **read-only proof.** `src/services/loan_derivation.py` and
`tools/replay_loans.py` exist and are tested; nothing in the running
application calls them, and no schema has changed.

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
    13  has a top-up (terms changed mid-life, not stored)
     7  has a manually edited entry
     6  has a buy-off
     3  accrual schedule differs
     1  no disbursement recorded
```

**73% replay exactly from events alone**, with no stored intermediate state —
no cached balance, no `next_due_date`, no running totals. That is the evidence
the model is sound.

The divergences are the interesting part, because they are not random:

* **13 top-ups and 7 manual edits — 20 of the 30 — are the two things the
  model adds.** The harness has to read terms from the *current* `loans` row,
  since no history exists; a loan whose installment changed at a top-up is
  therefore expected to diverge here and would not diverge under the real
  model. Same for a manual amount, which becomes an asserted `PaymentReceived`.
* **1 is loan 483** (no disbursement recorded), already known.
* **6 buy-offs and 3 accrual differences** are genuinely unexplained and are
  where the next investigation should go.

So the harness does what it was built for: it separates "the model is wrong"
from "this loan's history does not add up".

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

## Next step, if this is worth pursuing

Raise the 73%. Work through the 6 buy-offs and 3 accrual divergences, since
those are unexplained; if they resolve into modelling gaps that `derive` can
express, the case is made. Only then is a migration worth designing.
