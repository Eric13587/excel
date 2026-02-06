import sys
import unittest
import os
from datetime import datetime

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager
from src.statement_generator import StatementGenerator

class TestStatementValidation(unittest.TestCase):
    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        self.sg = StatementGenerator(self.db)

    def tearDown(self):
        self.db.close()

    def test_validate_inputs_valid(self):
        """Test valid inputs pass."""
        try:
            self.sg._validate_inputs(self.ind_id, "2026-01-01", "2026-01-31")
        except ValueError:
            self.fail("_validate_inputs raised ValueError unexpectedly!")

    def test_validate_inputs_invalid_id(self):
        """Test missing ID raises error."""
        with self.assertRaisesRegex(ValueError, "Individual with ID 999 not found"):
            self.sg._validate_inputs(999, "2026-01-01", "2026-01-31")

    def test_validate_inputs_invalid_date_format(self):
        """Test bad date format raises error."""
        with self.assertRaisesRegex(ValueError, "Invalid date format"):
            self.sg._validate_inputs(self.ind_id, "01-01-2026", "2026-01-31")

    def test_validate_inputs_inverted_dates(self):
        """Test start > end raises error."""
        with self.assertRaisesRegex(ValueError, "Start date cannot be after end date"):
            self.sg._validate_inputs(self.ind_id, "2026-02-01", "2026-01-01")

    def test_sanitize_filename(self):
        """Test filename sanitization."""
        # Standard
        self.assertEqual(self.sg._sanitize_filename("John Doe"), "John Doe")
        # Special chars
        self.assertEqual(self.sg._sanitize_filename("John/Doe*?"), "JohnDoe")
        # Empty
        self.assertEqual(self.sg._sanitize_filename(""), "Unknown")
        self.assertEqual(self.sg._sanitize_filename("   "), "Statement")
        # Length
        long_name = "A" * 300
        sanitized = self.sg._sanitize_filename(long_name)
        self.assertEqual(len(sanitized), 100)

if __name__ == "__main__":
    unittest.main()
