# Phase 3 — Business Logic Cleanup

One authoritative implementation per balance. Unlike Phase 2 this phase
**changes numbers** — that is its purpose, because the duplicates disagreed and
some of them were wrong.

| Sub-phase | Status | Scope |
|---|---|---|
| **3a** | **done** | Savings and fund balances (finding D1) |
| **3b** | **done** | Loan and interest balances — the `df[...].iloc[-1]` pattern |

---

## 3a — savings and fund balances

### What was wrong

Five implementations, three semantics (finding **D1**). They disagreed on:

| | Ordering | `Interest` | Rounding |
|---|---|---|---|
| `recalculate_savings_balance` | `(date, id)` | **increase** | none |
| `recalculate_savings_balances` | `id` alone | **decrease** | none |
| `SavingsService.recalculate_user_savings` | `(date, id)` via pandas | **decrease** | none |
| `fund_recalculate` | `(date, id)` | increase | 2dp |
| import-path `recalc` | `(date, id)` | increase | 2dp |

And the balance was *read* from the `balance` column of the most recent row —
a stored snapshot that goes stale the moment a back-dated row is inserted.

### It was not theoretical

The production journal has no `Interest` savings rows, so the semantic
disagreement was dormant. **The ordering disagreement was not.** Three members
have a back-dated row inserted after later-dated ones, and for two of them the
reported balance was simply wrong:

| Member | App showed | Correct | Error |
|---|---|---|---|
| 43 FREDRICK KARIMI | 60,000.00 | 80,000.00 | **understated 20,000** |
| 64 TABITHA KAVUA | 61,445.00 | 41,445.00 | **overstated 20,000** |

The two errors cancel in the total (18,758,789.00 either way), which is exactly
why the GL control account reconciled and nobody noticed. Member 64's case is
the clearest: their most recent row *by date* is a 2026-06-01 deposit, but a
2026-05-28 withdrawal of 20,000 was entered afterwards, so the snapshot the app
read predates a withdrawal that had already happened.

### What changed

* **`FundRepository.recalculate(table, member_id, cursor=None)`** — the one
  implementation. Ordered `(date, id)`, `Withdrawal` decreases and everything
  else increases, rounded to 2dp at each step. `Interest` increases, matching
  `GLService`, which credits member deposits when interest is paid on them.
* **`FundRepository.balance(table, member_id)`** — summed in one query instead
  of read off the last row, so it cannot go stale.
* The four entry points (`recalculate_savings_balance`,
  `recalculate_savings_balances`, `fund_recalculate`,
  `SavingsService.recalculate_user_savings`) all delegate to it, and a test
  asserts they agree.

The stored `balance` column remains — it is the per-row running total the
ledger view displays — but it is now derived, not authoritative.

### Effect on the production journal

Measured on a copy before applying:

```
reported balance changes:  2 of 119 members  (+20,000 / -20,000, total unchanged)
stored running column:    13 of 1,422 rows corrected, across 3 members
transaction amounts:      unchanged
GL 2000 Member Deposits:  18,758,789.00, unchanged
```

No amount was altered. The reported balance is now equal to the sum of the
member's own transactions for all 119 members, which was previously true for
117 of them.

### Tests

`test_running_balance_follows_date_order_not_insertion_order` is the regression
guard for the bug that corrupted the two members;
`test_every_entry_point_agrees` pins that the four call sites cannot drift
apart again; `test_balance_survives_a_stale_stored_column` pins that a reported
balance is right even when the display column has not been recalculated.

The two tests that previously asserted the *contradictory* answers (900 versus
700 on the same three rows) are replaced — that divergence no longer exists.

### Rollback

`git revert` the 3a commit restores the previous reads. The recalculated
`balance` column values are not reverted by that, and should not be: they now
agree with the transactions they are derived from. To restore the old stored
values, restore the pre-change backup named in the commit message.

---

## 3b — loan and interest balances

### The duplication

Six call sites across the loan lifecycle — issue, catch-up, single deduct,
top-up, buyoff, and the engine's rate refresh — each re-derived the member's
current position from a DataFrame:

```python
last_bal   = df["balance"].iloc[-1] if not df.empty else 0.0
last_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
last_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
```

Now one `LedgerPosition` and one `position_from_frame(df)`, plus
`LedgerRepository.current_position(member_id)` for callers that do not already
hold the frame. The column guards are kept rather than dropped: a frame read
from an older journal genuinely may not carry the split columns.

Behaviour-identical — both differentials (the 30-operation lifecycle and the
edit/undo/replay sequence) match the previous commit exactly.

---

## The finding 3b uncovered — measured, not acted on

Consolidating the *reads* does not make them current. All six read a **stored**
running total, which is only as fresh as the last recalculation. Running the
application's own `recalculate_balances()` across every member on a copy of the
production journal moves real numbers:

```
ledger rows changed : 146 of 2,192
  balance             146 rows
  interest_balance    146 rows
  deducted / added / portions   0 rows   <- no payment amount moves
members affected    : 12 of 84
loans rows changed  : 12  (interest_balance only; balance and status unchanged)
GL 1100             : 8,513,734.18, unchanged
sum(loans.balance)  : 8,513,734.18, unchanged
```

So twelve members carry stale accrued-interest figures. Nothing is lost — no
amount, no principal, and the control account still reconciles — but their
displayed interest is out of date.

**Not applied.** Some of the corrected loan-level values are *negative* accrued
interest (−7,696.05, −5,429.45, −1,008.00), which means more interest was
allocated in repayments than was ever accrued on those loans. That is a
data-quality question underneath the staleness, and mass-recalculating would
bury it rather than answer it. Recommended sequence: investigate the negative
accruals first, then recalculate.

### A note on measuring

The first two attempts to size this were wrong, in ways worth recording. The
initial probe ordered by `date, id DESC` — which is not "the last row" — and
replayed `added − deducted` **per member**. Both were wrong:
`BalanceRecalculator` groups by `loan_id` and runs a separate balance per loan,
and it decrements by `principal_portion`/`interest_portion`, not by `deducted`.
Those mistakes reported 1,131 stale rows across 36 members. Running the
application's own recalculator and diffing gives 146 across 12.

The lesson generalises: on a financial journal, measure with the code that owns
the definition, not with a reconstruction of it.
