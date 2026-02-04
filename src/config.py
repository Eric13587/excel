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
