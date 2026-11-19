# Duplicate and inconsistent SQL

Queries that exist more than once, or that answer the same question two
different ways. Each entry names the call sites, the concrete divergence, and
where the single implementation belongs. Severity reflects whether the
divergence can produce wrong numbers today (**Bug**), can produce wrong numbers
under data the app can create (**Latent**), or is duplication without a
behavioural difference (**Duplication**).

Line references are as of commit `49295cb`.

---

## D1 — Five implementations of "recalculate a member's running balance" · **Latent bug**

| Implementation | Ordering | `Deposit` | `Interest` | `Withdrawal` | Rounding |
|---|---|---|---|---|---|
| `database.recalculate_savings_balance` ([database.py:1425](../../src/database.py#L1425)) | `date ASC, id ASC` | `+` | **`+`** | `−` | none |
| `database.recalculate_savings_balances` ([database.py:1485](../../src/database.py#L1485)) | `id` only | `+` | **`−`** | `−` | none |
| `savings_service.recalculate_user_savings` ([savings_service.py:120](../../src/services/savings_service.py#L120)) | `date, id` (pandas) | `+` | **`−`** | `−` | none |
| `database.fund_recalculate` ([database.py:1542](../../src/database.py#L1542)) | `date ASC, id ASC` | `+` | `+` | `−` | `round(…, 2)` |
| `database._import_aux_tables.recalc` ([database.py:1929](../../src/database.py#L1929)) | `date, id` | `+` | `+` | `−` | `round(…, 2)` |

Three different answers to "what does an `Interest` row do to the balance", and
two different orderings. `savings.transaction_type = 'Interest'` is written by
`GLService.post_savings_interest`'s counterpart flow and is a first-class value
in the enum — it simply has no live rows yet, which is the only reason this
hasn't produced a wrong statement. Whichever recalculation happens to run last
wins.

Consolidate to one `FundRepository.recalculate_running_balance(fund, member_id)`
with the `fund_recalculate` semantics (Interest increases the balance — the only
reading consistent with `GLService._post_savings_event`, which credits member
deposits for it), ordered by `(date, id)`, rounded to 2dp.

---

## D2 — Two definitions of the `savings` table · **Duplication, with a real difference**

`create_tables()` ([database.py:467](../../src/database.py#L467)) declares
`savings` with `import_id`; `create_savings_table()`
([database.py:1372](../../src/database.py#L1372)) declares it with `batch_id`.
Whichever runs first on a fresh journal decides the initial shape; the ALTERs
then patch in the missing one.

Worse, `create_savings_table()` is called from **`add_savings_transaction`,
`get_savings_balance`, `get_savings_transactions` and
`recalculate_savings_balances`** — DDL plus a `commit()` on a read path, inside
loops that run once per member. It is also the reason
`recalculate_savings_balance` carries the comment *"Removed to prevent commit
during transaction"*: the commit was breaking the caller's transaction.

Delete `create_savings_table()` entirely once the schema is created from
`schema.sql`.

---

## D3 — The same loan-restructure probe, written twice · **Duplication**

```sql
SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id=?
  AND event_type IN ('Loan Top-Up','Loan Buyoff')
```

* [loan_service.py:151](../../src/services/loan_service.py#L151) — `has_loan_restructure_events`
* [engine.py:142](../../src/engine.py#L142) — inline inside `_loan_heal_plan`

Character-for-character the same question ("is this loan simple enough to
rebuild/heal?"), maintained in two places. `engine.py` should call the service
method.

---

## D4 — Three ways to clear the interest edit-lock · **Latent bug**

| Call site | Scope |
|---|---|
| `database.unlock_future_interest` ([database.py:1149](../../src/database.py#L1149)) | `event_type='Interest Earned' AND date > ?` |
| `engine.heal_loan_terms` ([engine.py:230](../../src/engine.py#L230)) | `event_type='Interest Earned'` — **all rows, no date bound** |
| `transaction_manager` ([transaction_manager.py:362](../../src/services/transaction_manager.py#L362)) | sets `is_edited = 1` for `date >= ?` |

Two of them unlock, one locks, and only one is date-bounded. Because
`is_edited` is what stops the recalculator overwriting a human's figure, an
unbounded unlock silently discards past manual corrections. The heal path
justifies this ("safe: simple loans have no legitimate interest edits") — but
that safety argument lives in a comment, not in the query, and the query is one
copy-paste away from a path where it isn't true.

Consolidate into `LedgerRepository.set_interest_lock(member_id, loan_ref, locked,
from_date=None)`.

---

## D5 — Loans updated by three different keys · **Inconsistency**

| Statement | Key |
|---|---|
| `update_loan_status`, `update_loan_details`, `update_loan_recalc_state`, `suspend_loan`, `resume_loan` | `WHERE id=?` |
| `loan_service.catch_up_loan` ([loan_service.py:417](../../src/services/loan_service.py#L417)) | `WHERE id=? AND individual_id=?` |
| `engine` loan-term refresh ([engine.py:755](../../src/engine.py#L755)) | `WHERE ref=? AND individual_id=?` |

The third form exists because of the "refs are unique per member only" footgun —
it is *correct* only because it also filters on `individual_id`. Earlier
versions of that query keyed on `ref` alone and updated other members' loans;
that is what the `CLAUDE.md` rule is about. Every write should go through the
primary key, and the repository should be the only place that resolves
`(member, ref) → id`.

---

## D6 — Raw SQL in the view layer, duplicating existing DB methods · **Duplication**

| View code | Existing equivalent |
|---|---|
| `views/ledger.py:2350` and `:2363` — `UPDATE savings SET date=?, amount=?, notes=? WHERE id=?` (twice in one method) | `database.update_savings_transaction` ([database.py:1478](../../src/database.py#L1478)) — the identical statement |
| `views/ledger.py:2445` — `DELETE FROM savings WHERE individual_id=?` | no DB method exists (funds have `fund_delete_all`; savings doesn't) |
| `views/ledger.py:1348` — `UPDATE ledger SET date=? WHERE id=?` | partially covered by `update_transaction` |

Three copies of one UPDATE, one of which bypasses `DatabaseManager` — so the
savings GL post-hook never fires for edits made through that path.

---

## D7 — Two "earliest transaction date" queries with different coverage · **Bug (report scope)**

* `database.get_earliest_record_date` / `_for_ids` ([database.py:893](../../src/database.py#L893)) — `MIN(date)` over `ledger ∪ savings`
* `reports.py:69` — `SELECT MIN(date) FROM (SELECT date FROM ledger UNION ALL SELECT date FROM savings)`

Both ignore `christmas_savings` and `benevolent_ledger`. A member whose only
history is Christmas or benevolent contributions (376 and 1,368 live rows) has
no "earliest date", so date-ranged reports silently start after their data.
One query, covering all four subledgers.

---

## D8 — Two case-insensitive name matches · **Inconsistency**

* `individual_name_exists` — `WHERE LOWER(name) = LOWER(?)`
* `check_import_conflicts` — `WHERE name LIKE ?` (no wildcards, relying on
  `LIKE` being case-insensitive for ASCII)

`LIKE` also treats `%` and `_` in the *supplied name* as wildcards, so a member
named `O'Brien_J` matches names the user never intended. Neither is
Unicode-correct. Use `name = ? COLLATE NOCASE` against the
`idx_individuals_name_nocase` index, in one place.

---

## D9 — Balance read two ways: stored snapshot vs. recomputed sum · **Latent bug**

`get_savings_balance` / `fund_balance` return the `balance` column of the most
recent row (`ORDER BY date DESC, id DESC LIMIT 1`) — a stored snapshot. Every
recalculation path re-derives that column by summing. When a mid-history row is
edited or deleted without a recalculation, the snapshot is stale, and
`ProvisioningService` (which nets savings off loan exposure to compute the
regulatory loan-loss provision) reads the stale value.

Either always sum (`v_member_position` does; cheap with the new index), or make
recalculation mandatory in the repository's write path.

---

## D10 — `SELECT *` with positional access · **Fragility**

`get_individuals()` returns raw tuples, and `excel_import._candidates` reads
`r[0], r[1]` — so adding a column before `name` silently corrupts the matcher.
`get_ledger` returns a DataFrame whose column set the callers probe with
`if "principal_balance" in df` (six occurrences), because they cannot be sure
which schema generation they are reading.

Repositories should return explicit column lists mapped to dataclasses.

---

## S1 — Divergent setting defaults · **Bug**

`fy_start_month` defaults to **`'November'`** in the Settings dialog
([dashboard.py:1039](../../src/views/dashboard.py#L1039)) and to **`'January'`**
in three places in [reports.py](../../src/reports.py#L34) (lines 34, 52, 129).
On a journal where the user never opened Settings, reports use a January fiscal
year while the UI claims November.

Fixed by seeding the row (`seed.sql`) *and* by moving defaults to one
`SETTING_DEFAULTS` dict rather than repeating them at each call site — there are
21 `get_setting` call sites carrying inline defaults today.

---

## Summary

| ID | Kind | Impact |
|---|---|---|
| D1 | 5 implementations, 3 semantics | Wrong balances once `Interest` rows exist |
| D2 | 2 table definitions + DDL on read paths | Schema drift; commits inside transactions |
| D3 | Identical query in 2 files | Maintenance |
| D4 | 3 edit-lock mutations, inconsistent scope | Silently discards manual corrections |
| D5 | 3 update keys for one table | The cross-member update class of bug |
| D6 | Raw SQL in views duplicating DB methods | GL hooks skipped |
| D7 | Coverage gap in 2 copies | Reports start after a member's real history |
| D8 | 2 case-insensitivity mechanisms | Wildcard injection into name matching |
| D9 | Snapshot vs. sum | Stale input to regulatory provisioning |
| D10 | `SELECT *` + positional access | Breaks on column addition |
| S1 | Divergent defaults for one setting | Wrong fiscal year in reports |
