from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import pandas as pd

@dataclass
class StatementData:
    """DTO for holding all data required for statement generation."""
    individual: Dict[str, Any]
    ledger_df: pd.DataFrame
    savings_df: pd.DataFrame
    savings_balance: float
    active_loans: List[Dict[str, Any]]

@dataclass
class StatementRow:
    date: str
    event_type: str
    debit: float
    interest: float
    credit: float
    balance: float
    gross_balance: float
    show_gross: bool
    notes: str

@dataclass
class StatementLoanSection:
    loan_ref: str
    rows: List[StatementRow]
    
@dataclass
class StatementSavingsRow:
    date: str
    type: str
    amount: float
    balance: float
    notes: str
    is_withdrawal: bool

@dataclass
class StatementPresentation:
    customer_name: str
    customer_phone: str
    period_display: str
    status_str: str
    status_color: str
    loan_sections: List[StatementLoanSection]
    savings_rows: List[StatementSavingsRow]
    total_net_outstanding: float
    total_gross_outstanding: float
    savings_balance: float

@dataclass
class StatementConfig:
    show_loans: bool = True
    show_savings: bool = True
    show_gross_balance: bool = True
    show_notes: bool = True
    custom_title: str = "ACCOUNT STATEMENT"
    custom_footer: str = ""
    date_format: str = "%B %d, %Y"
    # Default columns for PDF/Excel
    columns: List[str] = field(default_factory=lambda: ["Date", "Type", "Debit", "Interest", "Credit", "Balance", "Gross", "Notes"])
    company_logo_path: Optional[str] = None
    company_name: Optional[str] = None
    allow_html_fallback: bool = True
