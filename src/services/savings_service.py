"""Savings service for LoanMaster.

This service handles all savings-related operations including:
- Deposits
- Withdrawals
- Auto-increment catch-up
"""
from datetime import datetime
from dateutil.relativedelta import relativedelta


class SavingsService:
    """Handles savings account operations.
    
    This service manages deposit and withdrawal operations for individual
    savings accounts.
    """
    
    def __init__(self, db_manager):
        """Initialize SavingsService.
        
        Args:
            db_manager: DatabaseManager instance for data persistence.
        """
        self.db = db_manager
    
    def get_savings_balance(self, individual_id):
        """Get current savings balance for an individual.
        
        Args:
            individual_id: ID of the individual.
            
        Returns:
            Current savings balance as float.
        """
        return self.db.get_savings_balance(individual_id)
    
    def get_suggested_increment(self, individual_id):
        """Determine the suggested savings increment based on history.
        
        Args:
            individual_id: ID of the individual.
            
        Returns:
            Suggested increment amount, or 0 if no history.
        """
        transactions = self.db.get_savings_transactions(individual_id)
        if transactions.empty:
            return 0
        
        # Get the most common deposit amount
        deposits = transactions[transactions['transaction_type'] == 'Deposit']
        if deposits.empty:
            return 0
        
        # Return the last deposit amount as suggestion
        return float(deposits.iloc[-1]['amount'])
    
    def catch_up_savings(self, individual_id, monthly_amount=None):
        """Auto-increment savings from last entry up to current month.
        
        If monthly_amount is None/0, uses the amount from the last transaction.
        
        Args:
            individual_id: ID of the individual.
            monthly_amount: Optional fixed monthly deposit amount.
            
        Returns:
            Number of deposits added.
        """
        transactions = self.db.get_savings_transactions(individual_id)
        
        if transactions.empty:
            return 0
        
        # Get the last transaction details
        last_tx = transactions.iloc[-1]
        last_date_str = str(last_tx['date']).split()[0]
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
        
        # Determine amount
        if not monthly_amount or monthly_amount <= 0:
            monthly_amount = float(last_tx['amount'])
        
        if monthly_amount <= 0:
            return False
        
        # Calculate current date for comparison
        current_date = datetime.now()
        current_month_start = current_date.replace(day=1)
        
        # Start from next month after last entry
        next_month = (last_date + relativedelta(months=1)).replace(day=1)
        
        count = 0
        while next_month < current_month_start:
            date_str = next_month.strftime("%Y-%m-%d")
            
            # Add deposit
            self.db.add_savings_transaction(
                individual_id, 
                date_str, 
                "Deposit", 
                monthly_amount, 
                "Monthly Increment (Auto)"
            )
            
            count += 1
            next_month = next_month + relativedelta(months=1)
        
        return count
    
    def add_deposit(self, individual_id, amount, date_str, notes=""):
        """Add a savings deposit.
        
        Args:
            individual_id: ID of the individual.
            amount: Deposit amount.
            date_str: Date in YYYY-MM-DD format.
            notes: Optional transaction notes.
            
        Returns:
            True if successful.
        """
        self.db.add_savings_transaction(individual_id, date_str, "Deposit", amount, notes)
        return True
    
    def add_withdrawal(self, individual_id, amount, date_str, notes=""):
        """Add a savings withdrawal.
        
        Args:
            individual_id: ID of the individual.
            amount: Withdrawal amount (positive number).
            date_str: Date in YYYY-MM-DD format.
            notes: Optional transaction notes.
            
        Returns:
            True if successful.
        """
        self.db.add_savings_transaction(individual_id, date_str, "Withdrawal", amount, notes)
        return True
