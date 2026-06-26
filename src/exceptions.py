"""Custom exceptions for LoanMaster application."""
import calendar


class LoanMasterError(Exception):
    """Base exception for all LoanMaster errors."""
    
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
    
    def __str__(self):
        if self.details:
            return f"{self.message} - {self.details}"
        return self.message


class DatabaseError(LoanMasterError):
    """Raised when a database operation fails."""
    pass


class TransactionError(DatabaseError):
    """Raised when a database transaction fails to complete."""
    pass


class LoanNotFoundError(LoanMasterError):
    """Raised when a loan cannot be found."""
    
    def __init__(self, loan_ref: str = None, individual_id: int = None):
        details = {}
        if loan_ref:
            details['loan_ref'] = loan_ref
        if individual_id:
            details['individual_id'] = individual_id
        
        message = f"Loan not found"
        if loan_ref:
            message = f"Loan '{loan_ref}' not found"
        
        super().__init__(message, details)


class InsufficientBalanceError(LoanMasterError):
    """Raised when an operation cannot be completed due to insufficient balance."""
    
    def __init__(self, required: float, available: float, loan_ref: str = None):
        details = {
            'required': required,
            'available': available
        }
        if loan_ref:
            details['loan_ref'] = loan_ref
        
        message = f"Insufficient balance: required {required}, available {available}"
        super().__init__(message, details)


class IndividualNotFoundError(LoanMasterError):
    """Raised when an individual cannot be found."""
    
    def __init__(self, individual_id: int = None, name: str = None):
        details = {}
        if individual_id:
            details['individual_id'] = individual_id
        if name:
            details['name'] = name
        
        message = "Individual not found"
        if name:
            message = f"Individual '{name}' not found"
        elif individual_id:
            message = f"Individual with ID {individual_id} not found"
        
        super().__init__(message, details)


class LoanInactiveError(LoanMasterError):
    """Raised when an operation requires an active loan but the loan is inactive."""
    
    def __init__(self, loan_ref: str, status: str):
        details = {
            'loan_ref': loan_ref,
            'status': status
        }
        message = f"Loan '{loan_ref}' is not active (status: {status})"
        super().__init__(message, details)


class LoanSuspendedError(LoanMasterError):
    """Raised when a deduction is attempted on a suspended loan."""
    
    def __init__(self, loan_ref: str, suspend_until: str = None):
        details = {
            'loan_ref': loan_ref,
            'suspend_until': suspend_until
        }
        if suspend_until:
            message = f"Loan '{loan_ref}' is suspended until {suspend_until}"
        else:
            message = f"Loan '{loan_ref}' is suspended indefinitely"
        super().__init__(message, details)


class UnbalancedJournalError(LoanMasterError):
    """Raised when a journal entry's debits do not equal its credits."""

    def __init__(self, total_debit: float, total_credit: float):
        details = {
            'total_debit': round(total_debit, 2),
            'total_credit': round(total_credit, 2),
            'difference': round(total_debit - total_credit, 2),
        }
        message = (
            f"Journal does not balance: debits {total_debit:,.2f} "
            f"!= credits {total_credit:,.2f}"
        )
        super().__init__(message, details)


class UnknownAccountError(LoanMasterError):
    """Raised when a journal line references an account not in the chart."""

    def __init__(self, account_code: str):
        super().__init__(
            f"Account '{account_code}' is not in the chart of accounts",
            {'account_code': account_code},
        )


class ChristmasLockedError(LoanMasterError):
    """Raised when a Christmas-fund withdrawal is attempted outside the unlock month."""

    def __init__(self, unlock_month: int):
        month_name = calendar.month_name[unlock_month] if 1 <= unlock_month <= 12 else str(unlock_month)
        super().__init__(
            f"Christmas withdrawals are locked until {month_name}",
            {'unlock_month': unlock_month},
        )
