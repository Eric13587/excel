import sys
import os
import unittest
import sqlite3
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager

class TestDatabaseImport(unittest.TestCase):
    def setUp(self):
        self.main_db_path = "test_main.db"
        self.source_db_path = "test_source.db"
        
        # Cleanup
        if os.path.exists(self.main_db_path):
            os.remove(self.main_db_path)
        if os.path.exists(self.source_db_path):
            os.remove(self.source_db_path)
            
        # Setup Main DB
        self.main_db = DatabaseManager(self.main_db_path)
        
        # Setup Source DB
        self.source_db = DatabaseManager(self.source_db_path)
        
        # Initialize Tables if not implicitly done (DatabaseManager __init__ does it)
        pass

    def tearDown(self):
        self.main_db.conn.close()
        self.source_db.conn.close()
        if os.path.exists(self.main_db_path):
            os.remove(self.main_db_path)
        if os.path.exists(self.source_db_path):
            os.remove(self.source_db_path)

    def test_import_full_flow(self):
        print("Prepare Source Data...")
        # 1. Add Individual to Source
        self.source_db.add_individual("John Source", "555-0100", "john@source.com")
        
        # Get ID (should be 1)
        src_conn = self.source_db.conn
        cur = src_conn.cursor()
        cur.execute("SELECT id FROM individuals WHERE name='John Source'")
        ind_id = cur.fetchone()[0]
        
        # 2. Add Loan
        # add_loan_record signature: individual_id, ref, principal, total, balance, installment, monthly_interest, start_date, next_due_date
        self.source_db.add_loan_record(ind_id, "L-REF-101", 1000.0, 1100.0, 1100.0, 110.0, 10.0, "2023-01-01", "2023-02-01")
        
        # 3. Add Ledger Entry (Disbursement)
        # add_transaction signature: ind_id, date, event, loan_id, added, deducted, balance, notes, ...
        # Note: loan_id in add_transaction is usually text/int. 
        # But wait, add_loan_record inserts into 'loans'. It doesn't create a disbursement transaction automatically?
        # Let's manually add a transaction linked to the loan.
        cur.execute("SELECT id FROM loans WHERE ref='L-REF-101'")
        loan_id = cur.fetchone()[0]
        
        self.source_db.add_transaction(ind_id, "2023-01-01", "Disbursement", loan_id, 1000.0, 0, 1000.0, "Loan Issued", principal_portion=1000)
        
        # 4. Add Savings
        self.source_db.create_savings_table()
        self.source_db.add_savings_transaction(ind_id, "2023-01-05", "Deposit", 500.0, "Initial Savings")
        
        print("Running Import...")
        # 5. Run Import on Main DB
        options = {
            "import_loans": True,
            "import_savings": True
        }
        stats = self.main_db.import_data(self.source_db_path, options)
        
        print(f"Import returned stats (total ops): {stats}")
        
        # 6. Verify Results
        main_cur = self.main_db.conn.cursor()
        
        # Check Individual
        main_cur.execute("SELECT id, name FROM individuals WHERE name='John Source'")
        res = main_cur.fetchone()
        self.assertIsNotNone(res, "Individual not imported")
        new_ind_id = res[0]
        # self.assertNotEqual(new_ind_id, ind_id, "ID might be same in clean DB") 
        
        # Check Loan
        main_cur.execute("SELECT * FROM loans WHERE individual_id=?", (new_ind_id,))
        loan = main_cur.fetchone()
        self.assertIsNotNone(loan, "Loan not imported")
        self.assertEqual(loan[2], "L-REF-101", "Loan Ref mismatch")
        new_loan_id = loan[0]
        
        # Check Ledger
        main_cur.execute("SELECT * FROM ledger WHERE individual_id=?", (new_ind_id,))
        ledger = main_cur.fetchone()
        self.assertIsNotNone(ledger, "Ledger not imported")
        self.assertEqual(ledger[4], str(new_loan_id), "Ledger should link to NEW loan ID")
        self.assertEqual(ledger[8], "Loan Issued", "Notes mismatch")
        
        # Check Savings
        main_cur.execute("SELECT * FROM savings WHERE individual_id=?", (new_ind_id,))
        savings = main_cur.fetchone()
        self.assertIsNotNone(savings, "Savings not imported")
        self.assertEqual(savings[4], 500.0, "Savings amount mismatch")
        
        print("Verification Successful!")

if __name__ == "__main__":
    unittest.main()
