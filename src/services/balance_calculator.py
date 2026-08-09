"""Balance calculation service for LoanMaster.

This service handles all balance recalculation operations including:
- Running balance recalculation
- Loan history replay
- Unearned interest calculations
"""
import logging

logger = logging.getLogger(__name__)

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
        """Re-derive every loan this member holds.

        Was 88 lines that re-added the running balance column while keeping
        whatever principal/interest split the rows already carried. That was one
        of four answers to "what may I regenerate?", and the four disagreed —
        see docs/db/loan-ledger-assessment.md. There is now one derivation, and
        this is the name the rest of the application already calls it by.
        """
        from src.services.loan_projection import project_member
        return project_member(self.db, individual_id)

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
        """Re-derive one loan. See :meth:`recalculate_balances`.

        Was 174 lines that recomputed the splits from the loan's *current*
        terms, which is wrong for any month those terms did not govern. The
        derivation reads the terms in force on each date instead.
        """
        from src.services.loan_projection import project_loan
        return project_loan(self.db, individual_id, loan_ref)

    def recalculate_smart_loan_ledger(self, individual_id, loan_id):
        """Re-derive one loan, addressed by primary key rather than ref.

        Was 266 lines of "smart replay" with no test coverage at all. It agreed
        with the others on an untouched loan and diverged on every edit.
        """
        from src.services.loan_projection import project_loan
        row = self.db.conn.execute(
            "SELECT individual_id, ref FROM loans WHERE id=?", (loan_id,)).fetchone()
        if row is None:
            # Older callers pass a loan *ref* here rather than an id.
            return project_loan(self.db, individual_id, loan_id)
        return project_loan(self.db, row[0], row[1])

