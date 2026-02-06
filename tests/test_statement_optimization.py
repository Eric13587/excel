import sys
import unittest
from datetime import datetime

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager
from src.statement_generator import StatementGenerator
from src.data_structures import StatementData
from src.services.balance_calculator import BalanceRecalculator

class TestStatementOptimization(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        self.sg = StatementGenerator(self.db)
        self.calc = BalanceRecalculator(self.db)

    def tearDown(self):
        self.db.close()

    def test_get_statement_data(self):
        """Test retrieving consolidated statement data."""
        # Setup data
        self.db.add_loan_record(self.ind_id, "L1", 1000, 1200, 1200, 100, 0, "2026-01-01", "2026-02-01")
        self.db.add_savings_transaction(self.ind_id, "2026-01-15", "Deposit", 500)
        
        # Execute
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        
        # Verify
        self.assertIsInstance(data, StatementData)
        self.assertEqual(data.individual['name'], "Test User")
        self.assertEqual(len(data.active_loans), 1)
        self.assertFalse(data.savings_df.empty)
        self.assertEqual(data.savings_balance, 500)

    def test_prepare_presentation(self):
        """Test preparation of presentation model."""
        self.db.add_loan_record(self.ind_id, "L1", 1000, 1200, 1200, 100, 0, "2026-01-01", "2026-02-01")
        self.db.add_transaction(self.ind_id, "2026-01-01", "Loan Issued", "L1", 1000, 0, 1000, "Note")
        
        # Trigger recalculation to populate gross_balance
        self.calc.recalculate_balances(self.ind_id)
        
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        
        presentation = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31")
        
        self.assertEqual(presentation.customer_name, "Test User")
        self.assertEqual(len(presentation.loan_sections), 1)
        self.assertEqual(presentation.loan_sections[0].loan_ref, "L1")
        self.assertEqual(len(presentation.loan_sections[0].rows), 1)
        self.assertEqual(presentation.loan_sections[0].rows[0].debit, 1000)
        # Verify gross balance (1000 * 1.15 = 1150)
        self.assertEqual(presentation.loan_sections[0].rows[0].gross_balance, 1150)

    def test_generate_pdf_html_integration(self):
        """Test that _generate_pdf_html accepts StatementPresentation."""
        # Setup data
        self.db.add_loan_record(self.ind_id, "L1", 1000, 1200, 1200, 100, 0, "2026-01-01", "2026-02-01")
        self.db.add_transaction(self.ind_id, "2026-01-01", "Loan Issued", "L1", 1000, 0, 1000, "Note")
        
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        presentation = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31")
        
        # Execute
        html = self.sg._generate_pdf_html(presentation)
        
        # Verify
        self.assertIsNotNone(html)
        self.assertIn("Test User", html)
        self.assertIn("L1", html)

if __name__ == "__main__":
    unittest.main()
