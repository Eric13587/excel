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
            # Check settings for default
            default = self.db.get_setting("default_savings_increment", "2500")
            return float(default)
        
        # Return the last deposit amount as suggestion
        return float(deposits.iloc[-1]['amount'])
    
    def add_deposit(self, individual_id, amount, date_str, notes="", batch_id=None):
        """Add a savings deposit.
        
        Args:
            individual_id: ID of the individual.
            amount: Deposit amount.
            date_str: Date in YYYY-MM-DD format.
            notes: Optional transaction notes.
            batch_id: Optional batch ID.
            
        Returns:
            True if successful.
        """
        self.db.add_savings_transaction(individual_id, date_str, "Deposit", amount, notes, batch_id=batch_id)
        return True
    
    def add_withdrawal(self, individual_id, amount, date_str, notes="", batch_id=None):
        """Add a savings withdrawal.
        
        Args:
            individual_id: ID of the individual.
            amount: Withdrawal amount (positive number).
            date_str: Date in YYYY-MM-DD format.
            notes: Optional transaction notes.
            batch_id: Optional batch ID.
            
        Returns:
            True if successful.
        """
        self.db.add_savings_transaction(individual_id, date_str, "Withdrawal", amount, notes, batch_id=batch_id)
        return True

    def recalculate_user_savings(self, individual_id):
        """Recalculate running balances for a user's savings account."""
        transactions = self.db.get_savings_transactions(individual_id)
        if transactions.empty:
            return

        # Sort by date, then id (if available, usually implicit rowid)
        # Assuming get_savings_transactions returns sorted or we sort it
        # It usually returns sorted by Date descending?
        # We need Ascending for running balance.
        transactions = transactions.sort_values(by=['date', 'id'], ascending=[True, True])
        
        running_balance = 0.0
        cursor = self.db.conn.cursor()
        
        for index, row in transactions.iterrows():
            amount = float(row['amount'])
            t_type = row['transaction_type']
            t_id = row['id']
            
            if t_type == "Deposit":
                running_balance += amount
            else:
                running_balance -= amount
                
            # Update balance if different
            if abs(running_balance - float(row['balance'])) > 0.001:
                cursor.execute("UPDATE savings SET balance=? WHERE id=?", (running_balance, t_id))
                
        self.db.conn.commit()

    def catch_up_savings(self, individual_id, monthly_amount=None, batch_id=None, target_date=None):
        """Auto-increment savings from last entry up to current month (or target date).
        
        If monthly_amount is None/0, uses the amount from the last transaction.
        
        Args:
            individual_id: ID of the individual.
            monthly_amount: Optional fixed monthly deposit amount.
            batch_id: Optional batch ID.
            target_date: Optional datetime object. If None, uses current month start.
            
        Returns:
            Number of deposits added.
        """
        transactions = self.db.get_savings_transactions(individual_id)
        
        # Logic to determine start date
        if transactions.empty:
             # If completely new, can't auto-increment based on history?
             # Rules say "catch up from last entry". If no entry, no catch up.
            return 0
        
        # Get the last transaction details
        # transactions df is sorted by ID ascending (chronological usually)
        last_tx = transactions.iloc[-1] # Last row is latest
        last_date_str = str(last_tx['date']).split()[0]
        try:
            last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
        except ValueError:
            return 0
        
        # Determine amount
        if not monthly_amount or monthly_amount <= 0:
            monthly_amount = self.get_suggested_increment(individual_id)
        
        if monthly_amount <= 0:
            return 0
        
        # Calculate comparison date
        # Determine Limit Date (Exclusive end of loop)
        # If we want to catch up "upto the current month" (inclusive), we need limit_date to be NEXT month.
        if target_date:
            if isinstance(target_date, str):
                 target_date = datetime.strptime(target_date, "%Y-%m-%d")
            # If target provided, use it exactly? 
            # If user says "Target: Feb 2025", they usually mean "Include Feb 2025".
             # So we set limit to Mar 1.
            limit_date = (target_date + relativedelta(months=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            # Default: Catch up to NOW (Current Month Inclusive)
            # So if today is Feb 6, we want entries for Jan AND Feb.
            # Limit date should be Mar 1.
            limit_date = (datetime.now() + relativedelta(months=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Start from next month after last entry
        next_month = (last_date + relativedelta(months=1)).replace(day=1)
        
        count = 0
        while next_month < limit_date:
            date_str = next_month.strftime("%Y-%m-%d")
            
            # Add deposit
            self.db.add_savings_transaction(
                individual_id, 
                date_str, 
                "Deposit", 
                monthly_amount, 
                "Monthly Increment (Auto)",
                batch_id=batch_id
            )
            
            count += 1
            next_month = next_month + relativedelta(months=1)
        
        return count


    def mass_catch_up_savings(self, ind_ids_or_objects, progress_callback=None):
        """Process multiple catch-up operations in a single atomic transaction.
        
        Args:
            ind_ids_or_objects: List of ind_ids (integers) OR Qt objects with 'ind_id' property.
            progress_callback: Optional callable(index, obj) to update UI.
            
        Returns:
            (processed_count, total_transactions, batch_id)
        """
        import uuid
        batch_id = str(uuid.uuid4())
        processed_count = 0
        total_tx = 0
        errors = []
        
        try:
            with self.db.transaction():
                for i, item in enumerate(ind_ids_or_objects):
                    if hasattr(item, "property"):
                        i_id = item.property("ind_id")
                    else:
                        i_id = item
                    
                    try:
                        count = self.catch_up_savings(i_id, None, batch_id=batch_id)
                        if count > 0:
                            processed_count += 1
                            total_tx += count
                    except Exception as e:
                        errors.append((i_id, str(e)))
                        
                    if progress_callback:
                        progress_callback(i, item)
        except Exception as e:
            # Re-raise if overall transaction fails
            raise e
            
        return processed_count, total_tx, batch_id, errors

    def revert_batch_savings(self, batch_id):
        """Revert a batch of savings transactions."""
        if not batch_id: return False
        
        # 1. Identify affected individuals
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT DISTINCT individual_id FROM savings WHERE batch_id=?", (batch_id,))
        affected_ids = [row[0] for row in cursor.fetchall()]

        # 2. Delete batch
        self.db.delete_savings_batch(batch_id)
        
        # 3. Recalculate balances
        for i_id in affected_ids:
            self.recalculate_user_savings(i_id)
            
        return True
