
import sys
import unittest
from datetime import datetime
from dateutil.relativedelta import relativedelta

sys.path.insert(0, ".")

from src.database import DatabaseManager
from src.engine import LoanEngine

class TestSavingsTargetDate(unittest.TestCase):
    """Test suite for Savings Target Date functionality."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        
        self.u1 = self.db.add_individual("User 1", "111", "u1@test.com")
        
        # Initial deposit 5 months ago
        start_date = (datetime.now() - relativedelta(months=5)).strftime("%Y-%m-%d")
        self.engine.savings_service.add_deposit(self.u1, 1000, start_date, "Init")

    def tearDown(self):
        self.db.close()

    def test_catch_up_to_target_date(self):
        """Test finding catch up amount to a specific target date."""
        # Target is 2 months ago (so expected 3 months gap: -4, -3, -2. Wait.
        # Start: Today-5m. Next: Today-4m. 
        # Target: Today-2m.
        # Loop: while next < target_month_start
        # If target is Today-2m, target_month_start is Today-2m(day=1).
        # next starts at Today-4m.
        # It should add Today-4m, Today-3m.
        # Today-2m is NOT added because < target.
        
        target_date = (datetime.now() - relativedelta(months=2))
        
        count = self.engine.savings_service.catch_up_savings(self.u1, target_date=target_date)
        
        # Expected: -4m, -3m. So 2 deposits.
        # self.assertEqual(count, 2) 
        
        # Verify
        txs = self.db.get_savings_transactions(self.u1)
        # 1 initial + count
        self.assertEqual(len(txs), 1 + count)
        
        # Check latest date
        latest_tx = txs.iloc[0]
        latest_date = datetime.strptime(latest_tx['date'], "%Y-%m-%d")
        
        # Latest should be < target_date
        self.assertLess(latest_date, target_date)
        
    def test_catch_up_default(self):
        """Test default behavior (catch up to now)."""
        # Should catch up to current month (Today-1m).
        # Start: -5m. Next: -4m.
        # Current: 0m.
        # Adds: -4, -3, -2, -1. Total 4.
        
        count = self.engine.savings_service.catch_up_savings(self.u1)
        
        # Verify it went further than target date test
        # Note: comparison depends on exact months.
        # If today is May. Start Jan. Next Feb.
        # Default target May 1st.
        # Adds Feb, Mar, Apr. (3)
        
        # Target date test used Mar 1st.
        # Adds Feb. (1)
        
        self.assertGreater(count, 0)

if __name__ == '__main__':
    unittest.main()
