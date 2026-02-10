# Import Data Feature - Improvements & Enhancements

This document outlines potential improvements and enhancements for the **Import Data** feature in LoanMaster.

---

## 1. Missing Transaction Safety / Rollback on Partial Failure

**Current Issue:** The `import_selected_data` method commits all changes at the end (`self.conn.commit()`) but if an error occurs mid-import (e.g., after importing individuals but before completing loans), the partial data is lost via `self.conn.rollback()`. However, there's no per-entity transaction handling.

**Improvement:** 
- Implement granular checkpoints or savepoints so that if loans fail to import, individuals are still retained.
- Consider using SQLite's `SAVEPOINT` mechanism.
- Add a "Partial Import" status return to inform the user exactly what was imported before failure.

---

## 2. No Progress Indicator for Large Imports

**Current Issue:** The `import_individuals` method in `dashboard.py` performs the entire import synchronously without any progress feedback. For databases with hundreds or thousands of individuals/loans, the UI will freeze.

**Improvement:**
- Add a `QProgressDialog` or progress bar showing:
  - "Importing individuals... (X of Y)"
  - "Importing loans... (X of Y)"
  - "Importing ledger entries..."
- Run the import in a `QThread` to prevent UI blocking.

---

## 3. No Duplicate Detection Beyond Name Matching

**Current Issue:** Duplicates are detected solely by matching `name` strings (case-sensitive). This is fragile:
- "John Doe" vs "john doe" would create duplicate entries.
- Different people with the same name would be incorrectly merged.

**Improvement:**
- Implement case-insensitive name matching as a baseline.
- Add optional matching by phone or email for more robust deduplication.
- Show a "Potential Duplicates" review step before importing, letting users choose merge/skip/create-new.

---

## 4. No Conflict Resolution UI for Existing Records

**Current Issue:** When an individual already exists (by name), the import silently uses the existing record's ID without informing the user or allowing them to review the merge.

**Improvement:**
- Add a "Conflict Resolution" step showing:
  - Source record details vs. existing record details
  - Options: "Merge", "Skip", "Create as New"
- Allow users to decide per-record or apply a blanket policy.

---

## 5. No Dry Run / Preview of Changes

**Current Issue:** Users can only see a list of individuals to import but have no visibility into:
- Which individuals already exist (will be matched)
- How many loans/ledger entries will be imported
- Potential data conflicts

**Improvement:**
- Add a "Preview Import" button that shows:
  - New individuals to be created
  - Existing individuals that will be matched
  - Number of loans, ledger entries, and savings records per person
- Show warnings for any data inconsistencies detected.

---

## 6. Savings Import Does Not Recalculate Running Balances

**Current Issue:** When importing savings, the method directly inserts the `balance` value from the source database. If the target database already has savings entries for a matched individual, this creates incorrect running balance sequences.

**Improvement:**
- After importing savings, call `recalculate_savings_balances(individual_id)` for each affected individual.
- Alternatively, import only amounts and transaction types, then let the system recalculate all balances.

---

## 7. Loan Reference (ref) Collision Not Handled

**Current Issue:** Loan references are imported as-is. If the target database already has a loan with the same `ref` for an individual, this could cause confusion or data integrity issues (duplicate refs).

**Improvement:**
- Check for existing loan refs before import.
- Either:
  - Append a suffix to the imported ref (e.g., `L-001` → `L-001-IMP`)
  - Prompt the user to resolve the conflict
  - Skip loans with duplicate refs and report them

---

## 8. No Validation of Source Database Schema

**Current Issue:** The import methods attempt to query expected columns but silently fail or skip data if columns are missing. For example, `default_deduction`, `unearned_interest`, `interest_balance` are all optional with fallbacks to 0.

**Improvement:**
- Perform upfront schema validation:
  - Check source database version or structure
  - Warn user about missing columns and what data will be defaulted
- Consider adding a "Database Version" setting that import can check.

---

## 9. No Import Log or Audit Trail

**Current Issue:** After import completes, users only see summary counts. There's no detailed log of:
- Which individuals were created vs. matched
- Which loans were imported and their IDs
- Any errors or warnings encountered

**Improvement:**
- Generate an import log file or display a detailed report dialog.
- Include:
  - Timestamp of import
  - Source file path
  - Per-entity status (Created/Matched/Skipped/Error)
- Optionally save to a `logs/` directory.

---

## 10. No Undo for Import Operations

**Current Issue:** Once an import is completed, there's no way to undo it. If a user accidentally imports wrong data or from the wrong database, they must manually delete records.

**Improvement:**
- Tag all imported records with a `batch_id` (similar to mass operations).
- Add an "Undo Last Import" action that:
  - Identifies records by `import_batch_id`
  - Deletes all imported individuals, loans, ledger entries, and savings
- Store import metadata in a separate table for tracking.

---

## 11. Limited File Format Support

**Current Issue:** Only SQLite `.db` files are supported for import.

**Improvement:**
- Add support for:
  - CSV import with column mapping UI
  - Excel (`.xlsx`) import using `openpyxl`
- This would make migration from other systems easier.

---

## 12. No Date Range Filter for Ledger/Savings Import

**Current Issue:** When importing loans/ledger, ALL historical transactions are imported. For large databases, this may be unnecessary (e.g., user only wants last 12 months).

**Improvement:**
- Add date range filters to the `ImportDialog`:
  - "Import transactions from: [date] to: [date]"
- Apply these filters when querying the source ledger and savings tables.

---

## 13. Savings Statistics Missing from Import Summary

**Current Issue:** The `import_selected_data` returns a stats dict with `savings` count, but the dashboard's completion message does not display savings:
```python
msg += f"Individuals: {count['individuals']}\n"
msg += f"Loans: {count['loans']}\n"
msg += f"Ledger Entries: {count['ledger']}"
# Savings count is ignored!
```

**Improvement:**
- Update the success message in `dashboard.py` to include:
  ```python
  msg += f"\nSavings Entries: {count['savings']}"
  ```

---

## Summary

| # | Improvement | Priority | Status |
|---|-------------|----------|--------|
| 1 | Transaction Safety / Rollback | High | **Completed** |
| 2 | Progress Indicator | High | **Completed** |
| 3 | Better Duplicate Detection | High | **Completed** |
| 4 | Conflict Resolution UI | Medium | **Completed** |
| 5 | Dry Run / Preview | High | **Completed** |
| 6 | Savings Balance Recalculation | Medium | **Completed** |
| 7 | Loan Ref Collision Handling | Medium | **Completed** |
| 8 | Schema Validation | Medium | **Completed** |
| 9 | Import Log / Audit Trail | Medium | **Completed** |
| 10 | Undo Import Operations | High | **Completed** |
| 11 | CSV/Excel Import Support | Low | Pending |
| 12 | Date Range Filter | Low | **Completed** |
| 13 | Savings Stats in Summary | Low | **Completed** |
