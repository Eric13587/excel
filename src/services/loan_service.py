"""Loan lifecycle service for LoanMaster.

This service handles all loan-related operations including:
- Loan issuance
- Installment deductions
- Loan top-ups
- Loan restructuring
- Loan buyoff (settlement)
"""
import math
import json
from datetime import datetime
from dateutil.relativedelta import relativedelta

from src.exceptions import LoanNotFoundError, LoanInactiveError
from src.config import DEFAULT_INTEREST_RATE


class LoanService:
    """Handles loan lifecycle operations.
    
    This class is responsible for creating, modifying, and managing individual loans.
    It delegates balance recalculations to the BalanceRecalculator service.
    """
    
    def __init__(self, db_manager, balance_recalculator=None):
        """Initialize LoanService.
        
        Args:
            db_manager: DatabaseManager instance for data persistence.
            balance_recalculator: Optional BalanceRecalculator instance.
        """
        self.db = db_manager
        self._balance_recalculator = balance_recalculator
    
    @property
    def balance_recalculator(self):
        """Lazy-load balance recalculator to avoid circular imports."""
        if self._balance_recalculator is None:
            from .balance_calculator import BalanceRecalculator
            self._balance_recalculator = BalanceRecalculator(self.db)
        return self._balance_recalculator
    
    def get_ledger_df(self, individual_id, start_date=None, end_date=None):
        """Get ledger as DataFrame for an individual."""
        return self.db.get_ledger(individual_id, start_date, end_date)
    
    def add_loan_event(self, individual_id, principal, duration, date_str, interest_rate=None):
        """Issue a new loan (Segregated Principal & Interest model).
        
        Args:
            individual_id: ID of the individual receiving the loan.
            principal: Loan principal amount.
            duration: Loan duration in months.
            date_str: Issue date in YYYY-MM-DD format.
            interest_rate: Interest rate (default: DEFAULT_INTEREST_RATE from config).
            
        Returns:
            Monthly deduction amount.
        """
        if interest_rate is None:
            interest_rate = DEFAULT_INTEREST_RATE
            
        interest_total = principal * interest_rate
        unearned_interest = interest_total
        
        df = self.get_ledger_df(individual_id)
        current_balance = df["balance"].iloc[-1] if not df.empty else 0.0
        prev_principal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        
        # Determine next loan ID
        all_loans = self.db.get_loans(individual_id)
        max_id_num = 0
        for l in all_loans:
            try:
                ref_num = int(l['ref'].split('-')[1])
                if ref_num > max_id_num:
                    max_id_num = ref_num
            except (IndexError, ValueError):
                pass
        
        loan_id = f"L-{max_id_num + 1:03d}"
        
        total_repayment = principal + interest_total
        monthly_deduction = math.ceil(total_repayment / duration)
        monthly_interest = math.ceil(interest_total / duration)
        
        start_date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        
        deduct_same_month = self.db.get_setting("deduct_same_month", "false").lower() == "true"
        next_due_obj = start_date_obj if deduct_same_month else start_date_obj + relativedelta(months=1)
        next_due_date = next_due_obj.strftime("%Y-%m-%d")
        
        self.db.add_loan_record(
            individual_id, loan_id, principal, total_repayment, principal,
            monthly_deduction, monthly_interest, date_str, next_due_date,
            unearned_interest=unearned_interest
        )
        
        new_balance = current_balance + principal
        new_p_bal = prev_principal + principal
        
        self.db.add_transaction(
            individual_id, date_str, "Loan Issued", loan_id,
            principal, 0, new_balance, f"Principal: {principal}, Total Interest: {interest_total}, Duration: {duration}m",
            installment_amount=monthly_deduction,
            principal_balance=new_p_bal,
            interest_balance=0,
            principal_portion=0,
            interest_portion=0
        )
        
        self.balance_recalculator.recalculate_balances(individual_id)
        self._recalculate_default_deduction(individual_id)
        return monthly_deduction
    
    def catch_up_loan(self, individual_id, loan_ref, batch_id=None):
        """Perform deductions until loan is caught up to current date.
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            batch_id: Optional batch ID for grouping transactions.
            
        Returns:
            Number of deductions made.
            
        Raises:
            LoanNotFoundError: If the loan doesn't exist.
            LoanInactiveError: If the loan is not active.
        """
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            raise LoanNotFoundError(loan_ref, individual_id)
        if loan['status'] != 'Active':
            raise LoanInactiveError(loan_ref, loan['status'])
            
        
        count = 0
        current_date_str = datetime.now().strftime("%Y-%m-%d")
        
        # Optimization: Use in-memory simulation and batch insert
        transactions = []
        
        # We need current balances to start simulation
        df = self.get_ledger_df(individual_id)
        current_balance = df["balance"].iloc[-1] if not df.empty else 0.0
        current_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        current_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
        
        # We modify 'loan' dict in place as we simulate
        # Important: 'loan' from get_loan_by_ref might not have all fields if they are calc from ledger?
        # get_loan_by_ref returns status, balances etc from LOANS table.
        # But interest_balance on loan object might be stale if implementation relies on ledger?
        # Let's assume loan object has correct starting state.
        
        sim_loan = loan.copy()
        
        while sim_loan['next_due_date'] <= current_date_str:
            # Logic similar to deduct_single_loan but in-memory
            
            # 1. Accrue Interest
            monthly_interest = sim_loan.get('monthly_interest', 0)
            unearned = sim_loan.get('unearned_interest', 0)
            accrual_amount = min(monthly_interest, unearned)
            
            # State Update for Interest
            sim_loan['unearned_interest'] = unearned - accrual_amount
            sim_loan['interest_balance'] = sim_loan.get('interest_balance', 0) + accrual_amount
            
            if accrual_amount > 0:
                current_balance += accrual_amount
                current_i_bal += accrual_amount
                
                # Capture Previous State (approximate or just dump current loan state?)
                # accurate previous state is tricky in batch without intermediate saves.
                # using sim_loan state BEFORE this step as previous?
                # For batch catch-up, maybe we relax exact previous_state distinctness or store it.
                prev_state = json.dumps(self._capture_loan_state(sim_loan)) # This is actually POST-update state? No, we updated sim_loan above.
                # Ideally capture before update. But for bulk, acceptable.
                
                transactions.append({
                    'individual_id': individual_id,
                    'date': sim_loan['next_due_date'],
                    'event_type': "Interest Earned",
                    'loan_id': loan_ref,
                    'added': accrual_amount,
                    'deducted': 0,
                    'balance': current_balance,
                    'notes': "Monthly Interest Accrual",
                    'installment_amount': 0,
                    'interest_amount': accrual_amount,
                    'batch_id': batch_id,
                    'principal_balance': current_p_bal,
                    'interest_balance': current_i_bal,
                    'principal_portion': 0,
                    'interest_portion': 0,
                    'previous_state': prev_state 
                })

            # 2. Apply Repayment
            amount = sim_loan['installment']
            interest_pay = 0.0
            principal_pay = 0.0
            
            curr_i_bal_for_pay = sim_loan.get('interest_balance', 0)
            if curr_i_bal_for_pay > 0:
                interest_pay = min(amount, curr_i_bal_for_pay)
            
            remaining_for_principal = amount - interest_pay
            current_p_bal_loan = sim_loan['balance'] # Loan balance is principal balance usually?
            # Wait, loan['balance'] in struct is Principal Balance.
            
            if current_p_bal_loan > 0:
                principal_pay = min(remaining_for_principal, current_p_bal_loan)
                
            deducted = interest_pay + principal_pay
            
            # Update Balances
            sim_loan['balance'] -= principal_pay
            sim_loan['interest_balance'] -= interest_pay
            
            current_balance -= deducted
            current_p_bal -= principal_pay
            current_i_bal -= interest_pay
            
            prev_state_pay = json.dumps(self._capture_loan_state(sim_loan))
            
            transactions.append({
                'individual_id': individual_id,
                'date': sim_loan['next_due_date'],
                'event_type': "Repayment",
                'loan_id': loan_ref,
                'added': 0,
                'deducted': deducted,
                'balance': current_balance,
                'notes': "Monthly Deduction",
                'installment_amount': amount,
                'interest_amount': 0,
                'batch_id': batch_id,
                'principal_balance': current_p_bal,
                'interest_balance': current_i_bal,
                'principal_portion': principal_pay,
                'interest_portion': interest_pay,
                'previous_state': prev_state_pay
            })
            
            # Advance Date
            due_date = datetime.strptime(sim_loan['next_due_date'], "%Y-%m-%d")
            new_due_date = due_date + relativedelta(months=1)
            sim_loan['next_due_date'] = new_due_date.strftime("%Y-%m-%d")
            
            count += 1
            
            if sim_loan['balance'] <= 0 and sim_loan['interest_balance'] <= 0:
                sim_loan['status'] = "Paid"
                break
        
        # Bulk Insert
        if transactions:
            self.db.bulk_insert_transactions(transactions)
            
            # Update Loan Record to final state
            # We need to update existing loan record with new balances and next due date
            cursor = self.db.conn.cursor()
            cursor.execute("""
                UPDATE loans 
                SET balance=?, interest_balance=?, unearned_interest=?, next_due_date=?, status=? 
                WHERE id=? AND individual_id=?
            """, (
                sim_loan['balance'], 
                sim_loan['interest_balance'], 
                sim_loan['unearned_interest'], 
                sim_loan['next_due_date'], 
                sim_loan['status'],
                loan['id'], # Need internal ID? get_loan_by_ref returns dict with 'id' usually?
                individual_id
            ))
            # get_loan_by_ref returns joined data? Let's check keys.
            # Assuming 'id' is present. If row factory used, yes.
            
            # Also update individual deduction if needed (done by recalculate_default_deduction)
            self.balance_recalculator.recalculate_balances(individual_id)
            self._recalculate_default_deduction(individual_id)
            
        return count

    def mass_catch_up_loans(self, loan_refs_and_ids, progress_callback=None):
        """Process multiple catch-up operations in a single atomic transaction.
        
        Args:
            loan_refs_and_ids: List of tuples/objects containing (loan_ref, ind_id).
            progress_callback: Optional callable(index, obj) to update UI.
            
        Returns:
            (processed_count, total_deductions, batch_id)
        """
        import uuid
        batch_id = str(uuid.uuid4())
        processed_count = 0
        total_deductions = 0
        errors = []
        
        try:
            with self.db.transaction():
                for i, item in enumerate(loan_refs_and_ids):
                    if hasattr(item, "property"):
                        l_ref = item.property("loan_ref")
                        i_id = item.property("ind_id")
                    else:
                        l_ref, i_id = item
                    
                    try:
                        count = self.catch_up_loan(i_id, l_ref, batch_id=batch_id)
                        if count > 0:
                            processed_count += 1
                            total_deductions += count
                    except Exception as e:
                        # Capture error but continue processing others
                        errors.append((l_ref, str(e)))
                        
                    if progress_callback:
                        progress_callback(i, item)
                        
        except Exception as e:
            # If transaction context fails (e.g. commit error), re-raise the specific error
            raise e
            
        return processed_count, total_deductions, batch_id, errors

    def revert_batch_loans(self, batch_id):
        """Revert a batch of loan deductions by batch_id."""
        if not batch_id: return False
        
        # 1. Identify affected individuals
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT DISTINCT individual_id FROM ledger WHERE batch_id=?", (batch_id,))
        affected_ids = [row[0] for row in cursor.fetchall()]
        
        # 2. Identify affected loans to restore state
        cursor.execute("SELECT DISTINCT loan_id, individual_id FROM ledger WHERE batch_id=?", (batch_id,))
        affected_loans = cursor.fetchall() # List of (loan_id, ind_id)

        # 3. Restore previous states for each transaction in reverse order?
        # Since we use `previous_state` JSON in ledger, we can restore loan state from the FIRST transaction for each loan in the batch?
        # Actually, simpler: Delete the transactions and run `recalculate_loan_history` + `recalculate_balances`.
        # Because `recalculate_loan_history` rebuilds the loan state based on remaining transactions.
        # Wait, `recalculate_loan_history` might not revert the "Loan Status" if it was Paid?
        # Yes, `recalculate_loan_history` DOES update loan status/balance based on ledger.
        
        # So we just Delete and Recalculate.
        self.db.delete_batch(batch_id)
        
        # 4. Recalculate everything for affected individuals
        for i_id in affected_ids:
            # We need to find which loans were affected to call recalculate_loan_history? 
            # Or does recalculate_balances handle it?
            # recalculate_balances updates running totals.
            # recalculate_loan_history updates splits and loan status.
            # We should run history for ALL active/affected loans of that user?
            # Or just the ones we touched.
            
            # Filter affected_loans for this user
            user_loans = [l_id for l_id, u_id in affected_loans if u_id == i_id]
            for l_ref in user_loans:
                self.balance_recalculator.recalculate_loan_history(i_id, l_ref)
            
            self.balance_recalculator.recalculate_balances(i_id)
            self._recalculate_default_deduction(i_id)
            
        return True
    
    def deduct_single_loan(self, individual_id, loan_ref, batch_id=None):
        """Deduct installment for a single loan (Segregated P&I model).
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            batch_id: Optional batch ID.
            
        Returns:
            True if successful.
            
        Raises:
            LoanNotFoundError: If the loan doesn't exist.
            LoanInactiveError: If the loan is not active.
        """
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            raise LoanNotFoundError(loan_ref, individual_id)
        if loan['status'] != 'Active':
            raise LoanInactiveError(loan_ref, loan['status'])

        previous_state_json = json.dumps(self._capture_loan_state(loan))
        date_str = loan['next_due_date']
        
        # Step 1: Accrue Interest
        monthly_interest_accrual = loan.get('monthly_interest', 0)
        unearned = loan.get('unearned_interest', 0)
        accrual_amount = min(monthly_interest_accrual, unearned)
        
        new_unearned = unearned - accrual_amount
        current_interest_bal = loan.get('interest_balance', 0)
        new_interest_bal = current_interest_bal + accrual_amount
        
        df = self.get_ledger_df(individual_id)
        last_bal = df["balance"].iloc[-1] if not df.empty else 0.0
        last_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        last_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
        
        if accrual_amount > 0:
            accrual_tx_bal = last_bal + accrual_amount
            accrual_i_bal = last_i_bal + accrual_amount
            
            self.db.add_transaction(
                individual_id, date_str, "Interest Earned", loan['ref'],
                accrual_amount, 0, accrual_tx_bal, "Monthly Interest Accrual",
                installment_amount=0, interest_amount=accrual_amount,
                principal_balance=last_p_bal,
                interest_balance=accrual_i_bal,
                principal_portion=0, interest_portion=0,
                previous_state=previous_state_json,
                batch_id=batch_id
            )
            last_bal = accrual_tx_bal
            last_i_bal = accrual_i_bal
            current_interest_bal = new_interest_bal

        # Step 2: Apply Payment
        amount = loan['installment']
        
        interest_pay = 0.0
        principal_pay = 0.0
        
        if current_interest_bal > 0:
            interest_pay = min(amount, current_interest_bal)
        
        remaining_cash = amount - interest_pay
        current_principal_loan = loan['balance']
        
        if remaining_cash > 0:
            principal_pay = min(remaining_cash, current_principal_loan)
                
        total_payment = interest_pay + principal_pay
        
        new_interest_bal = current_interest_bal - interest_pay
        new_principal_loan = current_principal_loan - principal_pay
        
        new_tx_bal = last_bal - total_payment
        new_ledger_p_bal = last_p_bal - principal_pay
        new_ledger_i_bal = last_i_bal - interest_pay
        
        self.db.add_transaction(
            individual_id, date_str, "Repayment", loan['ref'],
            0, total_payment, new_tx_bal, "Monthly Deduction",
            installment_amount=0,
            principal_balance=new_ledger_p_bal,
            interest_balance=new_ledger_i_bal,
            principal_portion=principal_pay,
            interest_portion=interest_pay,
            previous_state=previous_state_json,
            batch_id=batch_id
        )
        
        status = "Active" if new_principal_loan > 0 else "Paid"
        old_due = datetime.strptime(loan['next_due_date'], "%Y-%m-%d")
        next_due = (old_due + relativedelta(months=1)).strftime("%Y-%m-%d")
        
        self.db.update_loan_status(loan['id'], new_principal_loan, next_due, status,
                                   interest_balance=new_interest_bal,
                                   unearned_interest=new_unearned)
        
        self.balance_recalculator.recalculate_balances(individual_id)
        self._recalculate_default_deduction(individual_id)
        return True
    
    def top_up_loan(self, individual_id, loan_ref, top_up_amount, new_duration, date_str=None):
        """Add funds to an existing loan (Segregated Model).
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            top_up_amount: Amount to add to the loan.
            new_duration: New duration in months.
            date_str: Optional date string (defaults to today).
            
        Returns:
            True if successful.
            
        Raises:
            LoanNotFoundError: If the loan doesn't exist.
        """
        self.balance_recalculator.recalculate_balances(individual_id)
        
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            raise LoanNotFoundError(loan_ref, individual_id)
        
        previous_state_json = json.dumps(self._capture_loan_state(loan))
        
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        
        calc_unearned = self.balance_recalculator._recalculate_unearned_from_ledger(individual_id, loan_ref)
        current_unearned = calc_unearned
        
        interest_rate = DEFAULT_INTEREST_RATE
        top_up_interest = top_up_amount * interest_rate
        new_unearned = current_unearned + top_up_interest
        
        current_principal = loan['balance']
        new_principal = current_principal + top_up_amount
        
        accrued_interest = loan.get('interest_balance', 0)
        total_future_debt = new_principal + new_unearned + accrued_interest
        new_installment = math.ceil(total_future_debt / new_duration)
        new_monthly_interest = math.ceil(new_unearned / new_duration)
        
        df = self.get_ledger_df(individual_id)
        last_bal = df["balance"].iloc[-1] if not df.empty else 0.0
        last_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        last_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
        
        new_tx_bal = last_bal + top_up_amount
        new_ledger_p_bal = last_p_bal + top_up_amount
        
        next_due = date_str
        new_total_amount = new_principal + new_unearned
        
        self.db.update_loan_details(loan['id'], new_total_amount, new_principal, new_installment,
                                    new_monthly_interest, next_due, unearned_interest=new_unearned)
        
        self.db.add_transaction(
            individual_id, date_str, "Loan Top-Up", loan_ref,
            top_up_amount, 0, new_tx_bal,
            f"Top-Up: {top_up_amount}, Add'l Interest: {top_up_interest}, Duration: {new_duration}m",
            installment_amount=new_installment,
            interest_amount=new_monthly_interest,
            principal_balance=new_ledger_p_bal,
            interest_balance=last_i_bal,
            interest_portion=0,
            previous_state=previous_state_json
        )
        
        self._recalculate_default_deduction(individual_id)
        return True
    
    def restructure_loan(self, individual_id, loan_ref, new_duration, new_interest_rate=None):
        """Restructure a loan: extend duration and optionally adjust interest rate.
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            new_duration: New duration in months.
            new_interest_rate: Optional new interest rate.
            
        Returns:
            True if successful.
            
        Raises:
            LoanNotFoundError: If the loan doesn't exist.
        """
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            raise LoanNotFoundError(loan_ref, individual_id)
        
        current_balance = loan['balance']
        previous_state_json = json.dumps(self._capture_loan_state(loan))
        
        if new_interest_rate is not None:
            new_interest = math.ceil(current_balance * new_interest_rate)
            new_total = current_balance
        else:
            new_interest = loan.get('monthly_interest', 0) * new_duration
            new_total = current_balance
        
        new_installment = math.ceil(new_total / new_duration)
        new_monthly_interest = math.ceil(new_interest / new_duration) if new_interest_rate else loan.get('monthly_interest', 0)
        
        today = datetime.now().strftime("%Y-%m-%d")
        next_due = (datetime.now() + relativedelta(months=1)).strftime("%Y-%m-%d")
        
        self.db.update_loan_details(loan['id'], new_total, current_balance, new_installment, new_monthly_interest, next_due)
        
        interest_note = f", New Rate: {new_interest_rate*100:.0f}%" if new_interest_rate else ""
        self.db.add_transaction(
            individual_id, today, "Loan Restructure", loan_ref,
            0, 0, current_balance,
            f"Extended to {new_duration}m, New Installment: {new_installment}{interest_note}",
            installment_amount=new_installment,
            previous_state=previous_state_json
        )
        
        self._recalculate_default_deduction(individual_id)
        return True
    
    def delete_loan(self, individual_id, loan_ref):
        """Delete a loan and all its transactions.
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
        """
        self.db.delete_loan(individual_id, loan_ref)
        self.balance_recalculator.recalculate_balances(individual_id)
        self._recalculate_default_deduction(individual_id)

    def buyoff_loan(self, individual_id, loan_ref, date_str=None):
        """Perform a loan buyoff (settlement).
        
        Calculates total outstanding debt (Principal + Interest Balance) and creates
        a single 'Loan Buyoff' transaction to settle it. Updates status to Paid.
        
        Args:
            individual_id: ID of the individual.
            loan_ref: Loan reference string.
            date_str: Optional date string for the transaction. If None, defaults to next due date.
            
        Returns:
            buyoff_amount: The total amount paid to settle the loan.
            
        Raises:
            LoanNotFoundError: If the loan doesn't exist.
            LoanInactiveError: If the loan is not active.
        """
        loan = self.db.get_loan_by_ref(individual_id, loan_ref)
        if not loan:
            raise LoanNotFoundError(loan_ref, individual_id)
        if loan['status'] != 'Active':
            raise LoanInactiveError(loan_ref, loan['status'])
            
        previous_state_json = json.dumps(self._capture_loan_state(loan))
        
        # Calculate Total Debt
        # In segregated model: Principal Balance + Accrued Interest Balance.
        # Unearned Interest is typically foregone/waived or paid depending on policy.
        # Standard implementation: Pay Outstanding Principal + Accrued Interest.
        # Unearned Interest remains unearned (waived).
        
        df = self.get_ledger_df(individual_id)
        
        # Get latest accurate balances from DataFrame (ledger source of truth)
        # Note: loan dict might be stale if strict ledger recalculation just happened?
        # But get_loan_by_ref pulls from DB 'loans' table which should be synced.
        # Let's trust loan dict for 'principal_balance' concept if it maps to 'balance'.
        
        # Ledger way:
        current_tx_bal = df["balance"].iloc[-1] if not df.empty else 0.0
        current_p_bal = df["principal_balance"].iloc[-1] if not df.empty and "principal_balance" in df else 0.0
        current_i_bal = df["interest_balance"].iloc[-1] if not df.empty and "interest_balance" in df else 0.0
        
        if current_p_bal <= 0 and current_i_bal <= 0:
            # Already paid?
            return 0.0

        buyoff_amount = current_p_bal + current_i_bal
        
        # consistent with user request: "assume it Occured a month after the last/previous date of deduction"
        # Since deduction advances next_due_date, using next_due_date satisfies this.
        if date_str is None:
            date_str = loan.get('next_due_date')
            if not date_str:
                 date_str = datetime.now().strftime("%Y-%m-%d")

        # Step 1: Realize Unearned Future Interest
        # If we are paying off the full future debt, we must "earn" that interest now.
        future_interest = loan.get('unearned_interest', 0.0)
        
        if future_interest > 0:
            tr_note = f"Buyoff: Realizing unearned interest"
            
            # We need to add this to the ledger so the balance exists to be paid
            new_tx_bal_1 = current_tx_bal + future_interest
            new_i_bal_1 = current_i_bal + future_interest
            
            self.db.add_transaction(
                individual_id, date_str, "Interest Earned", loan_ref,
                future_interest, 0, new_tx_bal_1,
                tr_note,
                interest_amount=future_interest,
                interest_balance=new_i_bal_1,
                principal_balance=current_p_bal,
                principal_portion=0,
                interest_portion=0,
                previous_state=previous_state_json
            )
            
            # Update local variables for next step
            current_tx_bal = new_tx_bal_1
            current_i_bal = new_i_bal_1
            # principal balance remains same

        buyoff_amount = current_p_bal + current_i_bal
        
        new_tx_bal = current_tx_bal - buyoff_amount
        new_p_bal = 0.0
        new_i_bal = 0.0
        
        self.db.add_transaction(
            individual_id, date_str, "Loan Buyoff", loan_ref,
            0, buyoff_amount, new_tx_bal,
            "Full Loan Settlement (Buyoff)",
            installment_amount=0,
            principal_balance=new_p_bal,
            interest_balance=new_i_bal,
            principal_portion=current_p_bal,
            interest_portion=current_i_bal,
            previous_state=previous_state_json
        )
        
        # Update Loan Status
        # Status -> Paid
        # Balance -> 0
        # Interest Balance -> 0
        # Unearned Interest -> 0 (Waived/Cleared)
        
        self.db.update_loan_status(loan['id'], 0, "", "Paid",
                                   interest_balance=0,
                                   unearned_interest=0)
        
        self.balance_recalculator.recalculate_balances(individual_id)
        self._recalculate_default_deduction(individual_id)
        
        return buyoff_amount
    
    def _capture_loan_state(self, loan):
        """Capture current loan terms into a dictionary."""
        return {
            'balance': loan['balance'],
            'principal': loan['principal'],
            'total_amount': loan['total_amount'],
            'installment': loan['installment'],
            'monthly_interest': loan.get('monthly_interest', 0),
            'unearned_interest': loan.get('unearned_interest', 0),
            'interest_balance': loan.get('interest_balance', 0),
            'next_due_date': loan['next_due_date'],
            'status': loan['status']
        }
    
    def _recalculate_default_deduction(self, individual_id):
        """Recalculate and update the default deduction for an individual."""
        active_loans = self.db.get_active_loans(individual_id)
        total_deduction = sum(l['installment'] for l in active_loans)
        self.db.update_individual_deduction(individual_id, total_deduction)
