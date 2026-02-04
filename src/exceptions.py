"""Custom exceptions for LoanMaster application."""


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
