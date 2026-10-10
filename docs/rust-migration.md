# LoanMaster 2.0: the Rust port

LoanMaster 2.0 is a rewrite of the 1.x Python/PyQt application in Rust. It
opens the same journal file and keeps the same accounting rules. This page
records how the port is checked, and every place where 2.0 behaves differently
from 1.x on purpose.

## Layout

| Crate | Holds |
|---|---|
| `crates/core` | The journal, loans, funds, the Ledger Grid, policy, undo, report and statement data, imports. No user interface. |
| `crates/reports` | Writing documents: workbooks, CSV, and PDFs typeset with Typst. |
| `crates/app` | The egui desktop application: the dashboard, a member's ledger, the Ledger Grid, and the Reports, Tools and import windows. |

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
| 15 report tables: five quarters of the interest and shares reports, the three fund reports, the members list, arrears | identical, cell by cell |
| 240 statements: every member, over a financial year and over all time | identical, except the gross figures listed under [Statements](#statements) |
| The whole production journal imported into an empty one | all 120 members, 124 loans and 3,116 ledger entries; every loan balance, due date and fund balance identical |
| Every screen of the application over the production journal: each member's ledger on every tab, the Ledger Grid's tabs with Fill Forward, every report and tool window (the opt-in smoke test in `crates/app/src/tests.rs`) | opens without error |

The scenarios stop short of the cases listed below, where 2.0 differs on
purpose.

## Removed

* **Mass Deduct, Mass Increment and the other mass operations.** The Ledger
  Grid's Fill Forward does the same work and shows every figure before saving.
* **Journal Health Check and the repair and scan tools.** New journals cannot
  get into the states they repaired. Existing journals are corrected by hand.
* **Auto Deduct and Auto Increment on the member's ledger.** Deduct (with its
  number of months) and Catch Up take a loan's or a fund's months in order,
  and the Ledger Grid fills any other months.
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

### Loan references

* **A deleted loan's reference is never given out again.** 1.x numbered a
  new loan one past the highest reference the member still held, so
  deleting their latest loan freed its number for the next one. 2.0 also
  counts the loans the audit trail shows were deleted, so `L-003` in the
  Activity Log, a backup or a statement already handed to the member always
  means one loan, and undoing a deletion cannot collide with a newer loan.
  Imported loans follow the same rule. On the production journal this moves
  one member's next loan from L-003 to L-004.

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

### Statements

* **The gross figure uses the loan's own rate.** 1.x used the journal's
  default rate for every loan. On the production journal, a 1,250,000 loan
  written at 0% showed 187,500 of interest it will never be charged.
* **The gross figure is kept to the cent.** 1.x added in binary fractions and
  rounded up, so a loan written at 10% opened one shilling high, and a loan
  paid off in full could show a gross balance of 1.
* **Savings balances are summed from the rows.** 1.x printed each row's
  stored running balance. On the production journal the two agree.
* **A statement covers active loans or all of them, over this financial
  year, the whole history or chosen months.** 1.x printed every loan with an
  entry in the period and started the period at the selected members' first
  entry, so a statement ran from the member's first loan, paid-off loans and
  all.
* **A balance from before the period is carried in** as a "Balance b/f" line
  on each loan and on savings, so a statement that starts mid-history still
  explains its figures. 1.x began with the period's first entry.
* **A member with nothing to show for the period — no entries and no balance
  carried in — is skipped, in either format**, and the summary says so. 1.x
  counted an empty workbook statement as a failure.
* **The member's PF number is printed, and the workbook has the PDF's
  letterhead**: the logo and the society's name, printed landscape and one
  page wide. 1.x's workbook had neither.
* **The default logo is built into the program**, so it prints however the
  program was installed. 1.x read it from a file beside the program.
* **Two members of the same name get a file each.** 1.x wrote both
  statements to one file name, so the second replaced the first.

### Importing from another journal

* **An import is one transaction.** It lands completely or not at all. 1.x
  could stop part-way and report a "partial" import that had to be undone by
  hand.
* **A loan reference changes only when the receiving member already holds
  it**, and then becomes their next free `L-NNN`. 1.x checked references
  against every member in the journal, so nearly every imported loan was
  renamed (`L-001-Import`, `L-001-Import-1`, …). It also looked renames up by
  reference alone, so when two incoming members both had an `L-001`, one
  member's ledger rows could be filed under the other's renamed loan.
* **Christmas, benevolent and suspension rows are checked** like every other
  row. 1.x copied them unchecked. A ledger row whose loan was not imported is
  left out and counted, not left pointing at nothing.
* **A loan keeps its rate, term and suspension.** 1.x dropped
  `interest_rate`, `duration_months`, `is_suspended` and `suspend_until`.
* **The date filter applies to every kind of transaction.** 1.x applied it
  to the ledger and savings only.
* **Undo also removes what was added to merged members**: their Christmas and
  benevolent rows, suspensions and new benevolent enrolment. The schema has no
  `import_id` on those tables, so 2.0 lists them in the run's details. A
  merged member's existing benevolent enrolment is kept, not replaced.
* **Conflicts compare names and emails exactly, ignoring case.** 1.x used
  `LIKE`, so `%` and `_` in a name acted as wildcards.
* **A member who has left with no leaving date is recorded as leaving on the
  import date**, so their status, retirement flag and date agree. 1.x dropped
  the flag but kept the status, which made any later status edit fail.
* **Import History is on the File menu and the dashboard's Import Data
  button.** 1.x had the window, with its undo, but nothing opened it.

### Importing from a spreadsheet

* **The safety copy is taken with SQLite's backup API, and the import is one
  transaction.** 1.x copied the database file, which can miss changes still in
  the write-ahead log, and wrote row by row.
* **A PF or ID number another member holds is skipped, never cleared.** 1.x
  wrote the member's own number as blank when the sheet's number clashed.
* **An employment status is read onto the vocabulary** ("employed" is
  Active). In 1.x an unrecognised one stopped the import part-way.
* **A benevolent sheet moves the member's schedule past what it recorded**,
  as the Ledger Grid does, so the next catch-up does not post those months
  again.

### Settings and backups

* **Workbook colours are part of Settings**, and Reset Colours restores the
  colours a new journal starts with. 1.x reset them to a different pair.
* **Restoring a backup clears the undo history.** 1.x kept it, so an undo
  after a restore could apply to entries the restore had replaced.

### Reports and documents

* **A quarterly report's period names the quarter's last month.** 1.x printed
  "Aug 2025 - Nov 2025" for August to October.
* **PDFs are typeset by the application** (Typst, with Liberation Sans built
  in), not printed from a web view. A PDF no longer falls back to an HTML file
  when printing fails.
* **Money in report PDFs is grouped by thousands** (1,234,567). 1.x printed
  whole-shilling columns ungrouped.
