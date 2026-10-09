# Deleting months in the Ledger Grid

What happens, row by row, when a month column — or several — is deleted in the
Ledger Grid, and why each rule is the one it is. This is the specification; the
code in `src/services/fund_grid.py`, `src/services/loan_grid.py` and
`src/views/fund_grid.py` implements it, and `tests/test_grid_delete_months.py`
holds it to it.

---

## 1. What "deleting a month" is

A month column is a calendar month. Deleting it removes **the transactions
recorded in that month for the rows on screen**. It does not remove the month —
the column stays, empty — and it touches nothing outside that month except what
is *derived* from it (running balances, splits, schedules), which is recomputed.

It is reached three ways, all of which end in the same place:

| Gesture | Result |
|---|---|
| Right-click a month's header → **Delete *month*…** | preview, then stage |
| Select whole column(s) (click the header) and press **Delete** | preview, then stage |
| Select some cells and press **Delete** | stage those cells (no preview) |

Like every grid edit, a deletion is **staged** (the cells turn orange) and
written only by **Save Changes**, as one transaction, with **Undo Last Save**
able to put it back exactly. The preview exists because a column is hundreds of
rows and the consequences differ by ledger and by loan; it says what will happen
before anything is staged.

## 2. Which rows

**The rows on screen, and only those.** That is the rule for every grid
operation (paste, clear, fill), and a column delete follows it:

* the search filter, *Include past members*, *Show excluded* and (Loans)
  *Include paid loans* decide what is on screen;
* excluded rows are read only and are never deleted, even when shown;
* the preview counts what is **not** touched in those months — rows not shown,
  postings the grid has no operation for, loan events that are not deductions —
  so nothing is left behind silently.

To delete a month for everybody, show everybody first.

---

## 3. Funds — Savings, Christmas, Benevolent

### 3.1 What is deleted

Every row in the month that a grid operation describes: deposits /
contributions and withdrawals / payouts. A month holding a row no operation
describes (an `Interest` posting) is left whole and reported — the grid does not
delete what it cannot show or put back.

### 3.2 What follows from it

* **Running balances** of every later row are recalculated. A fund balance is
  the sum of its rows; nothing else stores it.
* **The month becomes a missed month.** Every member is assumed to save every
  month, so a month with no deposit is a gap: Fill Forward will offer to fill it
  again (if it has passed).
* **Catch-up only moves forward** from the last deposit. Deleting the *latest*
  months lets catch-up re-post them; deleting earlier months leaves gaps that only
  Fill Forward (or typing) fills.
* **Benevolent schedule.** `benevolent_accounts.next_due_date` is re-derived from
  the last remaining contribution. Deleting the latest months moves it back
  (those months are owed again); deleting earlier months does not move it.
* **Audit.** Every deleted row's before-image is written to `audit_log` by the
  database triggers, with the time it happened.

### 3.3 The one refusal: a withdrawal left overdrawn

On **Savings** and **Christmas** the money is the member's own: the Withdraw
dialog refuses a withdrawal bigger than the balance. A deletion can produce the
same contradiction after the fact — delete January to March's deposits under a
December withdrawal that paid them out, and the record says the member withdrew
money they never had.

So a save may not leave any withdrawal taken from more than the balance at the
time. When it would, **all of that member's changes in the save are refused**,
with the withdrawal named, and every other member's changes go ahead. Include
the withdrawal's month in the deletion, or correct it in the member's ledger.

* This applies to every grid save on those two funds, not only deletions — a
  typed withdrawal is held to the same rule as the Withdraw dialog.
* A balance that was already overdrawn before the save is not a reason to
  refuse; only a save that makes it worse is.
* **Benevolent is exempt.** A payout is a welfare claim on a pooled fund and may
  exceed what the member contributed.

### 3.4 Christmas withdrawals

Deleting a Christmas withdrawal is allowed in any month. The unlock-month rule
governs taking money *out*; removing a withdrawal takes nothing out.

---

## 4. Loans

The loan grid is a grid of **deductions**, and deleting a month deletes
deductions — `Repayment` rows. Everything about a loan's later history is
derived from its rows, so what a deletion means depends on where in that history
the month sits.

### 4.1 What is never deleted

| In the deleted month | Behaviour |
|---|---|
| `Loan Issued`, `Loan Top-Up`, `Loan Restructure` | Not touched. They are not deductions. Reported in the preview. |
| Suspension records | Not touched. |
| `Loan Buyoff` | Not deleted; the loan is **refused** (§4.3). |
| A month with two deductions | Not deleted; reported (open the ledger). |

### 4.2 Two kinds of deletion

Each deleted deduction is one of two things. Which one is decided by what comes
after it on the same loan.

**Un-run — the deduction is the loan's last entry.**
The month is undone as if it had never been run:

* the deduction **and that month's interest charge** are removed;
* the interest goes back to the loan's **unearned interest**;
* the **due date moves back** to that month.

The month is due again. Deduct, Fill Forward and catch-up will take it again,
and if it has passed, the loan shows in the arrears report. This is the exact
inverse of Deduct.

**Missed — the loan has later deductions.**
The deduction did not happen, but the months after it did:

* **only the deduction is removed.** The month's interest charge stays — the
  month was due, and the interest was earned;
* **every later deduction on the loan is re-split, interest first**, so the next
  one settles the unpaid interest before any principal;
* later **balances rise by the missed principal**;
* the **due date does not move** — the schedule is current — and the shortfall
  stays in the balance. Deductions continue past the last scheduled month until
  the balance is cleared, so the loan **runs one instalment longer**.
* A loan that the deleted deduction had finished **reopens** (`Active`).

Because the interest is fixed when the loan is written, a missed month does not
change the total interest the member pays; it changes when the principal is
repaid.

**Why the two differ.** The loan's history is replayed by one derivation
(`src/services/loan_derivation.py`), which charges interest month by month up to
the loan's last entry. Removing a charge from the *middle* would leave a month
the schedule says was charged and the ledger says was not, and the loan would
stop reconciling with its own history. Removing it from the *end* is precisely
what undoing a deduction is.

**Not a payment holiday.** If the month was a holiday — nothing owed, term
extended, no interest for it — that is a suspension, not a deletion. Use `@S`
in the grid or Suspend in the ledger.

### 4.3 When a loan's deletion is refused

| Situation | Why |
|---|---|
| A **buy-off** on or after the month | The settlement figure was worked out from a history that included this deduction. Reverse the buy-off in the member's ledger (Delete Last restores the loan) first. |
| The deduction is the loan's last **deduction**, but a **top-up or restructure** follows it | The top-up re-termed the loan from the balance this deduction left. Un-running past it would put the due date before a re-terming; recording it as missed would charge the month's interest twice when it is next deducted. Remove the top-up in the ledger first, or keep this month. |
| The loan **does not reconcile** with its own ledger (its rows already disagree with its schedule) | Every consequence above is worked out by re-deriving the loan, and this one cannot be re-derived: its cached balance would stop following its ledger. Correct its history in the member's ledger first. The same refusal applies to correcting a deduction or recording a missed month on such a loan. |

A refusal holds back **all** of that loan's deletions in the save — half of a
loan's months deleted is not what anybody asked for — and the other loans go
ahead.

### 4.4 Several months at once

A loan's deletions are applied **newest month first**, and each is judged
against the loan as the earlier ones have left it:

* deleting the last three months un-runs all three — each becomes the last
  entry in turn — and the due date moves back three months;
* deleting March and July where July is the last: July is un-run, March is
  missed (April–June remain).

Within one save, a loan's deletions run before its corrections, and both before
any new deductions or operations; those are checked against the loan as the
deletions left it (a deduction staged for a month the loan is no longer due for
is refused, not misplaced).

### 4.5 Interest is conserved

For every loan, *interest charged so far + unearned interest* equals the
interest the loan was written for (its issue plus any top-ups). Un-running a
month moves that month's charge back to unearned; undoing it moves it out again.

> Before this specification, removing a deduction from the grid — including
> Undo Last Save of a deduction — deleted the month's interest charge without
> returning it to unearned interest. Each removal silently forgave one month's
> interest. That is fixed, and `test_interest_is_conserved_*` holds it.

### 4.6 Putting a missed month back

A **missed month** — interest charged, nothing paid, before the due date — is
shown as a red dash. Typing a figure there records the deduction in that month,
and later deductions are re-split again. It is the inverse of §4.2's *missed*,
so a month deleted by mistake can be restored later, not only by Undo.

A month before the due date with **no** interest charge (passed over, never
run) still cannot be typed into: what it would have charged depends on the
balance at the time, which is the derivation's job.

### 4.7 Paid loans

Paid loans are on screen only with **Include paid loans** ticked. Deleting a
paid loan's deduction reopens it: un-run makes its last month due again; missed
leaves a shortfall to collect.

### 4.8 The health check agrees

*Mass Operations → Journal Health Check* flags a month with **neither** a
deduction **nor** an interest charge as an unexplained gap — interest never
charged. A missed month was charged, so it is not a gap; the check counts it
("N missed deduction(s) recorded") instead of warning about it.

### 4.9 What is kept on the rows that remain

The amount, date and type of every remaining row is an assertion and is never
changed — including amounts entered by hand. Only derived values move: running
balances, the principal/interest split, and the loan's cached balance, interest
balance, status and due date.

---

## 5. Undo Last Save

Exact, and in reverse order:

* **Funds** — deleted rows are re-inserted **with their original ids** and
  every column, including a creation time that was never recorded (the insert
  trigger would otherwise stamp it "now"). Rows sharing a date are ordered by
  id, so a deposit and a withdrawal on the same day keep their order — and the
  balance between them. Balances and the benevolent schedule are re-derived.
* **Loans** — rows are restored **with their original ids**, so their order
  within a date — which the derivation relies on — is what it was:
  * un-run: the interest charge and the deduction come back, and the interest is
    taken back out of unearned;
  * missed: the deduction comes back and the later deductions get back the exact
    splits they had before.

---

## 6. Worked examples

A loan of 100,000 written at 15% over 20 months: instalment 5,750, interest 750
a month, issued in January and deducted February to June.

**Delete June** (its last entry) — *un-run*:

| | Before | After |
|---|---|---|
| June | 750 interest, 5,750 deducted | nothing |
| Principal | 75,000 | 80,000 |
| Unearned interest | 11,250 | 12,000 |
| Due | July | **June** |

**Delete March** (April–June follow) — *missed*:

| Month | Before: deducted (interest / principal) | After |
|---|---|---|
| March | 5,750 (750 / 5,000) | 750 interest charged, **nothing paid** |
| April | 5,750 (750 / 5,000) | 5,750 (**1,500 / 4,250**) |
| May | 5,750 (750 / 5,000) | 5,750 (750 / 5,000) |
| June | 5,750 (750 / 5,000) | 5,750 (750 / 5,000) |
| Principal after June | 75,000 | **80,750** — the missed 5,750 |
| Due | July | July |

Total interest is still 15,000; the loan needs one more instalment at the end.

**Savings: delete March for a member who withdrew 10,000 in June**, having
saved 2,000 a month from January: the June withdrawal was taken from 12,000;
without March it would come from 10,000 — still covered, so the deletion goes
ahead. Delete January to March instead and it would come from 6,000: the member's
deletions are refused, naming the June withdrawal.

---

## 7. Verified against the production journal

A copy of `loan_master.db` (9 Oct 2026; 94 active loans, 120 members), each
deletion saved and then undone:

| Deletion | Result |
|---|---|
| Loans, **Jun 2026** (a middle month) | 77 missed, 1 un-run; 1 loan refused — it does not reconcile with its own ledger. Every loan touched still replays; interest charged + unearned unchanged on every one. |
| Loans, **Aug–Oct 2026** (latest months) | 1 un-run, 3 missed. |
| Loans, **every month Nov 2025–Oct 2026** | 631 un-run, 12 missed; 8 loans refused (7 with a top-up after their last deduction, 1 unreconciled). |
| Savings, **Mar 2026** | 113 deposits (327,500); 1 member refused — a June withdrawal of 217,171 had taken the whole balance. |
| Savings, **Nov 2025–Mar 2026** | 565 deposits and 9 withdrawals; 2 members refused for the same reason. |

In every case Undo Last Save put back every row and column exactly — original
ids, splits and running balances to the cent — apart from `loans.updated_at`,
which records that the loan was written. The health check reported the same
warnings before and after, with the missed months counted, not flagged.

## 8. Alternatives considered

* **Delete every row in the month, whatever it is.** Rejected: deleting a
  `Loan Issued` row deletes the loan; deleting a top-up cascades through
  everything after it. A grid of deductions deletes deductions.
* **Missed months move the due date back** ("paid in order" — later deductions
  pay for earlier months). The derivation charges interest by calendar month, so
  a loan whose due date lags its own charged months would charge some months
  twice. Rejected in favour of carrying the shortfall.
* **Treat a middle deletion as a suspension** (remove the charge, extend the
  term). That is a payment holiday — a business decision with its own record —
  and it would hide a real missed payment. Available, deliberately, as `@S`.
* **Act on hidden rows too.** Rejected: every other grid operation acts on what
  is shown, and a deletion that reached rows the operator could not see would be
  the one exception. The preview counts them instead.
* **Refuse every middle deletion** (the previous rule). Safe, but it made a
  column delete half-work: some loans cleared, others refused, depending on
  history the operator could not see.
