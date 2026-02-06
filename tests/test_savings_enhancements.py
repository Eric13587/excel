
import sys
import unittest
import uuid
from datetime import datetime
from dateutil.relativedelta import relativedelta
from unittest.mock import Mock

sys.path.insert(0, ".") 

from src.database import DatabaseManager
from src.services.savings_service import SavingsService

class TestSavingsEnhancements(unittest.TestCase):
    """Test suite for Savings Logic Enhancements."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.service = SavingsService(self.db)
        
        self.u1 = self.db.add_individual("User 1", "111", "u@test.com")
        
        # Add a deposit in the past (e.g. 2 months ago)
        self.past_date = (datetime.now() - relativedelta(months=2)).replace(day=1)
        self.past_date_str = self.past_date.strftime("%Y-%m-%d")
        
        self.service.add_deposit(self.u1, 1000, self.past_date_str, "Initial")

    def tearDown(self):
        self.db.close()

    def test_catch_up_includes_current_month(self):
        """Test that catch_up_savings includes the current month."""
        # Current state: Deposit 2 months ago.
        # Catch up should add: Last Month, Current Month.
        # Total 2 new transactions.
        
        count = self.service.catch_up_savings(self.u1)
        
        # Determine expected count
        # If today is Feb 6, last was Dec 1.
        # Catch up adds: Jan 1, Feb 1. (2 months)
        # Prev logic would stop at Feb 1 (limit), so only Jan.
        # New logic sets limit to Mar 1, so stops at Mar.
        
        # We can calculate dynamically to be safe
        now = datetime.now().replace(day=1)
        
        # DEBUG
        print(f"\nDEBUG: Past Date: {self.past_date}")
        print(f"DEBUG: Now (Limit base): {now}")
        
        
        txs = self.db.get_savings_transactions(self.u1)
        # print("DEBUG: Txs in DB:")
        # for _, row in txs.iterrows():
        #     print(f"  {row['date']} - {row['transaction_type']}")
            
        expected_months = 0
        curr = self.past_date + relativedelta(months=1)
        # Limit is (now + 1 month).replace(day=1) -> Mar 1 (exclusive equivalent)
        limit_date = (datetime.now() + relativedelta(months=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        while curr < limit_date: 
            expected_months += 1
            curr += relativedelta(months=1)
            
        self.assertEqual(count, expected_months)
        
        # Verify dates
        txs = self.db.get_savings_transactions(self.u1)
        last_tx_date = datetime.strptime(txs.iloc[-1]['date'], "%Y-%m-%d")
        self.assertEqual(last_tx_date.month, now.month)
        self.assertEqual(last_tx_date.year, now.year)

    def test_catch_up_is_uptodate(self):
        """Test that running catch_up again returns 0."""
        self.service.catch_up_savings(self.u1) # Run once
        
        # Run again
        count_2 = self.service.catch_up_savings(self.u1)
        self.assertEqual(count_2, 0)
        
    def test_target_date_inclusive(self):
        """Test that explicit target date is inclusive."""
        # Reset
        self.db.delete_savings_batch("non-existent") # Just clear logic if needed, but easier to make new user
        u2 = self.db.add_individual("User 2", "222", "u2@test.com")
        start_date = datetime(2025, 1, 1)
        self.service.add_deposit(u2, 1000, "2025-01-01", "Start")
        
        # Target: March 2025
        # Should add: Feb, Mar
        target = "2025-03-15"
        
        count = self.service.catch_up_savings(u2, target_date=target)
        
        self.assertEqual(count, 2) # Feb, Mar
        
        txs = self.db.get_savings_transactions(u2)
        dates = sorted(txs['date'].tolist())
        self.assertIn("2025-02-01", dates)
        self.assertIn("2025-03-01", dates)
        self.assertNotIn("2025-04-01", dates)

    def test_withdrawal_does_not_affect_increment(self):
        """Test that a recent withdrawal doesn't change the auto-increment amount."""
        # Setup: Deposit 1000, then Withdraw 500
        u3 = self.db.add_individual("User 3", "333", "u3@test.com")
        self.service.add_deposit(u3, 1000, "2024-01-01", "Initial")
        self.service.add_withdrawal(u3, 500, "2024-01-15", "Withdrawal")
        
        # Catch up for Feb, Mar...
        # If logic is broken, it would use 500 (last tx amount).
        # If fixed, it should use 1000 (last deposit amount).
        
        count = self.service.catch_up_savings(u3, target_date="2024-02-15")
        
        self.assertEqual(count, 1) # Should add Feb 1
        
        # check amount of new transaction
        txs = self.db.get_savings_transactions(u3)
        last_tx = txs.iloc[-1]
        
        self.assertEqual(last_tx['date'], "2024-02-01")
        self.assertEqual(last_tx['transaction_type'], "Deposit")
        self.assertEqual(float(last_tx['amount']), 1000.0)
        self.assertNotEqual(float(last_tx['amount']), 500.0)

if __name__ == '__main__':
    unittest.main()
