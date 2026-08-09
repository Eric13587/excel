# Pipelines: deciding before doing

## The problem this solves

`individuals.is_retired` and `employment_status` existed from early on, and were
read only by `src/reports.py` to decide who appeared on a statement. Nothing
consulted them before *writing*. A monthly catch-up would happily generate nine
months of contributions for somebody who left the SACCO in March.

The obvious fix — an `if` at the top of `catch_up_savings` — would have had to
be repeated at eight catch-up entry points, every mass-operation dialog and
every single-transaction path, each with its own wording, drifting apart, and
adding a second rule would mean finding all of them again.

## The shape

`src/services/policy.py`:

```
Operation(kind, member_id, date, amount, loan_ref)   what is about to happen
rule(db, op) -> Refusal | None                       one question, no side effects
Policy.check(db, op)   -> [Refusal]                  ask
Policy.enforce(db, op) -> raises                     ask, and stop if refused
Policy.partition(db, kind, ids) -> allowed, blocked  ask about a batch
```

Two properties earn their keep.

**Rules are pure.** A rule may read the journal and must not write it, so the
same rule set can grey out ineligible members in a dialog *before* anything runs
(`check`) and stop a write at the point of writing (`enforce`). Same rules, same
wording, two uses. `test_check_never_writes` holds the line.

**A broken rule does not block work.** `check` logs and continues past a rule
that raises. A bug in a guard should not stop the SACCO taking a repayment.

## The rule that matters, and its limit

`member_must_be_active` refuses operations listed in `ACCRUING_OPERATIONS` —
catch-up, mass deduction, new loans, new scheduled contributions.

It deliberately does **not** refuse repayments, withdrawals, payouts or
corrections. Leaving the SACCO does not erase what you owe or what you are owed;
blocking everything would trap a retired member's own savings, which is a worse
failure than the one being fixed.

The refusal reads as a sentence a clerk can act on, and says what is still
possible:

> *Jane Wanjiru has retired (2025-06-30), so no new savings activity can be
> recorded. Repayments and withdrawals are still allowed.*

## Membership status (migration 012)

`employment_status` is authoritative — `Active`, `Retired`, `Resigned`,
`Terminated`, `Deceased`, `Suspended` — with `is_retired` kept as a mirror
because `reports.py` reads it in five places.

`DatabaseManager.set_membership_status()` is the only supported way to change
it, and that is not a stylistic preference: migration 004 constrains
`is_retired` and `retired_date` to agree, and a `CHECK` is evaluated *before* any
`AFTER` trigger can mirror a value, so updating one column alone is rejected
rather than half-applied.

Foreign journals carry free text here, so `import_rules` maps it —
`Employed → Active`, `Pensioner → Retired`, `Dismissed → Terminated` — falling
back to the retirement flag and reporting the translation. An unmapped value
would read as "has left" and silently stop somebody's contributions.

## Where else this shape fits

Ranked by what it would actually buy:

1. **Import** — already a pipeline in all but name. `import_rules.py` has
   `normalise_*` functions that trim, map, repair and reject, each returning a
   `Normalised` with values, warnings and a reason. Giving them a common runner
   would let the preview screen show exactly what the commit will do, from the
   same code that does it. **Highest value, lowest risk.**

2. **Posting a transaction** — validate → normalise → guard → write →
   recalculate is currently spelled out slightly differently in
   `add_transaction`, `add_savings_transaction` and `fund_add_transaction`. The
   back-dated running-balance bug existed in all three and was fixed in each
   separately; a shared post-write step would have fixed it once.

3. **Report generation** — gather → filter (retired, date range) → compute →
   render. The retired-member filter is currently four near-identical blocks in
   `reports.py`, and they do not agree on the edge case of somebody who left
   mid-period.

Not worth it: the loan derivation is already a pure function, and the migration
runner is already a pipeline with its own reporting.
