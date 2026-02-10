# Quarterly Report Feature — Improvements

Below are **12** concrete improvements / enhancements identified from an analysis of
[`src/reports.py`](file:///home/yhazadek/Desktop/excel/src/reports.py) and the
[`Dashboard.generate_quarterly_report`](file:///home/yhazadek/Desktop/excel/src/views/dashboard.py#L1051-L1147)
UI entry-point.

---

## 1. Silent Error Swallowing — Surface Failures to the User

**File:** `src/reports.py` — lines 283-287

The entire `generate_quarterly_report` method is wrapped in a bare `except Exception`
that prints to the console and returns `False`. The user sees a generic "Failed to
generate report" message box with no detail. Stack traces are invisible in a packaged
application.

**Improvement:** Propagate the exception (or at least its message) to the caller so the
UI can display a meaningful error dialog. Consider a structured result object
`(success, message)` instead of a bare `bool`.

---

## 2. Massive Single-Method Complexity (God Method)

**File:** `src/reports.py` — lines 15-287

The entire report logic — date math, data retrieval, interest calculation, balance
derivation, rounding, DataFrame construction, Excel formatting — lives in a **single
270-line method**. This makes the method extremely difficult to test, debug, or extend.

**Improvement:** Decompose into focused helper methods:
- `_compute_quarter_boundaries(start_date_str)` → date ranges
- `_collect_loan_report_row(ind_id, loan_ref, ...)` → per-loan data
- `_build_dataframe(report_data, ...)` → DataFrame assembly
- `_write_excel(df, output_path)` → formatting & export

---

## 3. No Input Validation on the Start Date

**File:** `src/reports.py` — line 27 & `dashboard.py` — line 1132

The user can pick *any* date from the calendar widget (e.g., the 15th of a month).
The code silently treats it as a quarter start, producing a misleadingly-labelled report.

**Improvement:** Validate that the chosen date is the 1st of a month and that it aligns
with a valid quarter boundary for the configured financial year. Show a warning or
auto-snap to the nearest valid quarter start.

---

## 4. Financial-Year Quarter Calculation Is Fragile & Duplicated

**Files:** `src/reports.py` lines 99-121, `dashboard.py` lines 1058-1111

The FY-quarter logic is implemented **twice** — once in the report generator and once in
the dashboard — using different algorithms.  The dashboard version contains dead code
(`q_starts` list is built but never properly consumed before being replaced by a second
loop, lines 1073-1090 vs 1097-1108).

**Improvement:** Extract a shared `QuarterHelper` utility that both the UI and the report
generator import, ensuring consistent behaviour and eliminating dead code.

---

## 5. Legacy / New-Model Balance Fallback Logic Is Brittle

**File:** `src/reports.py` — lines 151-184

The code tries `principal_balance`, then falls back to `balance` with a heuristic
`principal_ratio` estimation. The heuristic divides by `installment` (risking division issues)
and can silently produce inaccurate principal figures for loans that don't fit the
assumed model.

**Improvement:**
- Document the expected schema version and fail fast if columns are missing.
- If legacy data is detected, log a clear warning and consider a separate migration path
  rather than embedding approximation logic inside the report.

---

## 6. No Progress Indication for Large Datasets

**Files:** `dashboard.py` and `src/reports.py`

Report generation iterates over *every* individual and *every* loan synchronously on the
UI thread. With hundreds of borrowers the app freezes with no feedback.

**Improvement:** Run the generation in a `QThread` or `QRunnable` with a progress dialog
(`QProgressDialog`) showing `"Processing borrower 34 / 210…"`.

---

## 7. Hardcoded Excel Formatting — No Theme / Customisation

**File:** `src/reports.py` — lines 237-247

Colours (`#D7E4BC`, `#f0f0f0`), column widths (25, 15), and number formats (`#,##0`)
are hardcoded. Users cannot adjust currency symbols, decimal places, or branding colours.

**Improvement:** Move formatting constants into a `ReportConfig` dataclass (or pull from
user settings) so organisations can customise the look of generated reports without code
changes.

---

## 8. Rounding Uses `math.ceil` — Inconsistent with Financial Standards

**File:** `src/reports.py` — lines 194-198

All monetary values are rounded **up** using `math.ceil`. This means even ₹100.01 becomes
₹101, which inflates totals. Standard financial rounding uses banker's rounding
(`ROUND_HALF_EVEN`) or at least `round()`.

**Improvement:** Use `Decimal` with an explicit rounding mode from the `decimal` module,
or at minimum use Python's built-in `round()` with a configurable precision.

---

## 9. Report Only Supports Excel — No PDF / CSV Export

**File:** `src/reports.py` — line 230

The report is exclusively exported as `.xlsx`. Users who need a quick PDF for printing or
a CSV for further processing have no option.

**Improvement:** Add format selection in the save dialog (`Excel / PDF / CSV`) and
implement corresponding writers. For PDF, the existing `StatementGenerator` HTML→PDF
pipeline could be reused.

---

## 10. No Report Preview Before Export

**File:** `dashboard.py` — lines 1117-1147

After the user selects a date and file path the report is generated immediately with no
opportunity to preview the data.

**Improvement:** Show a read-only `QTableView` (or an HTML preview dialog) of the report
data **before** asking for a save path. This lets users verify the quarter, catch obvious
data issues, and optionally cancel.

---

## 11. Quarter Selector UX — Raw Date Picker Instead of Dropdown

**File:** `dashboard.py` — lines 1118-1123

The user is presented with a generic `QDateEdit` calendar widget. They must know which
dates are valid quarter starts — there's no guidance.

**Improvement:** Replace the date picker with a `QComboBox` listing the available quarters
in human-readable form (e.g., *"Q1 Nov 2025 – Jan 2026"*, *"Q2 Feb 2026 – Apr 2026"*).
This eliminates invalid date selection entirely.

---

## 12. No Unit Tests for the Report Generator

**Directory:** `tests/`

There are no test files covering `ReportGenerator`. Any refactoring or bug-fix risks
introducing regressions silently.

**Improvement:** Add unit tests that exercise:
- Correct monthly interest bucketing
- B/F calculation across quarter boundaries
- Legacy vs new-model balance fallback
- Empty dataset handling
- Total-row arithmetic
