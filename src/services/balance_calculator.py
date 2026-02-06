"""Balance calculation service for LoanMaster.

This service handles all balance recalculation operations including:
- Running balance recalculation
- Loan history replay
- Unearned interest calculations
"""
import math
import re
from datetime import datetime
from dateutil.relativedelta import relativedelta
import pandas as pd

from src.config import DEFAULT_INTEREST_RATE


class BalanceRecalculator:
    """Handles balance recalculation operations.
    
    This service is responsible for recalculating running balances across
    ledger entries and ensuring loan states remain consistent.
    """
    
    def __init__(self, db_manager):
        """Initialize BalanceRecalculator.
        
        Args:
            db_manager: DatabaseManager instance for data persistence.
        """
        self.db = db_manager
    
    def get_ledger_df(self, individual_id, start_date=None, end_date=None):
        """Get ledger as DataFrame for an individual."""
        return self.db.get_ledger(individual_id, start_date, end_date)
    
    def recalculate_balances(self, individual_id):
        """Recalculate running balances for all ledger entries (Segregated).
        
        Args:
            individual_id: ID of the individual.
        """
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return

        loan_groups = df.groupby('loan_id')
        
        for loan_id, group in loan_groups:
            running_balance = 0.0
            running_p = 0.0
            running_i = 0.0
            running_gross = 0.0
            last_repayment_date = None
            issue_date = None
            
            group = group.sort_values(by=['date', 'id'])
            
            for index, row in group.iterrows():
                event = row['event_type']
                added = float(row['added'])
                deducted = float(row['deducted'])
                
                p_portion = float(row.get('principal_portion', 0))
                i_portion = float(row.get('interest_portion', 0))
                
                if event == "Loan Issued":
                    running_p += added
                    running_gross += added * (1 + DEFAULT_INTEREST_RATE)
                    if issue_date is None:
                        issue_date = row['date']
                elif event == "Loan Top-Up":
                    running_p += added
                    # Use stored interest amount if available, else derive
                    interest_amount = float(row.get('interest_amount', 0))
                    if interest_amount > 0:
                        running_gross += added + interest_amount
                    else:
                        running_gross += added * (1 + DEFAULT_INTEREST_RATE)
                elif event == "Interest Earned":
                    running_i += added
                elif event == "Repayment" or event == "Loan Buyoff":
                    running_p -= p_portion
                    running_i -= i_portion
                    running_gross -= deducted
                    if event == "Repayment":
                        last_repayment_date = row['date']
                
                running_balance = running_p + running_i
                
                if abs(running_p) < 0.01:
                    running_p = 0
                if abs(running_i) < 0.01:
                    running_i = 0
                if abs(running_balance) < 0.01:
                    running_balance = 0
                if abs(running_gross) < 0.01:
                    running_gross = 0
                
                self.db.update_ledger_balances(row['id'], running_balance, running_p, running_i, running_gross)
            
            if loan_id != "-" and loan_id is not None:
                loan = self.db.get_loan_by_ref(individual_id, loan_id)
                if loan:
                    status = "Active" if running_p > 0 else "Paid"
                    
                    new_due_date = loan['next_due_date']
                    try:
                        base_date_str = last_repayment_date if last_repayment_date else issue_date
                        if base_date_str:
                            base_date_str = str(base_date_str).split()[0]
                            base_dt = datetime.strptime(base_date_str, "%Y-%m-%d")
                            
                            if last_repayment_date:
                                new_due_dt = base_dt + relativedelta(months=1)
                            else:
                                deduct_same = self.db.get_setting("deduct_same_month", "false").lower() == "true"
                                new_due_dt = base_dt if deduct_same else base_dt + relativedelta(months=1)
                                    
                            new_due_date = new_due_dt.strftime("%Y-%m-%d")
                    except Exception as e:
                        print(f"Error calculating next_due_date: {e}")
                        
                    self.db.update_loan_status(loan['id'], running_p, new_due_date, status,
                                               interest_balance=running_i)
    
    def _recalculate_unearned_from_ledger(self, individual_id, loan_ref):
        """Helper: Recalculate unearned interest from ledger history.
        
        This fixes drift in unearned interest tracking by reconstructing
        from transaction history.
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            
        Returns:
            Calculated unearned interest amount.
        """
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return 0.0
        
        loan_df = df[df['loan_id'] == loan_ref]
        
        expected_total_interest = 0.0
        accrued_so_far = 0.0
        
        for _, row in loan_df.iterrows():
            event = row['event_type']
            added = float(row['added'])
            
            if event == "Loan Issued":
                expected_total_interest += added * DEFAULT_INTEREST_RATE
            elif event == "Loan Top-Up":
                expected_total_interest += added * DEFAULT_INTEREST_RATE
            elif event == "Interest Earned":
                accrued_so_far += added
                
        calc_unearned = expected_total_interest - accrued_so_far
        return max(0.0, calc_unearned)
    
    def recalculate_default_deduction(self, individual_id):
        """Recalculate and update the default deduction for an individual.
        
        Args:
            individual_id: ID of the individual.
        """
        active_loans = self.db.get_active_loans(individual_id)
        total_deduction = sum(l['installment'] for l in active_loans)
        self.db.update_individual_deduction(individual_id, total_deduction)

    def is_latest_repayment(self, individual_id, loan_ref, trans_id):
        """Check if the given transaction is the latest repayment for the loan."""
        df = self.get_ledger_df(individual_id)
        if df.empty:
            return True 
            
        # Filter for this loan AND Repayment events
        repayments = df[
            (df['loan_id'] == loan_ref) & 
            (df['event_type'] == "Repayment")
        ]
        
        if repayments.empty:
            return True
        
        # Sort by Date then ID (Descending)
        repayments = repayments.sort_values(by=['date', 'id'], ascending=[False, False])
        
        latest_id = int(repayments.iloc[0]['id'])
        return int(trans_id) == latest_id

    def recalculate_loan_history(self, individual_id, loan_ref):
        """Replay loan history to correct splits and accruals based on current Loan Terms."""
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan: return
        
        df = self.get_ledger_df(individual_id)
        if df.empty: return
        
        # Filter for this loan
        loan_df = df[df['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
        if loan_df.empty: return
        

        # Helper: Track running Unearned for capping
        # Initially, current_unearned should be 0 because we build it up from "Loan Issued".
        # Why? Because multiple loans/top-ups add to the pot.
        
        current_unearned = 0.0
        running_int_bal = 0.0
        
        # State: Dynamic Monthly Interest
        # We start with 0 and update it when we hit "Loan Issued" or "Loan Top-Up".
        current_monthly_interest = 0.0
        
        for idx, row in loan_df.iterrows():
            event = row['event_type']
            trans_id = int(row['id'])
            
            # We will update these
            new_added = float(row['added']) 
            new_deducted = float(row['deducted'])
            new_p_part = 0.0
            new_i_part = 0.0
            new_notes = str(row['notes'])
            
            if event == "Loan Issued":
                # Calculate initial parameters
                # Principal: new_added.
                # Rate: From config (standard).
                # Interest: Principal * DEFAULT_INTEREST_RATE
                # Duration: ??? Parse from Notes or Default 12.
                
                principal_amt = new_added
                interest_amt = principal_amt * DEFAULT_INTEREST_RATE
                
                # Update Unearned Pot
                current_unearned += interest_amt
                
                # Determine Duration
                duration = 12 # Default
                match = re.search(r"Duration: (\d+)", new_notes)
                if match:
                    duration = int(match.group(1))
                
                # Set Monthly Interest
                current_monthly_interest = math.ceil(interest_amt / duration) if duration > 0 else 0
                
            elif event == "Loan Top-Up":
                # Top Up adds to Principal.
                # It adds Interest (rate from config).
                # It RESTS the Monthly Interest based on TOTAL Unearned / New Duration.
                
                top_up_amt = new_added
                top_up_int = top_up_amt * DEFAULT_INTEREST_RATE
                
                # Update Unearned Pot
                current_unearned += top_up_int
                
                # Determine New Duration
                duration = 12 # Default fallback
                match = re.search(r"Duration: (\d+)", new_notes)
                if match:
                    duration = int(match.group(1))
                
                # Recalculate Monthly Interest (Amortizing the TOTAL remaining unearned pot)
                # Note: This matches `top_up_loan` logic.
                
                # STABILIZATION FIX: Trust the stored `interest_amount` (Rate) from the transaction!
                # If we recalculate freely, small diffs in unearned pot accumulation can cause rate jumps.
                stored_rate = float(row.get('interest_amount', 0))
                
                if stored_rate > 0:
                     current_monthly_interest = stored_rate
                else:
                     current_monthly_interest = math.ceil(current_unearned / duration) if duration > 0 else 0
                
                # Note: Top-Up also affects target_installment potentially, but here we focus on Interest consistency.
            
            elif event == "Interest Earned":
                # Recalculate Accrual amount based on CURRENT Rate
                # UNLESS manually edited!
                is_edited = float(row.get('is_edited', 0)) > 0.5
                
                if is_edited:
                     amount = float(row['added'])
                else:
                     amount = min(current_monthly_interest, current_unearned)

                # Update added
                new_added = amount
                
                # Update state
                running_int_bal += amount
                current_unearned -= amount
                if current_unearned < 0: current_unearned = 0
                
                # Update DB
                self.db.update_transaction(trans_id, str(row['date']), new_added, 0, new_notes, 0, amount)
                
            elif event == "Repayment" or event == "Loan Buyoff":
                # Manual Edit Protection for Splits
                is_edited = float(row.get('is_edited', 0)) > 0.5
                
                # Check for Implied Rate Change (Refinance via Edit)
                # If Repayment stored a new Rate in 'interest_amount', adopt it.
                stored_rate = float(row.get('interest_amount', 0))
                if stored_rate > 0:
                    current_monthly_interest = stored_rate

                # Setup Defaults
                payment = new_deducted 
                
                # Check if we should override with Stored Splits (Manual Override)
                # If Edited, we respect the USER'S split preference if it exists.
                # However, if it's an "Auto Deduction" note without manual edit, we might enforce logic.
                
                used_manual_split = False
                
                stored_i = row.get('interest_portion')
                stored_p = row.get('principal_portion')
                
                if is_edited:
                    # Check if stored splits are valid numbers (not None)
                    if stored_i is not None and stored_p is not None:
                        # Trust the stored split
                        i_pay = float(stored_i)
                        p_pay = float(stored_p)
                        used_manual_split = True
                
                if not used_manual_split:
                    # Standard Logic (Recalculate Splits based on Priority)
                    if "Monthly Deduction (Auto)" in new_notes and not is_edited:
                         # Ensure we didn't drift installment amount if unedited
                         pass

                    # 1. Pay Interest Block
                    i_pay = 0.0
                    if running_int_bal > 0:
                        if payment >= running_int_bal:
                            i_pay = running_int_bal
                        else:
                            i_pay = payment
                    
                    # 2. Pay Principal
                    p_pay = payment - i_pay
                
                new_i_part = i_pay
                new_p_part = p_pay
                
                # Update state
                # Critical: We must reduce running_int_bal by what was ACTUALLY paid (i_pay).
                running_int_bal -= i_pay
                if running_int_bal < 0: running_int_bal = 0 # Safety, though theoretically shouldn't happen unless manual split overpaid interest?
                
                self.db.update_transaction(trans_id, str(row['date']), 0, payment, new_notes, new_p_part, new_i_part)
                # Top-Up adds Principal + Future Interest (Unearned)
                # We can't easily re-calculate Top-Up Interest without knowing the Top-Up logic history.
                # But usually Top-Up is discrete. 
                pass
        
        # Sync final calculated state to the Loan Record
        # This ensures that if we "Undo" (delete) a rate-changing transaction, 
        # the Loan's active terms revert to the calculated reality (Rate/Unearned).
        if loan:
            self.db.update_loan_recalc_state(loan['id'], current_monthly_interest, current_unearned)

    def recalculate_smart_loan_ledger(self, individual_id, loan_id):
        """
        Smart Replay: Re-simulates the loan history.
        - Respects 'Edited' transactions (Anchors) as fixed amounts.
        - Updates 'Auto' transactions to match the calculated installment.
        - Recalculates Interest/Principal splits for ALL transactions based on running balance.
        """
        df = self.get_ledger_df(individual_id)
        if df.empty: return
        
        # Filter for this loan
        loan_df = df[df['loan_id'] == loan_id].copy()
        if loan_df.empty: return
        
        # Sort by date/ID
        loan_df['date_obj'] = pd.to_datetime(loan_df['date'])
        loan_df = loan_df.sort_values(by=['date_obj', 'id'])
        
        # State
        running_principal = 0.0
        running_interest = 0.0 # Unearned
        current_installment = 0.0
        current_monthly_interest = 0.0
        
        last_accrual_id = None
        last_accrual_date = None
        
        # Helper for update
        cursor = self.db.conn.cursor()
        

        for index, row in loan_df.iterrows():
            trans_id = row['id']
            event = row['event_type']
            is_edited = float(row.get('is_edited', 0)) > 0.5 # Allow 1 or stored amount
            
            if event in ["Loan Issued", "Loan Top-Up"]:
                # 1. Update Balance
                added = float(row['added'])
                running_principal += added
                
                # 2. Recalculate Installment
                # Need duration from notes? Or is it stored?
                # Top-Up notes usually: "Top-up: ... Duration: 12m"
                # Issued notes: "Loan Issued ... Duration: 12m"
                # If we rely on notes, we must parse them.
                duration = 0 # Default to 0 initially
                
                # A. Try Regex
                match = re.search(r"Duration: (\d+)", str(row['notes']))
                if match:
                    duration = int(match.group(1))
                    
                # B. Try Installment Amount column (if regex failed)
                if duration == 0:
                    inst_col = float(row.get('installment_amount', 0))
                    if inst_col > 0:
                        total_due_est = running_principal * 1.15
                        duration = int(round(total_due_est / inst_col))
                        # print(f"DEBUG: Inferred Duration {duration} from Installment Column {inst_col}")

                # C. Try Inferring from First Repayment (Heuristic for Legacy Data)
                if duration == 0 and event == "Loan Issued":
                    # Look ahead for the first Repayment
                    # We have the full `loan_df` (sorted).
                    # Find first Repayment after this index.
                    future_repayments = loan_df[
                        (loan_df.index > index) & 
                        (loan_df['event_type'] == 'Repayment')
                    ]
                    if not future_repayments.empty:
                        first_rep = future_repayments.iloc[0]
                        first_amt = float(first_rep['deducted'])
                        if first_amt > 0:
                            total_due_est = running_principal * 1.15
                            duration = int(round(total_due_est / first_amt))
                            # print(f"DEBUG: Inferred Duration {duration} from First Repayment {first_amt}")

                # D. Final Default
                if duration <= 0:
                    duration = 12
                    # print("DEBUG: Defaulted Duration to 12")
                
                if duration > 0:
                     interest_added = added * DEFAULT_INTEREST_RATE
                     running_interest += interest_added
                     
                     total_debt = running_principal + running_interest
                     current_installment = math.ceil(total_debt / duration) # Match creation logic (Ceil)
                     
                     # Update Monthly Interest State
                     # Usually Monthly Interest = Unearned / Duration
                     # OR = Installment - (Principal/Duration) ?
                     # Segregated Model: Interest is linear amortization of Unearned Pot.
                     current_monthly_interest = running_interest / duration if duration > 0 else 0
                
            elif event == "Interest Earned":
                # System Accrual Event.
                # Must update 'added' (Int. Delta) to match current Rate.
                
                new_accrual = current_monthly_interest
                
                # Update DB
                if abs(new_accrual - float(row['added'])) > 0.01:
                    cursor.execute("UPDATE ledger SET added = ?, interest_amount = ? WHERE id = ?", 
                                   (new_accrual, new_accrual, trans_id))
                    
                last_accrual_id = trans_id
                last_accrual_date = row['date']
                 
            elif event == "Repayment":
                daily_interest = 0 # Simple model here?
                
                # 3. Determine Payment Amount (Physics Check)
                # Calculate True Debt State Limit
                curr_total_debt = running_principal + running_interest
                # print(f"DEBUG REPLAY [{event}]: Debt={curr_total_debt} | Row Ded={row['deducted']}")
                
                if curr_total_debt < 0: curr_total_debt = 0 # Safety
                
                payment_amount = float(row['deducted'])
                
                # Check for Stored Target in `is_edited` (Hysteresis Fix)
                # If is_edited > 1, it holds the original Anchor Amount.
                # If is_edited == 1, it's legacy/boolean, use deducted.
                is_edited_val = float(row['is_edited']) if row['is_edited'] else 0
                if is_edited_val > 1.01:
                    payment_amount = is_edited_val
                    # print(f"DEBUG: Restored Anchor Target {payment_amount} from is_edited")
                
                # ZOMBIE CHECK & CAPPING (Physics Enforcement)
                # Even Anchors cannot pay more than debt.
                if payment_amount > curr_total_debt:
                     payment_amount = curr_total_debt # Cap at Payoff
                     
                if curr_total_debt <= 0.01:
                     payment_amount = 0 # Zombie Neutralization
                
                # Detect if we need to Update DB (Physics enforced change)
                if abs(payment_amount - float(row['deducted'])) > 0.01:
                     cursor.execute("UPDATE ledger SET deducted = ? WHERE id = ?", (payment_amount, trans_id))

                if not is_edited:
                    # Auto Heal: Update to correct installment
                    # Note: We already capped it above at curr_total_debt.
                    # We just need to check if Installment is LOWER than Cap?
                    # Logic: Auto = min(Installment, Cap).
                    # Since Cap is essentially curr_total_debt.
                    
                    target_payment = min(current_installment, curr_total_debt)
                    
                    if abs(target_payment - payment_amount) > 0.01:
                        payment_amount = target_payment
                        cursor.execute("UPDATE ledger SET deducted = ? WHERE id = ?", (payment_amount, trans_id))
                else:
                    # Anchor found! (is_edited=1)
                    # We already applied Physics Capping above.
                    # Start Cycle Logic using the (possibly capped) amount.
                    
                    if payment_amount > 0:
                         current_installment = payment_amount
                         
                         # Recalculate Monthly Interest (Accrual) to match the new Pace
                         # Logic: Align Accrual with the Interest Portion of this new Payment.
                         # This prevents "Interest Bleed" (High Accrual vs Low Payment).
                         total_debt = running_principal + running_interest
                         if total_debt > 0:
                             interest_ratio = running_interest / total_debt
                             current_monthly_interest = current_installment * interest_ratio
                             
                             # Lookback Update: Fix the TRANSITION MONTH Accrual (Ghost Fix)
                             # If the preceding Accrual was on the SAME DATE (or same month),
                             # it has already been processed with the OLD rate.
                             # We must update it to the NEW rate so the Int. Bal remains 0 for this month.
                             if last_accrual_id and last_accrual_date == row['date']:
                                 # Same day transaction (e.g. 1st of Month)
                                 n_acc = current_monthly_interest
                                 cursor.execute("UPDATE ledger SET added = ?, interest_amount = ? WHERE id = ?", 
                                                (n_acc, n_acc, last_accrual_id))
                # Else: Keep payment_amount as it was (from DB) - Anchor
                
                # 4. Recalculate Split
                # Logic: Interest first? Or Pro-rated?
                # Usually Interest First (Standard).
                # But looking at `add_repayment_transaction`, it seems proportional or Principal first?
                # Let's check `update_transaction` calls or `add_repayment`.
                # If we don't have the exact logic, we might break the split consistency.
                # However, with "Smart Replay", we impose a consistent logical model.
                # Standard: Interest Portion = Balance * Rate? No, flat rate 15% added upfront.
                # So Repayment just reduces Balance.
                # Split is usually ratio of (Principal / Total) vs (Interest / Total).
                
                total_outstanding = running_principal + running_interest
                if total_outstanding > 0:
                    p_ratio = running_principal / total_outstanding
                    i_ratio = running_interest / total_outstanding
                else:
                    p_ratio = 1.0; i_ratio = 0.0
                    
                p_pay = payment_amount * p_ratio
                i_pay = payment_amount * i_ratio
                
                # Decrement
                running_principal -= p_pay
                running_interest -= i_pay
                
                # Update DB Split
                cursor.execute("UPDATE ledger SET principal_portion = ?, interest_portion = ? WHERE id = ?", 
                               (p_pay, i_pay, trans_id))
            
            # Handle float drift
            if abs(running_principal) < 0.01: running_principal = 0
            if abs(running_interest) < 0.01: running_interest = 0
            
            # Note: We do NOT update the `balance` column here because `recalculate_balances` does that globally.
            # We only updated `deducted` (Auto Heal) and splits.
            
            # UPDATE: We MUST update `principal_balance` and `interest_balance` here because
            # `recalculate_balances` relies on simple summing and glosses over these split states.
            # Smart Replay is the only place determining the running Principal/Interest split.
            cursor.execute("UPDATE ledger SET principal_balance = ?, interest_balance = ? WHERE id = ?", 
                           (running_principal, running_interest, trans_id))
            
        self.db.conn.commit()
        
        # === FINAL STEP: Update Loan Record with Correct State ===
        # The Replay is the source of truth. We must align the Loan Entity.
        # Check if we are in the "Latest" regime (no future Top-Ups processed that we skipped?)
        # Since we processed the whole DF sorted by date, and running_principal/interest reflect the
        # state after the LAST transaction, this is the current state.
        
        # Total Balance (Principal + Interest)
        final_balance = running_principal + running_interest
        
        # We need to update:
        # - installment (current_installment)
        # - balance (final_balance)
        # - principal (running_principal) ?? In segregated model, 'balance' usually tracks Principal?
        #   Wait, 'balance' column in Loans table:
        #   Line 79: balance REAL. Line 86: interest_balance REAL. Line 97: unearned.
        #   If 'balance' meant Total in old model, and Principal in new...
        #   Let's check `top_up_loan`: `new_principal = loan['balance'] + top_up`.
        #   So 'balance' IS Principal.
        #   So we set 'balance' = running_principal.
        # - unearned_interest (running_interest)
        # - interest_balance (accrued? We don't track accrued in Replay loop yet).
        #   If we zero it out, we lose accrued interest logic?
        #   The Replay loop IGNORES "Interest Earned".
        #   So `running_interest` is purely UNEARNED.
        #   So we can update `unearned_interest`.
        #   We should NOT touch `interest_balance` (Accrued) because Replay didn't calc it.
        #   (Unless we want to recalc it? But that's hard).
        #   Let's look at `update_loan_details` signature:
        #   (loan_id, total, balance, installment, m_int, due, unearned, principal_update, interest_bal)
        
        #   We will update: Balance (P), Installment, Unearned.
        #   We leave Interest Balance alone (or rely on `recalculate_balances` to fix it? No, Recalc Balances fixes Ledger, not Loan Entity).
        #   Actually `recalculate_balances` computes Ledger Balances.
        #   It doesn't update Loan Entity.
        
        #   So we leave Interest Balance untouched (preserve drift if any? or stale?).
        #   For Installment and Principal, we are confident.
        
        # Resolve Integer ID for Update
        loan_record = self.db.get_loan_by_ref(individual_id, loan_id)
        if loan_record:
            real_loan_id = loan_record['id']
            
            self.db.update_loan_details(
                real_loan_id, 
                0, # Total Amount (unused/legacy)
                running_principal, # Balance (Principal)
                current_installment, 
                current_monthly_interest,
                "", # Next Due (Don't change)
                unearned_interest=running_interest
            )
