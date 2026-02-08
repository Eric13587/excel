
import unittest
from datetime import datetime
import os
import sys

# Add src to path
sys.path.append(os.getcwd())

from src.database import DatabaseManager
from src.services.loan_service import LoanService

class TestDateOverride(unittest.TestCase):
    def setUp(self):
        self.db_name = "test_date_override.db"
        if os.path.exists(self.db_name):
            os.remove(self.db_name)
        self.db = DatabaseManager(self.db_name)
        self.loan_service = LoanService(self.db)
        
    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_name):
            os.remove(self.db_name)
            
    def test_next_due_date_respected(self):
        # 1. Create a loan
        ind_id = self.db.add_individual("Override User", "777", "d@d.com")
        self.loan_service.add_loan_event(ind_id, 1000, 10, "2025-01-01")
        
        # 2. Manually force 'next_due_date' to a specific future date
        # different from today (2026-02-09)
        forced_date = "2099-12-31" 
        
        loans = self.db.get_active_loans(ind_id)
        loan_id = loans[0]['id']
        l_ref = loans[0]['ref']
        
        # SQL Injection for setup to bypass logic
        cursor = self.db.conn.cursor()
        cursor.execute("UPDATE loans SET next_due_date=? WHERE id=?", (forced_date, loan_id))
        self.db.conn.commit()
        
        # Verify setup
        updated_loan = self.db.get_loan_by_ref(ind_id, l_ref)
        self.assertEqual(updated_loan['next_due_date'], forced_date)
        
        # 3. Call buyoff_loan with date_str=None
        self.loan_service.buyoff_loan(ind_id, l_ref, date_str=None)
        
        # 4. Check Ledger
        ledger = self.db.get_ledger(ind_id)
        buyoff_tx = ledger.iloc[-1]
        
        print(f"Buyoff Date: {buyoff_tx['date']}")
        
        # Should be forced_date, NOT today
        current_date = datetime.now().strftime("%Y-%m-%d")
        self.assertNotEqual(str(buyoff_tx['date']), current_date)
        self.assertEqual(str(buyoff_tx['date']), forced_date)

if __name__ == '__main__':
    unittest.main()
