
import unittest
import os
import sys
import math
from datetime import datetime

# Add project root
sys.path.insert(0, os.getcwd())

from src.database import DatabaseManager
from src.engine import LoanEngine

class TestGiants(unittest.TestCase):
    """Test the 'Giants': Complex Undo/Redo Scenarios (Top-Ups, Edits)."""
    
    def setUp(self):
        self.db_path = ":memory:"
        self.db = DatabaseManager(self.db_path)
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test Giant", "123", "test@test.com")
        
        # 1. Issue Loan
        # 10,000 at 15% for 10 months.
        # Total = 11,500. Installment = 1,150.
        self.engine.add_loan_event(self.ind_id, 10000, 10, "2023-01-01")
        self.loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = self.loans[0]['ref']
        
    def test_top_up_giant(self):
        """Verify Top-Up lifecycle: Create -> Delete (Revert) -> Undo (Restore)."""
        print("\n--- Testing Top-Up Giant ---")
        
        # Initial State
        loan0 = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        initial_inst = loan0['installment']
        initial_bal = loan0['balance']
        print(f"Initial: Installment={initial_inst}, Balance={initial_bal}")
        
        # 2. Perform Top-Up
        # Add 5,000. New Principal = 15,000. Interest on 5k = 750. Total Delta = 5,750.
        # Duration = 10 months. New Installment approx (11,500 + 5,750) / 10 = 1,725.
        self.engine.top_up_loan(self.ind_id, self.loan_ref, 5000, 10, "2023-01-05")
        
        loan1 = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        topup_inst = loan1['installment']
        topup_bal = loan1['balance']
        print(f"Post Top-Up: Installment={topup_inst}, Balance={topup_bal}")
        
        self.assertNotEqual(initial_inst, topup_inst)
        self.assertNotEqual(initial_bal, topup_bal)
        
        # 3. DELETE Top-Up (The "Giant" Step 1 - Reversion)
        # Using undo_last_for_loan which triggers the command execution (Delete)
        print("Deleting Top-Up...")
        self.engine.undo_last_for_loan(self.ind_id, self.loan_ref)
        
        loan2 = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        reverted_inst = loan2['installment']
        reverted_bal = loan2['balance']
        print(f"Post Delete: Installment={reverted_inst}, Balance={reverted_bal}")
        
        # VERIFY REVERSION
        self.assertEqual(reverted_inst, initial_inst, "Giant Slayed: Installment reverted correctly")
        self.assertEqual(reverted_bal, initial_bal, "Giant Slayed: Balance reverted correctly")
        
        # 4. UNDO Delete (The "Giant" Step 2 - Restoration)
        # Use Ctrl+Z logic (engine.undo())
        print("Restoring Top-Up (Ctrl+Z)...")
        self.engine.undo()
        
        loan3 = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        restored_inst = loan3['installment']
        restored_bal = loan3['balance']
        print(f"Post Restore: Installment={restored_inst}, Balance={restored_bal}")
        
        # VERIFY RESTORATION
        self.assertEqual(restored_inst, topup_inst, "Giant Slayed: Installment restored correctly")
        self.assertEqual(restored_bal, topup_bal, "Giant Slayed: Balance restored correctly")

if __name__ == "__main__":
    unittest.main()
