"""Centralized configuration for LoanMaster application.

This module contains all magic numbers, default values, and business rule
constants that were previously hardcoded throughout the codebase.
"""

# =============================================================================
# LOAN DEFAULTS
# =============================================================================

# Default annual interest rate (15%)
DEFAULT_INTEREST_RATE = 0.15

# Default loan duration in months
DEFAULT_LOAN_DURATION = 12

# =============================================================================
# BUSINESS RULES
# =============================================================================

# Minimum loan amount allowed
MIN_LOAN_AMOUNT = 100

# Maximum loan duration in months
MAX_LOAN_DURATION = 60

# Minimum loan duration in months
MIN_LOAN_DURATION = 1

# =============================================================================
# DISPLAY FORMATS
# =============================================================================

# Date format for storage (ISO 8601)
DATE_FORMAT_STORAGE = "%Y-%m-%d"

# Date format for display
DATE_FORMAT_DISPLAY = "%B %d, %Y"

# Currency rounding (use ceiling for payments)
CURRENCY_ROUNDING = "ceil"

# =============================================================================
# STATEMENT GENERATION
# =============================================================================

# Default date range for statements (months back from today)
DEFAULT_STATEMENT_RANGE_MONTHS = 12

# PDF margins in mm
PDF_MARGIN_MM = 10

# =============================================================================
# GENERAL LEDGER — CHART OF ACCOUNTS
# =============================================================================
# Standard SACCO chart of accounts seeded into chart_of_accounts. Each tuple is
# (code, name, type, normal_balance). normal_balance is the side that increases
# the account: assets/expenses are 'debit'; liabilities/equity/income are
# 'credit'. Contra accounts (e.g. the loan-loss allowance) carry the opposite
# of their section. Codes follow the usual 1000/2000/... banding so reports can
# group by leading digit. Money is tracked to 2 decimal places.
GL_ACCOUNT_TYPES = ("Asset", "Liability", "Equity", "Income", "Expense")

# Well-known account codes referenced by the auto-posting engine.
GL_CASH = "1000"
GL_LOANS_RECEIVABLE = "1100"
GL_ALLOWANCE_LOAN_LOSS = "1190"
GL_INTEREST_RECEIVABLE = "1200"
GL_MEMBER_DEPOSITS = "2000"
GL_SHARE_CAPITAL = "3000"
GL_RETAINED_EARNINGS = "3100"
GL_OPENING_EQUITY = "3900"          # suspense / opening balance for migrations
GL_LOAN_INTEREST_INCOME = "4000"
GL_FEES_INCOME = "4100"
GL_BANK_INTEREST_INCOME = "4200"
GL_OTHER_INCOME = "4900"
GL_LOAN_LOSS_EXPENSE = "5600"
GL_OTHER_EXPENSE = "5900"

DEFAULT_CHART_OF_ACCOUNTS = [
    # Assets (1000s) — normal debit
    (GL_CASH,                 "Cash at Bank",              "Asset",     "debit"),
    ("1010",                  "Cash on Hand",              "Asset",     "debit"),
    (GL_LOANS_RECEIVABLE,     "Loans Receivable",          "Asset",     "debit"),
    (GL_ALLOWANCE_LOAN_LOSS,  "Allowance for Loan Losses", "Asset",     "credit"),  # contra-asset
    (GL_INTEREST_RECEIVABLE,  "Interest Receivable",       "Asset",     "debit"),
    # Liabilities (2000s) — normal credit
    (GL_MEMBER_DEPOSITS,      "Member Deposits / Savings", "Liability", "credit"),
    ("2100",                  "External Borrowings",       "Liability", "credit"),
    ("2200",                  "Accounts Payable",          "Liability", "credit"),
    # Equity (3000s) — normal credit
    (GL_SHARE_CAPITAL,        "Member Share Capital",      "Equity",    "credit"),
    (GL_RETAINED_EARNINGS,    "Retained Earnings",         "Equity",    "credit"),
    (GL_OPENING_EQUITY,       "Opening Balance Equity",    "Equity",    "credit"),
    # Income (4000s) — normal credit
    (GL_LOAN_INTEREST_INCOME, "Loan Interest Income",      "Income",    "credit"),
    (GL_FEES_INCOME,          "Fees & Fines Income",       "Income",    "credit"),
    (GL_BANK_INTEREST_INCOME, "Bank Interest Income",      "Income",    "credit"),
    (GL_OTHER_INCOME,         "Other Income",              "Income",    "credit"),
    # Expenses (5000s) — normal debit
    ("5000",                  "Staff Salaries",            "Expense",   "debit"),
    ("5100",                  "Office Rent",               "Expense",   "debit"),
    ("5200",                  "Utility Bills",             "Expense",   "debit"),
    ("5300",                  "Stationery / Supplies",     "Expense",   "debit"),
    ("5400",                  "Bank Fees / Charges",       "Expense",   "debit"),
    ("5500",                  "Marketing / PR",            "Expense",   "debit"),
    (GL_LOAN_LOSS_EXPENSE,    "Loan Loss Provision",       "Expense",   "debit"),
    (GL_OTHER_EXPENSE,        "Other Expenses",            "Expense",   "debit"),
]

# =============================================================================
# LOAN-LOSS PROVISIONING (SASRA classification)
# =============================================================================
# SACCO loan classification by days in arrears and the minimum provision rate
# applied to the outstanding balance of loans in each band. These are the
# standard SASRA (SACCO Societies Regulations) bands; edit here to match your
# institution's approved policy. Each tuple is
# (label, min_days, max_days_inclusive_or_None, provision_rate).
SASRA_PROVISION_BANDS = [
    ("Performing",  0,    30,   0.01),
    ("Watch",       31,   180,  0.05),
    ("Substandard", 181,  360,  0.25),
    ("Doubtful",    361,  540,  0.50),
    ("Loss",        541,  None, 1.00),
]

# Loans more than this many days in arrears count as non-performing (used for
# the Portfolio-at-Risk ratio).
PAR_THRESHOLD_DAYS = 30

# SASRA allows a member's (non-withdrawable) deposits to be netted off their
# loan exposure before provisioning, since the deposits are attachable. When
# True, a member's savings reduce their loan balance (allocated across their
# loans pro-rata) before the provision rate is applied.
PROVISION_NET_OF_SAVINGS = True
