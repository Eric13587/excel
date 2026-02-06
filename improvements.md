# Mass Operations Improvements

An analysis of the mass deduction and mass savings increment functionality in the LoanMaster application.

---

## Overview

The mass operations are implemented in two main dialogs:
- `open_mass_deduction_dialog()` - Mass loan catch-up (lines 827-935 in dashboard.py)
- `open_mass_savings_dialog()` - Mass savings increment (lines 1035-1139 in dashboard.py)

These delegate to:
- `LoanService.catch_up_loan()` - Individual loan catch-up
- `SavingsService.catch_up_savings()` - Individual savings catch-up

---

## 1. No Progress Indicator During Processing

**Current Issue**: Both mass operations process loans/savings in a synchronous loop without any progress feedback. For large numbers of records, the UI freezes completely.

**Location**: `dashboard.py` lines 919-926 (loans) and 1126-1132 (savings)

**Code**:
```python
for cb in selected:
     l_ref = cb.property("loan_ref")
     i_id = cb.property("ind_id")
     count = engine.catch_up_loan(i_id, l_ref)
     # No progress update!
```

**Improvement**: Add a `QProgressDialog` similar to `batch_print_selected()`. Include `QApplication.processEvents()` calls to keep the UI responsive.

---

## 2. No Transaction Safety (Batch Atomicity)

**Current Issue**: Mass operations process each loan/individual independently. If the operation fails midway (e.g., power loss, crash), some records are updated while others are not, leaving the database in an inconsistent state.

**Location**: `loan_service.py` `catch_up_loan()` and `savings_service.py` `catch_up_savings()`

**Improvement**: Wrap the entire mass operation in a single database transaction with proper commit/rollback:

```python
def mass_catch_up_loans(self, loan_refs_and_ids):
    try:
        self.db.begin_transaction()
        for loan_ref, ind_id in loan_refs_and_ids:
            self.catch_up_loan(ind_id, loan_ref)
        self.db.commit_transaction()
    except Exception as e:
        self.db.rollback_transaction()
        raise
```

---

## 3. No Undo Capability for Mass Operations

**Current Issue**: The warning dialog says "This cannot be easily undone en masse" - there's no undo functionality at all for mass operations.

**Location**: `dashboard.py` line 909

**Improvement**: 
- Create a `MassOperationCommand` that implements the undo pattern
- Store the list of affected loans/individuals before execution
- For undo, either:
  - Store snapshots of each affected record
  - Or provide a "reverse" operation (mass delete of auto-generated transactions)

---

## 4. No Error Handling / Partial Failure Reporting

**Current Issue**: If `catch_up_loan()` or `catch_up_savings()` raises an exception for one record, the loop silently continues or crashes. No detailed error report is shown.

**Location**: The for-loops don't have try-except blocks around individual operations.

**Improvement**:
```python
errors = []
for cb in selected:
    try:
        count = engine.catch_up_loan(i_id, l_ref)
    except Exception as e:
        errors.append((l_ref, str(e)))
        
if errors:
    show_error_report_dialog(errors)
```

---

## 5. Redundant Database Queries in Mass Savings Dialog

**Current Issue**: The mass savings dialog fetches `get_suggested_savings_increment()` for every individual during dialog setup, even for individuals that won't be selected.

**Location**: `dashboard.py` lines 1072-1086

**Code**:
```python
for ind in individuals:
    bal = self.db.get_savings_balance(ind[0])
    auto_amt = engine.get_suggested_savings_increment(ind[0])  # Extra query per user!
```

**Improvement**: 
- Lazy-load the suggested amount only when the checkbox is hovered or checked
- Or batch-fetch all amounts in a single optimized query
- Consider caching with `functools.lru_cache` if the data doesn't change during dialog lifetime

---

## 6. No Date Range Selection for Mass Deduction

**Current Issue**: Mass deduction always catches up to "today". There's no option to catch up to a specific past date (e.g., end of last quarter for reporting purposes).

**Location**: `open_mass_deduction_dialog()` - no date inputs

**Improvement**: Add date range inputs similar to the batch print dialog:
```python
# Add to dialog
from_date_edit = QDateEdit()
to_date_edit = QDateEdit()  # Default: today
# Pass to engine
engine.catch_up_loan_to_date(i_id, l_ref, target_date)
```

---

## 7. No Preview/Dry-Run Mode

**Current Issue**: Users cannot see what transactions will be created before committing. This is risky for financial data.

**Improvement**: Add a "Preview" button that shows:
- Number of transactions that will be created per loan
- Total deduction amounts
- Projected new balances

```python
def preview_mass_catch_up(self, loan_refs):
    results = []
    for loan_ref, ind_id in loan_refs:
        count = self._simulate_catch_up(ind_id, loan_ref)
        results.append({'loan': loan_ref, 'tx_count': count})
    return results
```

---

## 8. Inefficient One-by-One Processing in catch_up_loan

**Current Issue**: `catch_up_loan()` processes deductions one-by-one in a while loop, each time:
1. Fetching the loan from DB
2. Calling `deduct_single_loan()`
3. Re-fetching the loan again

**Location**: `loan_service.py` lines 137-147

**Code**:
```python
while loan['next_due_date'] <= current_date_str:
    self.deduct_single_loan(individual_id, loan_ref)  # Full roundtrip!
    count += 1
    loan = self.db.get_loan_by_ref(individual_id, loan_ref)  # Another query!
```

**Improvement**: Use batch processing similar to `auto_deduct_range()` in engine.py which processes in-memory and only persists at the end:
```python
def catch_up_loan_batch(self, individual_id, loan_ref):
    loan = self.db.get_loan_by_ref(individual_id, loan_ref)
    transactions_to_create = []
    
    # Simulate all deductions in memory
    while loan_state['next_due_date'] <= current_date_str:
        tx = self._simulate_deduction(loan_state)
        transactions_to_create.append(tx)
        loan_state = self._apply_deduction(loan_state, tx)
    
    # Batch insert all transactions
    self.db.bulk_insert_transactions(transactions_to_create)
    # Single loan update
    self.db.update_loan_status(...)
```

---

## 9. No Filtering/Search in Mass Dialogs

**Current Issue**: Mass deduction dialog shows all active loans in a flat list. For users with many loans, finding specific ones is difficult.

**Location**: `open_mass_deduction_dialog()` - no filter input (unlike `batch_print_selected()` which has one)

**Improvement**: Add filter input like in other dialogs:
```python
filter_input = QLineEdit()
filter_input.setPlaceholderText("Filter by name or loan ref...")
filter_input.textChanged.connect(filter_checkboxes)
```

---

## 10. Hardcoded UI Text and Typos

**Current Issue**: 
- Typo: "Deafult: 2500" should be "Default: 2500"
- Hardcoded default amount (2500) should come from configuration

**Location**: `dashboard.py` line 1045

**Code**:
```python
layout.addWidget(QLabel("<i>Amt will be auto-detected from each user's last transaction (Deafult: 2500).</i>"))
```

**Improvement**:
```python
default_amount = self.db.get_setting("default_savings_increment", "2500")
layout.addWidget(QLabel(f"<i>Amt will be auto-detected from each user's last transaction (Default: {default_amount}).</i>"))
```

---

## 11. BONUS: Missing Catch-Up To Specific Date in SavingsService

**Current Issue**: `catch_up_savings()` always catches up to `current_month_start`. There's no parameter to catch up to a specific target date.

**Location**: `savings_service.py` lines 89-96

**Improvement**: Add an optional `target_date` parameter:
```python
def catch_up_savings(self, individual_id, monthly_amount=None, target_date=None):
    if target_date is None:
        target_date = datetime.now().replace(day=1)
    # Use target_date instead of current_month_start
```

---

## 12. BONUS: Engine Creates New Instance Each Time

**Current Issue**: Both mass dialogs create a new `LoanEngine` instance inside the dialog method:

**Location**: `dashboard.py` lines 899-900, 1069-1070

**Code**:
```python
from ..engine import LoanEngine
engine = LoanEngine(self.db)
```

**Improvement**: The Dashboard should have a single shared engine instance to:
- Avoid redundant initialization
- Enable shared undo stack across operations
- Improve memory efficiency

```python
# In __init__
self._engine = LoanEngine(self.db)

# In dialogs
engine = self._engine
```

---

## Summary Table

| # | Issue | Severity | Effort |
|---|-------|----------|--------|
| 1 | No progress indicator | High | Low |
| 2 | No batch transaction safety | Critical | Medium |
| 3 | No undo capability | High | High |
| 4 | No error handling/reporting | High | Low |
| 5 | Redundant DB queries | Medium | Medium |
| 6 | No date range selection | Medium | Low |
| 7 | No preview/dry-run | Medium | Medium |
| 8 | Inefficient one-by-one processing | Medium | High |
| 9 | No filtering in dialogs | Low | Low |
| 10 | Hardcoded text/typos | Low | Low |
| 11 | No target date for savings | Low | Low |
| 12 | Engine instance per dialog | Low | Low |
