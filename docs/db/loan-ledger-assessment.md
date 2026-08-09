# The loan ledger: is it over-engineered?

Short answer: **yes, and it is measurably wrong because of it.** Four
recalculators exist, three of them disagree, and which answer a user gets
depends on which button they pressed.

Everything below is measured on this repository and the production journal.

## 1. The size of it

| | lines |
|---|---|
| `services/loan_service.py` | 910 |
| `services/balance_calculator.py` | 638 |
| `services/undo_manager.py` | 510 |
| `services/transaction_manager.py` | 379 |
| `db/ledger.py` | 349 |
| **`views/ledger.py` (the UI)** | **2,852** |

Of that, **1,009 lines are loan calculation** spread across two modules:

```
   88  recalculate_balances
  174  recalculate_loan_history
  266  recalculate_smart_loan_ledger
   38  rebuild_loan_schedule
  231  catch_up_loan
  119  deduct_single_loan
   34  _recalculate_unearned_from_ledger
   12  recalculate_default_deduction (x2)
   47  mass_catch_up_loans
```

The fund side, after the posting pipeline, is **411 lines total** (`db/fund.py`
214 + `services/posting.py` 197) and has **one** write path. The ledger has
three.

## 2. The proof that it is over-engineered

Take one loan: 100,000 principal, 15,000 total interest, 5,750/month. Catch it
up five months, then insert a **back-dated 20,000 lump sum** dated between two
existing rows — the loan analogue of the savings case.

Correct answer: `100,000 − (5 × 5,000) − 20,000 = **55,000**`.

| what you call | answer |
|---|---|
| `recalculate_balances` (22 call sites) | **55,000** ✅ |
| `recalculate_loan_history` (7 call sites) | 75,000 ❌ |
| `recalculate_smart_loan_ledger` (3 call sites) | 75,000 ❌ |
| `derive()` (the pure function) | **55,000** ✅ |

Two of the three ignore the back-dated payment entirely. **32 call sites are
split across implementations that do not agree**, and nothing in the code says
which is authoritative.

## 3. What happens today if you back-date a loan deduction

Worse than savings did. The row **sorts into the right position** — `ORDER BY
date, id` — but nothing else moves:

```
2025-03-01  Repayment        -5,750   bal 90,000
2025-03-15  Repayment       -20,000   bal      0     <-- inserted, balance nonsense
2025-04-01  Interest Earned    +750   bal 90,750     <-- carries on as if it never happened
2025-04-01  Repayment        -5,750   bal 85,000
...
loans row: balance 75,000   (should be 55,000)
```

So the position is right and every number is wrong, including the cached loans
row. Savings had this too; the difference is that savings had **one** repair
function and the loan side has four that disagree.

## 4. Can the savings approach apply? Yes — and it already exists

The savings fix worked because a running balance is a pure function of the
ordered series. A loan is harder — interest accrual depends on order, a payment
splits by the balance *at that moment*, a top-up changes the terms — but that
function has already been written and proved:

`src/services/loan_derivation.py` (298 lines) + `src/services/loan_events.py`
(150 lines) = **448 lines replacing 1,009**, and it reproduces
**111 of 111 production loans exactly**.

Tested against the same back-dated cases:

| case | `derive()` |
|---|---|
| back-dated 20,000 lump sum | 55,000 ✅ |
| back-dated 50,000 top-up | 125,000 ✅ (`100,000 + 50,000 − 25,000`) |

**Cost: 0.36 ms per loan.** Re-deriving *every* loan in the production journal
is **~40 ms** — so "recalculate the affected loan on every write" is free, and
even "recalculate everything" would be imperceptible.

## 5. Why it grew this way

The four recalculators are not redundancy for its own sake. Each was written for
a case the previous one could not express, because `ledger` mixes **facts**
(a repayment payroll took) with **consequences** (an accrual the app generated)
and nothing marks which is which. So each new requirement meant a new function
that guessed differently about what it was allowed to regenerate:

* `recalculate_balances` — re-adds the running column, keeps every row;
* `recalculate_loan_history` — replays splits and accruals from current terms;
* `recalculate_smart_loan_ledger` — "smart replay", re-simulates from issue;
* `rebuild_loan_schedule` — deletes every repayment and re-runs catch-up.

The event model removes the guess: an asserted event is an input, everything
else is output, and there is one function.

## 6. The UI table

`views/ledger.py` is 2,852 lines and 75 methods. The two largest are
`refresh_table` (293) and `on_item_changed` (153) — that is **446 lines of
table plumbing**, and `on_item_changed` is where an in-place cell edit decides
which recalculator to call. It is the single place where the divergence above
becomes visible to a user.

The table widget itself is not the problem, exactly as with savings: ordering
was one parameter and the running totals were one shared post-write step. What
makes the loan table hard is that it edits cells that are *derived* — the
balance column, the principal/interest split — as though they were facts.

## 7. What I would do, in order

1. **Pick one recalculator.** `recalculate_balances` is the one that is right on
   the back-dated case and has 22 of the 32 call sites. Point the other 10 at
   it and delete 440 lines. Low risk, immediate correctness win, no schema
   change.
2. **Make the loan write path repair like the funds do** — a shared post-write
   step, so a back-dated row cannot leave the rows below it stale.
3. **Then** the event model, which makes the derived columns read-only in the
   UI and removes the "which recalculator?" question permanently.

Steps 1 and 2 are independently valuable and do not commit to step 3.

## 8. The honest caveats

* `derive()` is proved **read-only**. Nothing writes through it yet, and a
  write path has its own risks that a replay harness cannot exercise.
* Terms history is *inferred* from the accruals, which works because the app
  posts `min(monthly_interest, unearned)` monthly. A loan whose terms changed
  without producing a differing accrual would not be recoverable — the 26
  top-up/buy-off loans were, but that is evidence rather than a guarantee.
* The 0.36 ms figure is for a 7-event loan. The largest production loan has 23
  ledger rows, so the real figure is a small multiple of that, not an order of
  magnitude.
