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
