import sys
import unittest
import os
from datetime import datetime
import pandas as pd

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager
from src.statement_generator import StatementGenerator
from src.data_structures import StatementData, StatementConfig

class TestStatementCustomization(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        self.sg = StatementGenerator(self.db)
        
        # Setup Data
        self.db.add_loan_record(self.ind_id, "L1", 1000, 1200, 1200, 100, 0, "2026-01-01", "2026-02-01")
        self.db.add_transaction(self.ind_id, "2026-01-01", "Loan Issued", "L1", 1000, 0, 1000, "Note")
        self.db.add_savings_transaction(self.ind_id, "2026-01-15", "Deposit", 500)

    def tearDown(self):
        self.db.close()

    def test_custom_config_render(self):
        """Test that config changes affect presentation."""
        # 1. Test Hide Savings
        config = StatementConfig(show_savings=False)
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        pres = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31", config)
        
        html = self.sg._generate_pdf_html(pres, config)
        self.assertNotIn("SAVINGS / SHARES", html)
        
        # 2. Test Show Savings
        config.show_savings = True
        html = self.sg._generate_pdf_html(pres, config)
        self.assertIn("SAVINGS / SHARES", html)

    def test_custom_columns(self):
        """Test specific columns are rendered."""
        config = StatementConfig(columns=["Date", "Debit"])
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        pres = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31", config)
        
        html = self.sg._generate_pdf_html(pres, config)
        self.assertIn("<th>Date</th>", html)
        self.assertIn("<th>Debit</th>", html)
        self.assertNotIn("<th>Credit</th>", html)

    def test_custom_title_footer(self):
        """Test custom title and footer."""
        config = StatementConfig(custom_title="MY CUSTOM BANK", custom_footer="CONFIDENTIAL")
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        pres = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31", config)
        
        html = self.sg._generate_pdf_html(pres, config)
        self.assertIn("MY CUSTOM BANK", html)
        self.assertIn("CONFIDENTIAL", html)

    def test_date_format(self):
        """Test custom date format."""
        config = StatementConfig(date_format="%d/%m/%Y")
        data = self.db.get_statement_data(self.ind_id, "2026-01-01", "2026-01-31")
        pres = self.sg._prepare_presentation(data, "2026-01-01", "2026-01-31", config)
        
        # Check Period Display
        self.assertIn("01/01/2026", pres.period_display)

if __name__ == "__main__":
    unittest.main()
