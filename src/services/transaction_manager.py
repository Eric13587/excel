"""Transaction management service for LoanMaster.

This service handles generic and complex transaction operations such as
editing, deleting, undoing, and updating repayments, which often involve
cascading effects and state restoration.
"""
import json
import math
from datetime import datetime
import pandas as pd
from src.config import DEFAULT_INTEREST_RATE

from src.exceptions import TransactionError

class TransactionManager:
    """Handles generic transaction operations."""
    
    def __init__(self, db_manager, balance_recalculator):
        """Initialize TransactionManager.
        
        Args:
            db_manager: DatabaseManager instance.
            balance_recalculator: BalanceRecalculator instance for triggering updates.
        """
        self.db = db_manager
        self.balance_recalculator = balance_recalculator

    def get_ledger_df(self, individual_id):
        """Helper to get ledger dataframe."""
        return self.db.get_ledger(individual_id)

    def delete_transaction(self, individual_id, trans_id):
        """Delete a transaction and revert loan balance, handling pairs and state restoration."""
        trans = self.db.get_transaction(int(trans_id))
        if not trans:
            return

        loan_ref = trans.get('loan_id')
        if not loan_ref or loan_ref == "-":
            # Just delete if no loan attached
            self.db.delete_transaction(int(trans_id))
            return

        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            # Maybe loan deleted? Just delete trans.
            self.db.delete_transaction(int(trans_id))
            return

        event_type = trans['event_type']
        
        # --- 1. Identify Pair (Sibling) ---
        sibling_id = None
        if event_type == "Repayment" or event_type == "Interest Earned":
            # Look for neighbor
            txs = self.get_ledger_df(individual_id)
            loan_txs = txs[txs['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
            
            # Find this trans in df
            my_idx_list = loan_txs.index[loan_txs['id'] == int(trans_id)].tolist()
            if my_idx_list:
                idx = loan_txs.index.get_loc(my_idx_list[0])
                
                potential_sibling = None
                if event_type == "Repayment":
                    # Interest Earned should be BEFORE me (idx - 1)
                    if idx > 0:
                        prev = loan_txs.iloc[idx - 1]
                        if prev['event_type'] == "Interest Earned" and prev['date'] == trans['date']:
                             potential_sibling = int(prev['id'])
                elif event_type == "Interest Earned":
                    # Repayment should be AFTER me (idx + 1)
                    if idx < len(loan_txs) - 1:
                        nxt = loan_txs.iloc[idx + 1]
                        if nxt['event_type'] == "Repayment" and nxt['date'] == trans['date']:
                             potential_sibling = int(nxt['id'])
                
                if potential_sibling:
                    sibling_id = potential_sibling

        # --- 2. Check for Snapshot (State Restoration) ---
        prev_state = None
        prev_state_json = trans.get('previous_state')
        
        # If I don't have snapshot, maybe sibling does?
        if not prev_state_json and sibling_id:
             sib_trans = self.db.get_transaction(sibling_id)
             if sib_trans:
                  prev_state_json = sib_trans.get('previous_state')
        
        if prev_state_json:
             try:
                 prev_state = json.loads(prev_state_json)
             except:
                 prev_state = None

        # --- 2b. Cascade Delete Trigger (User Request) ---
        structural_events = ["Loan Top-Up", "Loan Restructure", "Loan Consolidated", "Loan Issued"]
        if event_type in structural_events:
             txs = self.get_ledger_df(individual_id)
             # Cascade delete future transactions for this loan
             future_txs = txs[(txs['loan_id'] == loan_ref) & (txs['id'] > int(trans_id))]
             
             if not future_txs.empty:
                 for _, ftx in future_txs.iterrows():
                     self.db.delete_transaction(int(ftx['id']))

        # --- 2c. Cascade Revert (Terms Reset) ---
        if event_type == "Repayment":
            self.db.unlock_future_interest(loan_ref, trans['date'])

        # --- 3. Execute Deletion ---
        self.db.delete_transaction(int(trans_id))
        
        # Collect all sibling IDs to delete
        sibling_ids = []
        if sibling_id:
            sibling_ids.append(sibling_id)
        
        if event_type == "Loan Buyoff":
            txs = self.get_ledger_df(individual_id)
            potential_ie_txs = txs[(txs['loan_id'] == loan_ref) & 
                                   (txs['date'] == trans['date']) & 
                                   (txs['event_type'] == "Interest Earned")]
            for _, ie_tx in potential_ie_txs.iterrows():
                if int(ie_tx['id']) != int(trans_id): 
                    sibling_ids.append(int(ie_tx['id']))

        found_ids = []
        for s_id in sibling_ids:
            if self.db.get_transaction(s_id): 
                self.db.delete_transaction(s_id)
                found_ids.append(s_id)

        missing_ids = [s for s in sibling_ids if s not in found_ids]
        for m_id in missing_ids:
            self.db.delete_transaction(m_id)
            
        # --- 4. State Restoration (The "Undo" logic) ---
        if prev_state:
             self.db.update_loan_details(
                 loan['id'],
                 prev_state['total_amount'],
                 prev_state['balance'], # Principal
                 prev_state['installment'],
                 prev_state['monthly_interest'],
                 prev_state['next_due_date'],
                 unearned_interest=prev_state.get('unearned_interest'),
                 interest_balance=prev_state.get('interest_balance'),
                 principal_update=prev_state.get('principal')
             )
             if 'status' in prev_state:
                 self.db.update_loan_status(
                     loan['id'], 
                     prev_state['balance'], 
                     prev_state['next_due_date'], 
                     prev_state['status'],
                     interest_balance=prev_state.get('interest_balance')
                 )
        else:
            # --- 5. Legacy Fallback (No Snapshot) ---
            deducted = trans['deducted']
            added = trans['added']
            
            if event_type == "Interest Earned":
                 curr_u = loan.get('unearned_interest', 0)
                 self.db.update_loan_details(loan['id'], 0, loan['balance'], loan['installment'], loan['monthly_interest'], loan['next_due_date'], unearned_interest=curr_u + added)
            
            elif event_type == "Loan Top-Up":
                 new_bal = loan['balance'] - added 
                 interest_removed = added * DEFAULT_INTEREST_RATE
                 curr_u = loan.get('unearned_interest', 0)
                 new_u = curr_u - interest_removed
                 
                 curr_m_int = loan.get('monthly_interest', 0)
                 dur = 12
                 if curr_m_int > 0: dur = round(curr_u / curr_m_int)
                 if dur < 1: dur = 1
                 
                 new_inst = math.ceil((loan['balance'] + new_u) / dur)
                 new_m_int = math.ceil(new_u / dur)
                 
                 self.db.update_loan_details(loan['id'], 0, loan['balance'], new_inst, new_m_int, loan['next_due_date'], unearned_interest=new_u)

        # --- 6. Replay History for Splits Integrity ---
        if loan_ref and loan_ref != "-":
            self.balance_recalculator.recalculate_loan_history(individual_id, loan_ref)
            
        # --- 7. Recalculate Running Totals ---
        self.balance_recalculator.recalculate_balances(individual_id)
        
        self.balance_recalculator.recalculate_default_deduction(individual_id)

    def undo_last_transaction(self, individual_id):
        """Undo the last transaction."""
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return False
        
        last_trans_id = df.iloc[-1]['id']
        self.delete_transaction(individual_id, last_trans_id)
        return True

    def undo_last_for_loan(self, individual_id, loan_ref):
        """Undo the last transaction for a specific loan."""
        txs = self.get_ledger_df(individual_id)
        if txs.empty:
            return False
            
        loan_txs = txs[txs['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
        if loan_txs.empty:
            return False
            
        last_tx = loan_txs.iloc[-1]
        trans_id = int(last_tx['id'])
        
        self.delete_transaction(individual_id, trans_id)
        return True

    def edit_transaction(self, individual_id, trans_id, date, added, deducted, notes, mark_edited=False):
        """Edit a transaction."""
        self.db.update_transaction(int(trans_id), date, added, deducted, notes, mark_edited=mark_edited)
        
        # Trigger Smart Replay to propagate Manual Override to future Auto Payments
        tx = self.db.get_transaction(trans_id)
        if tx and tx['loan_id']:
            self.balance_recalculator.recalculate_smart_loan_ledger(individual_id, tx['loan_id'])
        
        self.balance_recalculator.recalculate_balances(individual_id)

    def update_repayment_amount(self, individual_id, trans_id, new_amount, notes, skip_recursive_update=False):
        """Update a repayment transaction, recalculating splits (Segregated Model)."""
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return False
            
        # 1. Sort ledger to ensure correct timeline
        df['date'] = df['date'].astype(str)
        df = df.sort_values(by=['date', 'id']).reset_index(drop=True)
        
        # 2. Find target transaction index
        try:
            # trans_id might be int vs str in df? database returns int id usually.
            idx = df[df['id'] == trans_id].index[0]
        except IndexError:
            return False
        
        # 3. Determine Interest Balance BEFORE this transaction
        # If it's the first transaction, bal is 0. Else, take previous row's int_balance
        if idx == 0:
            prev_i_bal = 0.0
        else:
            prev_i_bal = float(df.iloc[idx-1]['interest_balance'])
        
        # 4. Recalculate Splits
        # Logic matches 'process_loan' step 2:
        # Pay available Interest Balance first, then Principal.
        
        interest_pay = 0.0
        if prev_i_bal > 0:
            if new_amount >= prev_i_bal:
                interest_pay = prev_i_bal
            else:
                interest_pay = new_amount
        
        principal_pay = new_amount - interest_pay
        
        # 5. Update Transaction
        trans = df.loc[idx]
        current_date = str(trans['date'])
        
        self.db.update_transaction(
            int(trans_id), 
            current_date, 
            0, # added is 0 for repayment
            new_amount, # deducted
            str(notes),
            principal_portion=principal_pay,
            interest_portion=interest_pay,
            mark_edited=True
        )
        
        self.balance_recalculator.recalculate_balances(individual_id)
        self.balance_recalculator.recalculate_default_deduction(individual_id) 
        
        # === STICKY DEDUCTION LOGIC ===
        # Always active as per user request
        if not skip_recursive_update:
            loan_ref = trans['loan_id']
            if loan_ref and loan_ref != "-":
                loan = self.db.get_loan_by_ref(individual_id, loan_ref)
                if loan and loan['status'] == 'Active':
                    # Re-Evaluate Duration and Installment
                    
                    # Ensure we have fresh balances (from recalc above)
                    loan = self.db.get_loan_by_ref(individual_id, loan_ref)
                    
                    total_future_debt = loan['balance'] + loan.get('interest_balance', 0) + loan.get('unearned_interest', 0)
                    
                    # === REFINE UNEARNED POT ===
                    # Add back Concurrent & Future Accruals (date >= current_date) to the Unearned Pot
                    
                    future_accruals_sum = 0.0
                    df = self.get_ledger_df(individual_id)
                    if not df.empty:
                        df['date_obj'] = pd.to_datetime(df['date'])
                        curr_date_obj = pd.to_datetime(current_date)
                        
                        # Find Future "Interest Earned" for THIS loan
                        future_accruals = df[
                            (df['loan_id'] == loan_ref) & 
                            (df['event_type'] == 'Interest Earned') & 
                            (df['date_obj'] >= curr_date_obj)
                        ]
                        future_accruals_sum = future_accruals['added'].sum()
                        
                    # Adjusted Unearned Pot = Current DB Unearned + Future Accruals already deducted
                    adjusted_unearned_pot = loan.get('unearned_interest', 0) + future_accruals_sum
                    
                    new_monthly_interest = loan.get('monthly_interest', 0)
                    
                    if new_amount > 0:
                        # User Request: Use Total Remaining BEFORE this payment to determine total months.
                        pre_payment_debt = total_future_debt + new_amount
                        
                        new_duration = math.ceil(pre_payment_debt / new_amount)
                        new_duration = max(1, int(new_duration))
                        
                        # Recalculate Monthly Interest (Accrual) for future
                        new_monthly_interest = 0.0
                        if adjusted_unearned_pot > 0:
                            # User Request: Round UP the monthly interest
                            new_monthly_interest = math.ceil(adjusted_unearned_pot / new_duration)
                            
                        # Update Loan
                        future_count = 0
                        if not df.empty:
                            future_count = len(df[
                                (df['loan_id'] == loan_ref) & 
                                (df['event_type'] == 'Interest Earned') & 
                                (df['date_obj'] >= curr_date_obj)
                            ])
                        
                        consumed_by_future_rows = new_monthly_interest * future_count
                        new_db_unearned = max(0.0, adjusted_unearned_pot - consumed_by_future_rows)
                        
                        self.db.update_loan_details(
                            loan['id'],
                            0,
                            loan['balance'], # Unchanged
                            new_amount, # New Installment
                            new_monthly_interest,
                            loan['next_due_date'],
                            unearned_interest=new_db_unearned
                        )
                    
                    # === INSTANT ACCRUAL UPDATE ===
                    # Update concurrent & future accruals to reflect new rate immediately.
                    try:
                        cursor = self.db.conn.cursor()
                        cursor.execute("""
                            UPDATE ledger
                            SET added = ?, is_edited = 1
                            WHERE loan_id = ? AND event_type = 'Interest Earned' AND date >= ?
                        """, (new_monthly_interest, loan_ref, current_date))
                        
                        # Also persist the New Rate in the Repayment Transaction itself
                        cursor.execute("""
                            UPDATE ledger
                            SET interest_amount = ? 
                            WHERE id = ?
                        """, (new_monthly_interest, int(trans_id)))
                        
                        self.db.conn.commit()
                        
                        # Recalculate Balances again after direct SQL update
                        self.balance_recalculator.recalculate_balances(individual_id)
                        
                        # === RECURSIVE SPLIT FIX ===
                        # Calling again with skip_recursive_update=True forces re-evaluation of splits
                        self.update_repayment_amount(individual_id, trans_id, new_amount, notes, skip_recursive_update=True)
                        
                    except Exception as e:
                        print(f"Error updating future accruals: {e}")

        return True
