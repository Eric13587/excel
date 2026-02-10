import unittest
from unittest.mock import MagicMock
import pandas as pd
from datetime import datetime
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '../src'))
from reports import ReportGenerator

class TestReportCalculations(unittest.TestCase):
    
    def setUp(self):
        self.mock_db = MagicMock()
        self.gen = ReportGenerator(self.mock_db)
        
        # Consistent dates
        self.m1_start = datetime(2025, 1, 1)
        self.m2_start = datetime(2025, 2, 1)
        self.m3_start = datetime(2025, 3, 1)
        self.fy_start_date = datetime(2024, 11, 1) # FY started Nov 2024

    def test_calculate_loan_interest_basic(self):
        """Test basic interest bucketing into months."""
        ledger_df = pd.DataFrame({
            'date': ['2025-01-15', '2025-02-10', '2025-03-05', '2025-04-01'],
            'event_type': ['Interest Earned', 'Interest Earned', 'Interest Earned', 'Interest Earned'],
            'amount': [0.0, 0.0, 0.0, 0.0], # Amount isn't used, interest_amount is
            'interest_amount': [100.0, 200.0, 300.0, 400.0],
            'loan_id': ['loan1', 'loan1', 'loan1', 'loan1'],
            'id': [1, 2, 3, 4]
        })
        
        q_dates = (self.m1_start, self.m2_start, self.m3_start, datetime(2025, 4, 1), datetime(2025, 4, 1))
        
        # Test basic bucketing
        res = self.gen._calculate_loan_interest(
             'loan1', ledger_df, q_dates, self.fy_start_date, '2025-01-01'
        )
        
        self.assertIsNotNone(res)
        self.assertEqual(res['m1'], 100.0)
        self.assertEqual(res['m2'], 200.0)
        self.assertEqual(res['m3'], 300.0)

    def test_calculate_loan_interest_multi_entry(self):
        """Test multiple interest entries in the same month."""
        ledger_df = pd.DataFrame({
            'date': ['2025-01-05', '2025-01-20'],
            'event_type': ['Interest Earned', 'Interest Earned'],
            'interest_amount': [50.0, 75.0],
            'loan_id': ['loan1', 'loan1'],
            'id': [1, 2]
        })
        
        q_dates = (self.m1_start, self.m2_start, self.m3_start, datetime(2025, 4, 1), datetime(2025, 4, 1))
        
        res = self.gen._calculate_loan_interest(
             'loan1', ledger_df, q_dates, self.fy_start_date, '2025-01-01'
        )
        
        self.assertIsNotNone(res)
        self.assertEqual(res['m1'], 125.0) # 50 + 75
        self.assertEqual(res['m2'], 0.0)

    def test_get_balance_from_tx_exact(self):
        """Test B/F calculation logic."""
        # Method signature: _get_balance_from_tx(self, tx, use_accrual)
        # It takes a ROW (Series), not a DataFrame.
        
        # Test 1: Principal Balance present
        tx = pd.Series({
            'principal_balance': 1000.0,
            'balance': 1100.0
        })
        bal = self.gen._get_balance_from_tx(tx, True)
        self.assertEqual(bal, 1000.0)
        
        # Test 2: Fallback to balance (Legacy)
        tx_legacy = pd.Series({
            'balance': 1100.0
            # missing principal_balance
        })
        bal = self.gen._get_balance_from_tx(tx_legacy, True)
        self.assertEqual(bal, 1100.0) # Should return raw balance
        
        # Test 3: Zero Principal Balance (Paid off)
        tx_paid = pd.Series({
            'principal_balance': 0.0,
            'balance': 50.0 # Some interest left?
        })
        bal = self.gen._get_balance_from_tx(tx_paid, True)
        self.assertEqual(bal, 0.0)

if __name__ == '__main__':
    unittest.main()
