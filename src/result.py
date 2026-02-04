"""Result pattern for consistent return types in LoanMaster.

This module provides a Result class for standardizing method returns,
replacing inconsistent returns of False, 0, None, etc.
"""
from dataclasses import dataclass
from typing import Any, Optional, TypeVar, Generic

T = TypeVar('T')


@dataclass
class Result(Generic[T]):
    """Represents the outcome of an operation.
    
    Provides a consistent way to return success/failure information
    along with a value or error details.
    
    Attributes:
        success: Whether the operation succeeded.
        value: The return value on success, None on failure.
        error: Error message on failure, None on success.
        error_type: Type/category of error (e.g., "NOT_FOUND", "VALIDATION").
        
    Usage:
        # Success case
        return Result.ok(monthly_deduction)
        
        # Failure case
        return Result.fail("Loan not found", "NOT_FOUND")
        
        # Checking result
        result = engine.some_operation()
        if result.success:
            print(f"Value: {result.value}")
        else:
            print(f"Error: {result.error}")
    """
    success: bool
    value: Optional[T] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    
    @classmethod
    def ok(cls, value: T = None) -> 'Result[T]':
        """Create a successful result.
        
        Args:
            value: The return value.
            
        Returns:
            A Result with success=True and the given value.
        """
        return cls(success=True, value=value)
    
    @classmethod
    def fail(cls, error: str, error_type: str = None) -> 'Result[T]':
        """Create a failure result.
        
        Args:
            error: Error message describing what went wrong.
            error_type: Optional error category for programmatic handling.
            
        Returns:
            A Result with success=False and error details.
        """
        return cls(success=False, error=error, error_type=error_type)
    
    def __bool__(self) -> bool:
        """Allow using Result in boolean context.
        
        Returns:
            True if the operation succeeded, False otherwise.
        """
        return self.success
    
    def unwrap(self) -> T:
        """Get the value, raising an exception if the operation failed.
        
        Returns:
            The result value.
            
        Raises:
            ValueError: If the operation failed.
        """
        if not self.success:
            raise ValueError(f"Result unwrap failed: {self.error}")
        return self.value
    
    def unwrap_or(self, default: T) -> T:
        """Get the value or a default if the operation failed.
        
        Args:
            default: Value to return if operation failed.
            
        Returns:
            The result value or the default.
        """
        return self.value if self.success else default


# Common error types for consistency
class ErrorType:
    """Standard error type constants."""
    NOT_FOUND = "NOT_FOUND"
    INACTIVE = "INACTIVE"
    VALIDATION = "VALIDATION"
    DATABASE = "DATABASE"
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    PERMISSION = "PERMISSION"
