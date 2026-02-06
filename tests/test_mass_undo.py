
import sys
import unittest
import uuid
from datetime import datetime
from unittest.mock import Mock

sys.path.insert(0, ".") # Ensure src is in path

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services.undo_manager import UndoManager, MassLoanCatchUpCommand, MassSavingsCatchUpCommand

class TestMassUndo(unittest.TestCase):
    """Test suite for Mass Operation Undo functionality."""

    def setUp(self):
        # Use in-memory DB
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        
        # Create Dummy Data
        self.u1 = self.db.add_individual("User 1", "111", "u1@test.com")
        self.u2 = self.db.add_individual("User 2", "222", "u2@test.com")
        
        # Create Loans
        # User 1: Loan 1
        self.engine.add_loan_event(self.u1, 5000, 10, "2025-01-01", 0.15) 
        # User 2: Loan 1
        self.engine.add_loan_event(self.u2, 10000, 10, "2025-01-01", 0.15)
        
        self.l1_ref = self.db.get_active_loans(self.u1)[0]['ref']
        self.l2_ref = self.db.get_active_loans(self.u2)[0]['ref']

        # Add Savings
        self.engine.savings_service.add_deposit(self.u1, 1000, "2025-01-01", "Init")
        self.engine.savings_service.add_deposit(self.u2, 2000, "2025-01-01", "Init")

    def tearDown(self):
        self.db.close()

    def test_mass_loan_catchup_undo(self):
        """Test mass loan catch-up execution and undo."""
        # 1. Prepare items for mass op
        items = [(self.l1_ref, self.u1), (self.l2_ref, self.u2)]
        
        # Mock callback
        cb = Mock()
        
        # 2. Execute Mass Operation
        result = self.engine.mass_catch_up_loans(items, progress_callback=cb)
        
        # Verify execution
        processed, total, errors = result
        self.assertTrue(processed > 0)
        self.assertTrue(total > 0)
        
        # Check DB for transactions
        ledger1 = self.db.get_ledger(self.u1)
        ledger2 = self.db.get_ledger(self.u2)
        
        self.assertGreater(len(ledger1), 1) # Should have repayments
        self.assertGreater(len(ledger2), 1)
        
        # Verify batch_id presence (manually check one transaction)
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT batch_id FROM ledger WHERE individual_id=? AND event_type='Repayment'", (self.u1,))
        batch_ids = [r[0] for r in cursor.fetchall()]
        self.assertTrue(any(bid is not None for bid in batch_ids))
        
        # 3. Undo
        undone_cmd = self.engine.undo_manager.undo()
        
        self.assertIsNotNone(undone_cmd)
        self.assertIsInstance(undone_cmd, MassLoanCatchUpCommand)
        
        # 4. Verify Undo Results
        # Ledger counts should be back to 1 (Loan Issued only)
        ledger1_after = self.db.get_ledger(self.u1)
        ledger2_after = self.db.get_ledger(self.u2)
        
        self.assertEqual(len(ledger1_after), 1)
        self.assertEqual(len(ledger2_after), 1)
        self.assertEqual(ledger1_after.iloc[0]['event_type'], 'Loan Issued')
        
        # Verify DB batch delete worked
        cursor.execute("SELECT count(*) FROM ledger WHERE batch_id IS NOT NULL")
        count = cursor.fetchone()[0]
        self.assertEqual(count, 0)

    def test_mass_savings_catchup_undo(self):
        """Test mass savings catch-up execution and undo."""
        # Setup: Ensure they have a previous deposit to catch up from.
        # Added in setUp.
        
        items = [self.u1, self.u2]
        cb = Mock()
        
        # 1. Execute Mass Op
        result = self.engine.mass_catch_up_savings(items, progress_callback=cb)
        processed, total, errors = result
        
        self.assertEqual(processed, 2)
        self.assertGreater(total, 0)
        
        # Verify transactions added
        sav1 = self.db.get_savings_transactions(self.u1)
        sav2 = self.db.get_savings_transactions(self.u2)
        
        self.assertGreater(len(sav1), 1)
        self.assertGreater(len(sav2), 1)
        
        # Check batch_id
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT batch_id FROM savings WHERE individual_id=?", (self.u1,))
        batch_ids = [r[0] for r in cursor.fetchall()]
        # One of them should be the batch_id (the new ones)
        self.assertTrue(any(bid is not None for bid in batch_ids if bid))

        # 2. Undo
        undone_cmd = self.engine.undo_manager.undo()
        self.assertIsInstance(undone_cmd, MassSavingsCatchUpCommand)
        
        # 3. Verify Undo
        sav1_after = self.db.get_savings_transactions(self.u1)
        sav2_after = self.db.get_savings_transactions(self.u2)
        
        self.assertEqual(len(sav1_after), 1) # Only initial deposit
        self.assertEqual(len(sav2_after), 1)
        
        self.assertEqual(float(sav1_after.iloc[0]['balance']), 1000.0) # Restored balance

    def test_redo_mass_loan(self):
        """Test redo functionality for mass loan operation."""
        items = [(self.l1_ref, self.u1)]
        self.engine.mass_catch_up_loans(items)
        
        # Undo
        self.engine.undo_manager.undo()
        
        # Redo
        redo_cmd = self.engine.undo_manager.redo()
        self.assertIsNotNone(redo_cmd)
        
        # Verify transactions re-added
        ledger1 = self.db.get_ledger(self.u1)
        self.assertGreater(len(ledger1), 1)
        
        # Verify batch_id is new or same? 
        # The command logic regenerates batch_id in `mass_catch_up_loans` method.
        # So it will be a NEW batch_id.
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT batch_id FROM ledger WHERE individual_id=? AND event_type='Repayment'", (self.u1,))
        batch_ids = [r[0] for r in cursor.fetchall()]
        self.assertTrue(any(bid is not None for bid in batch_ids))

if __name__ == '__main__':
    unittest.main()
