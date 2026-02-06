
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, ".")

from src.database import DatabaseManager
from src.engine import LoanEngine

class TestMassPartialFailure(unittest.TestCase):
    """Test suite for Partial Failures in Mass Operations."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        
        self.u1 = self.db.add_individual("User 1", "111", "u1@test.com")
        self.u2 = self.db.add_individual("User 2", "222", "u2@test.com") # Will fail
        
        # User 1 Loan
        self.engine.add_loan_event(self.u1, 5000, 10, "2025-01-01", 0.15) 
        self.l1_ref = self.db.get_active_loans(self.u1)[0]['ref']
        
        # User 2 Loan
        self.engine.add_loan_event(self.u2, 5000, 10, "2025-01-01", 0.15) 
        self.l2_ref = self.db.get_active_loans(self.u2)[0]['ref']

    def tearDown(self):
        self.db.close()

    def test_partial_failure_loans(self):
        """Test that detailed errors are returned for failed loan operations."""
        items = [(self.l1_ref, self.u1), (self.l2_ref, self.u2)]
        
        # Prepare a side effect that fails for u2 but works for u1
        # We need to patch `catch_up_loan` on the service instance
        original_catch_up = self.engine.loan_service.catch_up_loan
        
        def side_effect(ind_id, loan_ref, batch_id=None):
            if ind_id == self.u2:
                raise ValueError("Simulated Failure for U2")
            return original_catch_up(ind_id, loan_ref, batch_id)
            
        with patch.object(self.engine.loan_service, 'catch_up_loan', side_effect=side_effect):
            result = self.engine.mass_catch_up_loans(items)
            
        processed, total, errors = result
        
        # Assertions
        self.assertEqual(processed, 1) # U1 succeeded
        self.assertEqual(len(errors), 1) # U2 failed
        self.assertEqual(errors[0][0], self.l2_ref) # Failed item ref
        self.assertIn("Simulated Failure", errors[0][1])

        # Verify DB state
        # U1 should have transactions
        ledger1 = self.db.get_ledger(self.u1)
        self.assertGreater(len(ledger1), 1)
        
        # U2 should NOT have NEW transactions (only initial loan)
        ledger2 = self.db.get_ledger(self.u2)
        self.assertEqual(len(ledger2), 1)

    def test_partial_failure_savings(self):
        """Test that detailed errors are returned for failed savings operations."""
        # Add initial deposits
        self.engine.savings_service.add_deposit(self.u1, 1000, "2025-01-01", "Init")
        self.engine.savings_service.add_deposit(self.u2, 2000, "2025-01-01", "Init")
        
        items = [self.u1, self.u2]
        
        original_catch_up = self.engine.savings_service.catch_up_savings
        
        def side_effect(ind_id, amount=None, batch_id=None):
            if ind_id == self.u2:
                raise ValueError("Simulated Savings Failure")
            return original_catch_up(ind_id, amount, batch_id)
            
        with patch.object(self.engine.savings_service, 'catch_up_savings', side_effect=side_effect):
            result = self.engine.mass_catch_up_savings(items)
            
        processed, total, errors = result
        
        self.assertEqual(processed, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Simulated Savings Failure", errors[0][1])
        
        # Verify U1 updated, U2 not
        sav1 = self.db.get_savings_transactions(self.u1)
        sav2 = self.db.get_savings_transactions(self.u2)
        
        self.assertGreater(len(sav1), 1)
        self.assertEqual(len(sav2), 1)

if __name__ == '__main__':
    unittest.main()
