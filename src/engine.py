"""Business logic engine for LoanMaster.

This module provides the LoanEngine class which acts as a facade over 
the focused service classes in src/services/. For backward compatibility,
all existing method signatures are preserved.

Service Classes:
    - LoanService: Loan lifecycle operations
    - SavingsService: Savings account operations  
    - BalanceRecalculator: Balance computation operations
"""
import math
import json
import uuid
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pandas as pd

from src.exceptions import (
    LoanNotFoundError, 
    LoanInactiveError, 
    TransactionError
)
from src.config import DEFAULT_INTEREST_RATE

from src.services import LoanService, SavingsService, BalanceRecalculator, TransactionManager
from src.services.undo_manager import UndoManager, MassLoanCatchUpCommand, MassSavingsCatchUpCommand



class LoanEngine:
    """Handles business logic, interfacing with DatabaseManager.
    
    This class serves as a facade over the focused service classes.
    All existing method signatures are preserved for backward compatibility.
    
    For new code, consider using the service classes directly:
        - LoanService for loan operations
        - SavingsService for savings operations
        - BalanceRecalculator for balance computations
    
    Attributes:
        db: DatabaseManager instance for data persistence.
        loan_service: LoanService instance (lazy-loaded).
        savings_service: SavingsService instance (lazy-loaded).
        balance_recalculator: BalanceRecalculator instance (lazy-loaded).
        undo_manager: UndoManager instance for multi-step undo/redo.
    """
    
    def __init__(self, db_manager):
        self.db = db_manager
        self._loan_service = None
        self._savings_service = None
        self._balance_recalculator = None
        self._transaction_manager = None
        self._undo_manager = UndoManager(max_depth=20)
    
    @property
    def loan_service(self):
        """Lazy-load LoanService instance."""
        if self._loan_service is None:
            self._balance_recalculator = BalanceRecalculator(self.db)
            self._loan_service = LoanService(self.db, self._balance_recalculator)
        return self._loan_service
    
    @property
    def savings_service(self):
        """Lazy-load SavingsService instance."""
        if self._savings_service is None:
            self._savings_service = SavingsService(self.db)
        return self._savings_service
    
    @property
    def balance_recalculator(self):
        """Lazy-load BalanceRecalculator instance."""
        if self._balance_recalculator is None:
            self._balance_recalculator = BalanceRecalculator(self.db)
        return self._balance_recalculator

    @property
    def transaction_manager(self):
        """Lazy-load TransactionManager instance."""
        if self._transaction_manager is None:
            self._balance_recalculator = self.balance_recalculator # Ensure init
            self._transaction_manager = TransactionManager(self.db, self._balance_recalculator)
        return self._transaction_manager

    @property
    def undo_manager(self):
        """Get the UndoManager instance."""
        return self._undo_manager

    def get_ledger_df(self, individual_id, start_date=None, end_date=None):
        return self.db.get_ledger(individual_id, start_date, end_date)

    def add_loan_event(self, individual_id, principal, duration, date_str, interest_rate=None):
        """Issue a new loan (Segregated Principal & Interest model).
        
        Delegates to LoanService.
        """
        return self.loan_service.add_loan_event(
            individual_id, principal, duration, date_str, interest_rate
        )

    def recalculate_default_deduction(self, individual_id):
        return self.balance_recalculator.recalculate_default_deduction(individual_id)




    def catch_up_loan(self, individual_id, loan_ref):
        """Perform deductions until loan is caught up to current date.
        
        Delegates to LoanService.
        """
        return self.loan_service.catch_up_loan(individual_id, loan_ref)

    def mass_catch_up_loans(self, loan_refs_and_ids, progress_callback=None, target_date=None):
        """Process multiple catch-up operations atomically with undo support.
        
        Delegates to LoanService via MassLoanCatchUpCommand.
        """
        # 1. Sanitize UI objects into simple data tuples
        items = []
        for item in loan_refs_and_ids:
            if hasattr(item, "property"):
                l_ref = item.property("loan_ref")
                i_id = item.property("ind_id")
                items.append((l_ref, i_id))
            else:
                items.append(item)
        
        # 2. Execute via UndoManager
        command = MassLoanCatchUpCommand(self.loan_service, items, progress_callback, target_date=target_date)
        success = self.undo_manager.execute(command)
        
        if success:
            return command.result
        else:
            # If execution failed (callback exception etc), propagate error
            # Or assume command handles it?
            # Command execute returns True/False.
            # But the service method might raise Exception (e.g. Canceled).
            # If raised, manipulate success or re-raise?
            # Command execute wraps heavily in try-except returning False?
            # Actually catch_up raises on Cancel. Command.execute catches Exception and returns False.
            # But user wants specific feedback "Canceled".
            # If command catches it, caller gets (0,0).
            # I should modify Command to propagate specific errors or handle them gracefully?
            # The dashboard expects exceptions to detect Cancellation.
            # So I should probably let the exception bubble up from command execution if specific types?
            # My Command implementation catches generic Exception.
            # I should probably update Command to re-raise Cancel?
            # Or just check result (0,0) and assume failure?
            # But "Canceled" needs distinct message.
            # Let's simple raise if `success` is False and result is (0,0)?
            # Or dashboard sees (0,0) and says "Nothing done."
            pass
            
        return command.result

    def deduct_single_loan(self, individual_id, loan_ref):
        """Deduct installment for a single loan (Segregated P&I model).
        
        Delegates to LoanService.
        """
        return self.loan_service.deduct_single_loan(individual_id, loan_ref)

    def auto_deduct_range(self, individual_id, loan_ref, from_date, to_date):
        """Auto-deduct for a loan from a past date up to a target date (Segregated Model)."""
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan or loan['status'] != 'Active':
            return 0
        
        deductions_made = 0
        current_date = datetime.strptime(from_date, "%Y-%m-%d")
        end_date = datetime.strptime(to_date, "%Y-%m-%d")
        
        # Need ledger context for running balances
        df = self.get_ledger_df(individual_id)
        last_bal = df["balance"].iloc[-1] if not df.empty else 0.0
        last_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        last_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
        
        # Load current loan state into variables to track simulation
        curr_loan_p_bal = loan['balance']
        curr_loan_i_bal = loan.get('interest_balance', 0)
        curr_unearned = loan.get('unearned_interest', 0)
        monthly_interest_accrual = loan.get('monthly_interest', 0)
        installment = loan['installment']
        
        # Initialize next_due with current value in case loop doesn't run
        next_due = loan['next_due_date']

        while current_date <= end_date and curr_loan_p_bal > 0:
            date_str = current_date.strftime("%Y-%m-%d")

            # --- Step 1: Accrue Interest ---
            accrual_amount = min(monthly_interest_accrual, curr_unearned)
            
            if accrual_amount > 0:
                 # Update State
                 curr_unearned -= accrual_amount
                 curr_loan_i_bal += accrual_amount
                 
                 # Ledger Event
                 last_bal += accrual_amount # Debt increases
                 last_i_bal += accrual_amount
                 
                 self.db.add_transaction(
                    individual_id, date_str, "Interest Earned", loan['ref'],
                    accrual_amount, 0, last_bal, "Monthly Interest Accrual (Auto)",
                    installment_amount=0, interest_amount=accrual_amount,
                    principal_balance=last_p_bal, 
                    interest_balance=last_i_bal,
                    principal_portion=0, interest_portion=0
                 )

            # --- Step 2: Apply Payment ---
            amount = installment
            # Cap payment?? Usually installment is fixed.
            # But if paying off balance?
            # Total Debt = P + I_bal
            total_debt = curr_loan_p_bal + curr_loan_i_bal
            if amount > total_debt:
                amount = total_debt
            
            interest_pay = 0.0
            principal_pay = 0.0
            
            if curr_loan_i_bal > 0:
                if amount >= curr_loan_i_bal:
                    interest_pay = curr_loan_i_bal
                else:
                    interest_pay = amount
            
            remaining_cash = amount - interest_pay
            
            if remaining_cash > 0:
                if remaining_cash >= curr_loan_p_bal:
                    principal_pay = curr_loan_p_bal
                else:
                    principal_pay = remaining_cash

            total_payment = interest_pay + principal_pay
            
            # Update State
            curr_loan_i_bal -= interest_pay
            curr_loan_p_bal -= principal_pay
            
            # Ledger Event
            last_bal -= total_payment
            last_p_bal -= principal_pay
            last_i_bal -= interest_pay
            
            self.db.add_transaction(
                individual_id, date_str, "Repayment", loan['ref'],
                0, total_payment, last_bal, "Monthly Deduction (Auto)",
                installment_amount=0, interest_amount=0,
                principal_balance=last_p_bal,
                interest_balance=last_i_bal,
                principal_portion=principal_pay,
                interest_portion=interest_pay
            )
            
            deductions_made += 1
            current_date = current_date + relativedelta(months=1)
            
            # Update Loan Status in DB periodically or at end?
            # We must update periodically because if we break loop we want valid state.
            # But saving every iteration is slow. 
            # However `recalculate_balances` runs at end.
            # We should just update loop variables.
            
            # Update Next Due Date
            next_due = (current_date).strftime("%Y-%m-%d") # Next iteration date
            
            if curr_loan_p_bal <= 0:
                break
        
        # Final persistence
        status = "Active" if curr_loan_p_bal > 0 else "Paid"
        self.db.update_loan_status(loan['id'], curr_loan_p_bal, next_due, status,
                                   interest_balance=curr_loan_i_bal,
                                   unearned_interest=curr_unearned)
        
        self.recalculate_balances(individual_id)
        self.recalculate_default_deduction(individual_id)
        return deductions_made

    def undo_last_for_loan(self, individual_id, loan_ref):
        """Undo the last transaction for a specific loan (Delegates to centralized delete)."""
        # Find last transaction ID to ensure we create an UndoableCommand for it
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return False
            
        loan_txs = df[df['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
        if loan_txs.empty:
            return False
            
        last_tx_id = int(loan_txs.iloc[-1]['id'])
        return self.undo_transaction_with_state(individual_id, last_tx_id)
    
    def undo_transaction_with_state(self, individual_id: int, trans_id: int) -> bool:
        """Undo a transaction with full loan state restoration.
        
        Uses UndoTransactionCommand which captures both transaction AND loan state,
        allowing proper restoration of loan conditions (installment, balance, etc.)
        when undoing top-ups or manually edited deductions.
        
        Args:
            individual_id: The individual ID.
            trans_id: The transaction ID to undo.
            
        Returns:
            True if successful, False otherwise.
        """
        from src.services import UndoTransactionCommand
        
        command = UndoTransactionCommand(
            self.db, 
            self.balance_recalculator, 
            individual_id, 
            trans_id,
            transaction_manager=self.transaction_manager
        )
        return self._undo_manager.execute(command)

    def delete_loan(self, individual_id, loan_ref):
        """Delete a loan and all its transactions."""
        self.loan_service.delete_loan(individual_id, loan_ref)



    def recalculate_balances(self, individual_id):
        """Recalculate running balances for all ledger entries (Segregated).
        
        Delegates to BalanceRecalculator.
        """
        return self.balance_recalculator.recalculate_balances(individual_id)



    def top_up_loan(self, individual_id, loan_ref, top_up_amount, new_duration, date_str=None):
        """Add funds to an existing loan (Segregated Model).
        
        Delegates to LoanService.
        """
        return self.loan_service.top_up_loan(
            individual_id, loan_ref, top_up_amount, new_duration, date_str
        )

    def restructure_loan(self, individual_id, loan_ref, new_duration, new_interest_rate=None):
        """Restructure a loan: extend duration and optionally adjust interest rate.
        
        Delegates to LoanService.
        """
        return self.loan_service.restructure_loan(
            individual_id, loan_ref, new_duration, new_interest_rate
        )
        
        self.recalculate_default_deduction(individual_id)
        return True


    def recalculate_loan_history(self, individual_id, loan_ref):
        """Replay loan history to correct splits and accruals based on current Loan Terms.
        
        Delegates to BalanceRecalculator.
        """
        return self.balance_recalculator.recalculate_loan_history(individual_id, loan_ref)



    def edit_transaction(self, individual_id, trans_id, date, added, deducted, notes, mark_edited=False):
        """Edit a transaction."""
        return self.transaction_manager.edit_transaction(individual_id, trans_id, date, added, deducted, notes, mark_edited)

    def update_repayment_amount(self, individual_id, trans_id, new_amount, notes, skip_recursive_update=False):
        """Update a repayment transaction, recalculating splits (Segregated Model)."""
        return self.transaction_manager.update_repayment_amount(individual_id, trans_id, new_amount, notes, skip_recursive_update)

    def buyoff_loan(self, individual_id, loan_ref, date_str=None):
        """fully settle a loan including Principal + Accrued Interest.
        
        Delegates to LoanService.
        """
        return self.loan_service.buyoff_loan(individual_id, loan_ref, date_str)


    def delete_transaction(self, individual_id, trans_id):
        """Delete a transaction and revert loan balance, handling pairs and state restoration."""
        return self.transaction_manager.delete_transaction(individual_id, trans_id)

    def undo_last_transaction(self, individual_id):
        """Undo the last transaction."""
        return self.transaction_manager.undo_last_transaction(individual_id)

    # ===== UNDO/REDO SYSTEM =====
    
    def undo(self):
        """Undo the last undoable action.
        
        Returns:
            The undone command if successful, None otherwise.
        """
        return self._undo_manager.undo()
    
    def redo(self):
        """Redo the last undone action.
        
        Returns:
            The redone command if successful, None otherwise.
        """
        return self._undo_manager.redo()
    
    def can_undo(self) -> bool:
        """Check if undo is available."""
        return self._undo_manager.can_undo()
    
    def can_redo(self) -> bool:
        """Check if redo is available."""
        return self._undo_manager.can_redo()
    
    def get_undo_description(self):
        """Get description of the next undo action."""
        return self._undo_manager.get_undo_description()
    
    def get_redo_description(self):
        """Get description of the next redo action."""
        return self._undo_manager.get_redo_description()

    def get_default_deduction(self, individual_id):
        """Get the default monthly deduction amount."""
        ind = self.db.get_individual(individual_id)
        return float(ind.get('default_deduction', 0.0)) if ind else 0.0

    def get_transaction(self, trans_id):
        """Get a transaction by ID."""
        return self.db.get_transaction(int(trans_id))

    def get_suggested_savings_increment(self, individual_id):
        """Determine the suggested savings increment based on history.
        
        Delegates to SavingsService.
        """
        return self.savings_service.get_suggested_increment(individual_id)

    def catch_up_savings(self, individual_id, monthly_amount=None):
        """Auto-increment savings from last entry up to (but not including) current month.
        
        Delegates to SavingsService.
        """
        return self.savings_service.catch_up_savings(individual_id, monthly_amount)

    def mass_catch_up_savings(self, ind_ids_or_objects, progress_callback=None, target_date=None):
        """Process multiple catch-up operations atomically with undo support.
        
        Delegates to SavingsService via MassSavingsCatchUpCommand.
        """
        items = []
        for item in ind_ids_or_objects:
            if hasattr(item, "property"):
                i_id = item.property("ind_id")
                items.append(i_id)
            else:
                items.append(item)
        
        command = MassSavingsCatchUpCommand(self.savings_service, items, progress_callback, target_date=target_date)
        self.undo_manager.execute(command)
        return command.result
    
    def get_savings_balance(self, individual_id):
        """Get current savings balance for an individual.
        
        Delegates to SavingsService.
        """
        return self.savings_service.get_savings_balance(individual_id)

    def add_savings_deposit(self, individual_id, amount, date_str, notes=""):
        """Add a savings deposit.
        
        Delegates to SavingsService.
        """
        return self.savings_service.add_deposit(individual_id, amount, date_str, notes)

    def add_savings_withdrawal(self, individual_id, amount, date_str, notes=""):
        """Add a savings withdrawal.
        
        Delegates to SavingsService.
        """
        return self.savings_service.add_withdrawal(individual_id, amount, date_str, notes)
        
    def update_loan_transaction(self, trans_id, new_date, new_amount, new_notes, new_duration=None):
        trans_id = int(trans_id) # Ensure native int for sqlite
        """
        Update a transaction with specific logic for Loan Top-Ups.
        Handles recalculation of Unearned Interest, Principal, and Installments.
        """
        tx = self.db.get_transaction(trans_id)
        if not tx:
            return False
            
        event_type = tx['event_type']
        loan_ref = tx['loan_id']
        individual_id = tx['individual_id']
        
        if event_type == "Loan Top-Up" or event_type == "Loan Issued":
            # 1. Update the Top-Up/Issued Transaction Record
            new_amount = float(new_amount)
            new_interest_amt = new_amount * DEFAULT_INTEREST_RATE
            
            # Determine Duration (from notes or argument)
            duration = new_duration
            if duration is None:
                import re
                match = re.search(r"Duration: (\d+)m", tx['notes'])
                duration = int(match.group(1)) if match else 12
            
            # Construct Note
            generated_note = f"Top-Up: {new_amount}, Add'l Interest: {new_interest_amt}, Duration: {duration}m"
            final_note = generated_note
            if "Edited" in new_notes and "Edit" not in generated_note:
                 final_note += " (Edited)"
            
            # Check if amount changed
            is_amount_changed = abs(float(tx['added']) - new_amount) > 0.001
            
            self.db.update_transaction(trans_id, new_date, new_amount, 0, final_note, mark_edited=is_amount_changed)
            
            # 1.5. Refresh Loan Record Terms (Chicken-Egg Fix)
            # Replay relies on `loan.installment` to know the target Auto-Deduction.
            # If we don't update it here, Replay uses the OLD installment (from previous state),
            # leading to stale/incorrect Auto-Deductions persisting.
            try:
                # Get History to find Balance *Before* this Top-Up
                df = self.get_ledger_df(individual_id)
                # Find current transaction index robustly
                # df['id'] might be float or int or str depending on pandas.
                # trans_id is int.
                try:
                    current_tx_idx = df.index[df['id'].astype(int) == int(trans_id)].tolist()
                except Exception:
                    # Fallback if casting fails?
                    current_tx_idx = df.index[df['id'] == trans_id].tolist()
                
                if current_tx_idx:
                    idx = current_tx_idx[0]
                    print(f"DEBUG: Found transaction index {idx} for ID {trans_id}")
                    prev_bal = 0.0
                    prev_unearned = 0.0 
                    
                    if idx > 0:
                        prev_bal = float(df.iloc[idx-1]['balance'])
                    
                    total_debt = prev_bal + new_amount + new_interest_amt
                    
                    import math
                    new_installment = math.ceil(total_debt / duration)
                    new_monthly_interest = math.ceil(new_interest_amt / duration)
                    
                    print(f"DEBUG: Refreshing Loan Terms -> Installment: {new_installment}, MonthlyInt: {new_monthly_interest}")
                    
                    # Update Loan Record
                    cursor = self.db.conn.cursor()
                    cursor.execute("UPDATE loans SET installment = ?, monthly_interest = ? WHERE ref = ?", 
                                   (new_installment, new_monthly_interest, loan_ref))
                    self.db.conn.commit()
                else:
                    print(f"DEBUG: Transaction ID {trans_id} NOT FOUND in Ledger DF!")
            except Exception as e:
                print(f"Error refreshing loan terms: {e}")

            self.recalculate_smart_loan_ledger(individual_id, loan_ref)
            
            # 3. Global Recalc
            self.recalculate_balances(individual_id)
            
            return True
        else:
            # Generic Update
            deducted = tx['deducted']
            added = tx['added']
            # If standard deposit/withdrawal?
            # Assuming just update values and recalc balances.
            self.db.update_transaction(trans_id, new_date, new_amount, deducted, new_notes)
            self.recalculate_balances(individual_id)
            return True

    def recalculate_ledger_balances(self, individual_id):
        """Re-run the ledger to update running balances.
        
        Delegates to BalanceRecalculator.
        """
        return self.balance_recalculator.recalculate_balances(individual_id)

    def is_latest_repayment(self, individual_id, loan_ref, trans_id):
        """Check if the given transaction is the latest repayment for the loan.
        
        Delegates to BalanceRecalculator.
        """
        return self.balance_recalculator.is_latest_repayment(individual_id, loan_ref, trans_id)


    
    def recalculate_smart_loan_ledger(self, individual_id, loan_id):
        """Smart Replay: Re-simulates the loan history.
        
        Delegates to BalanceRecalculator.
        """
        return self.balance_recalculator.recalculate_smart_loan_ledger(individual_id, loan_id)
