# LoanMaster 2.0: the Rust port

LoanMaster 2.0 is a rewrite of the 1.x Python/PyQt application in Rust. It
opens the same journal file and keeps the same accounting rules. This page
records how the port is checked, and every place where 2.0 behaves differently
from 1.x on purpose.

## Layout

| Crate | Holds |
|---|---|
| `crates/core` | The journal, loans, funds, the Ledger Grid, policy and undo. No user interface. |
| `crates/app` | The egui desktop application (in progress). |

## The journal

* The schema in `crates/core/src/schema.sql` is byte-identical to the one 1.x
  creates at version 12, so 1.x and 2.0 can open the same file. Loan snapshots
  (`ledger.previous_state`) are written as the same JSON, so either version can
  restore from rows the other wrote.
* 2.0 opens only version-12 journals. It refuses an older journal instead of
  migrating it, and says so: open the journal once in 1.x first, which brings
  it up to date. A backup older than version 12 is refused for the same reason.
* Money stays in `REAL` columns. `money::round2` matches Python's
  `round(x, 2)` exactly, including round-half-even on the binary value.

## How parity is checked

During the port, a script of loan operations is applied to two copies of the
production journal: one by 1.x and one by `crates/core/examples/replay_ops.rs`.
The ledger, the loans and the members are then compared column by column.

| Scenario | Result |
|---|---|
| Catch-up of every loan to 2026-10-31 (3,117 ledger rows, 124 loans) | identical |
| 126 mixed steps: issues, deductions, catch-ups, top-ups, buy-offs | identical figures (notes differ only where the harness passed whole numbers) |
| 28 Ledger Grid steps: Fill Forward, deleting months (un-run and missed), missed months recorded, `@S`, `@T`, `@B`, corrections, Undo Last Save | identical ledger, loans, members and suspensions |

The scenarios stop short of the cases listed below, where 2.0 differs on
purpose.

## Removed

* **Mass Deduct, Mass Increment and the other mass operations.** The Ledger
  Grid's Fill Forward does the same work and shows every figure before saving.
* **Journal Health Check and the repair and scan tools.** New journals cannot
  get into the states they repaired. Existing journals are corrected by hand.
* **Loan restructuring.** 1.x had the code but no screen that called it, and the
  production journal holds no restructure rows. 2.0 still reads restructure
  rows, so an older journal that has them still derives correctly.

## Deliberate differences from 1.x

### Interest rates

* **A top-up charges interest at the loan's own rate.** 1.x always used 15%,
  including on a loan written at another rate. The rate is the loan's
  `interest_rate`, or the journal's default rate when the loan has none.
* **Rebuilding a loan's schedule resets unearned interest at the loan's own
  rate**, for the same reason.
* **Editing a top-up** re-terms the loan as if the top-up had been made with
  the corrected figures, at the loan's own rate. 1.x used 15%, read the balance
  from the member's previous ledger row (which can belong to another loan), and
  spread only the top-up's interest over the new term.

### Correcting the member's ledger

* **Deleting a deduction follows the Ledger Grid's rules**
  ([deleting-months.md](grid/deleting-months.md) §4). The loan's last
  deduction is *un-run*: the deduction and that month's interest charge go,
  the interest returns to unearned, and the month falls due again. An earlier
  deduction is recorded as *missed*: only the payment goes, the interest stays
  charged, and later deductions are re-split. 1.x removed the payment and its
  interest charge from anywhere in the history, then restored the loan's figures
  from the deleted row's snapshot. When later rows remained, that left them
  out of step with the loan.
* **Deleting a top-up or restructure removes every later entry on the loan by
  date.** 1.x chose the rows by id, so a back-dated entry could be missed or
  wrongly included.
* **Deleting the issue deletes the loan.** 1.x deleted the rows but kept the
  loan's record, leaving a loan with no history.
* **Changing a repayment ("sticky" deduction)** settles interest from the same
  loan's previous row and keeps the loan's total. 1.x read the member's
  previous row of any loan, and set the loan's total amount to zero.
* **Catch-up records each month's snapshot as the loan stood before that
  month.** 1.x stored the state after the month's interest accrual, so
  restoring from it lost a month of interest.

### Undo

* **Undo puts the whole loan back**: the loan, every ledger row on it with its
  original ids, and its suspension spans. 1.x restored only the deleted row and
  its pair, so undoing a top-up deletion did not bring back the entries
  deleted with it.
* **Undo writes only what differs.** Rows that are already correct are not
  touched, so the Activity Log shows what the undo changed and nothing else.

### Ledger Grid

* **Undo Last Save on the Loans tab refuses when a loan has changed since the
  save**, for example by a deduction taken in the member's ledger. Nothing is
  undone and the loan is named. 1.x reversed the save anyway, which could undo
  the later work too.
* **A corrected deduction is re-derived before the next month in the same save
  is taken.** In 1.x a later deduction in the same save worked from the balance
  before the correction. This changed the result only when the correction and
  the loan's final month were in the same save.
