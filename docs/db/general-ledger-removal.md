# Removing the general ledger (migration 011)

The Treasury / double-entry GL subsystem was withdrawn at the owner's request,
with the consequences below stated and accepted beforehand.

## What went

| | |
|---|---|
| `services/gl_service.py`, `db/journal.py`, `views/treasury.py` | the engine, its SQL, its screen |
| `services/provisioning.py` | SASRA loan-loss provisioning (posted to the GL) |
| `services/period_service.py`, `db/period.py` | fiscal periods (closed against the trial balance) |
| `reports.py::generate_financial_statements` | 211 lines — Balance Sheet, P&L, cash flow |
| 4 test modules, ~10 integrity checks | |
| Live data | 25 accounts, 5,327 entries, 11,678 lines |

**Capabilities lost:** Balance Sheet, Profit & Loss, trial balance, cash-flow
statement, SASRA provisioning, fiscal-period closing, and the four
control-account reconciliations that compared the GL against the member
subledgers — the checks that originally found KES 47,500 of Christmas savings
missing from the books.

## What stayed

Everything member-facing: `individuals`, `loans`, `ledger`, `savings`,
`christmas_savings`, `benevolent_ledger`, statements, deductions, catch-up,
both funds, the importer, the audit trail (migration 007), the health check and
the loan replay harness. Member balances and `loans.balance` are unchanged —
8,621,262.18 before and after.

## Two decisions worth recording

**Applied migrations were not edited.** Migrations 001 and 004 index and rebuild
the journal tables, and they are recorded as applied on every journal in the
field. Rewriting that history would have meant the same version number
describing two different schemas. Instead `create_tables` still builds the GL
tables, the historical chain runs over them unmodified, and 011 drops them —
costing a few milliseconds on journal creation.

**That needed a guard.** `create_tables` runs on *every* open, so without one
each reopen resurrected the tables 011 had just dropped, while 011 — already
recorded as applied — would never run again to clear them.
`_general_ledger_removed()` checks the migration record first;
`test_reopening_a_journal_is_idempotent` covers it.

## A limitation this introduces

Migrations are no longer re-runnable out of order past 011: replaying 008–010
on a journal that has had 011 applied fails, because they build objects that no
longer exist. This is normal for a migration that drops things, and only
reachable by editing `schema_migrations` by hand. Restoring a real backup is
unaffected — it carries the schema of its own moment and is brought forward.

## Verified

```
migration 011 applied in 1.44s
member data unchanged: 119 individuals, 111 loans, 2,174 ledger,
                       1,422 savings, 376 christmas, 1,368 benevolent, 170 audit
GL objects remaining : none
integrity_check ok | foreign_key_check clean
26 checks, 0 failures, 0 warnings   (was 39 checks)
loan replay: 111 of 111 (100%)
449 tests, ruff clean
```

## Rollback

Restore `backups/loan_master.pre-pre-gl-removal-20260809-191454.db` and check
out the commit before this one. The journals are deleted, not archived, so the
backup is the only copy.
