# Phase 3 — Business Logic Cleanup

One authoritative implementation per balance. Unlike Phase 2 this phase
**changes numbers** — that is its purpose, because the duplicates disagreed and
some of them were wrong.

| Sub-phase | Status | Scope |
|---|---|---|
| **3a** | **done** | Savings and fund balances (finding D1) |
| 3b | next | Loan and interest balances — the `df[...].iloc[-1]` pattern, 17 sites |

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

## 3b — loan and interest balances (next)

The same shape of problem on the lending side: 17 sites read a member's current
position with

```python
df["balance"].iloc[-1] if not df.empty else 0.0
df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
```

each re-deriving "the latest running balance" from a DataFrame, with its own
empty-frame and missing-column handling. Two known consequences are already
documented and pinned:

* `loans.balance` drifting from the ledger it is derived from (the Repair Loans
  tool exists for this);
* `restructure_loan` followed by `buyoff_loan` returning 0.00 and leaving the
  loan `Active`, because the buyoff reads the ledger while `loans.balance` still
  carries the amount.

Both need one accessor for "this member's current principal/interest position",
and both change numbers, so 3b wants the same measure-then-apply treatment.
