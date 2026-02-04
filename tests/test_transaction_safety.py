"""Tests for transaction safety and structured error handling."""
import sys
import os
import unittest

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.exceptions import (
    LoanNotFoundError,
    LoanInactiveError,
    TransactionError,
)


class TestExceptions(unittest.TestCase):
    """Test that custom exceptions work correctly."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")

    def tearDown(self):
        self.db.close()

    def test_loan_not_found_error_deduct(self):
        """Test that deduct_single_loan raises LoanNotFoundError."""
        with self.assertRaises(LoanNotFoundError) as context:
            self.engine.deduct_single_loan(self.ind_id, "L-NONEXISTENT")
        
        self.assertIn("L-NONEXISTENT", str(context.exception))

    def test_loan_not_found_error_top_up(self):
        """Test that top_up_loan raises LoanNotFoundError."""
        with self.assertRaises(LoanNotFoundError):
            self.engine.top_up_loan(self.ind_id, "L-NONEXISTENT", 1000, 12)

    def test_loan_not_found_error_restructure(self):
        """Test that restructure_loan raises LoanNotFoundError."""
        with self.assertRaises(LoanNotFoundError):
            self.engine.restructure_loan(self.ind_id, "L-NONEXISTENT", 12)

    def test_loan_inactive_error(self):
        """Test that deducting from a paid loan raises LoanInactiveError."""
        # Create a loan and mark it as paid
        self.engine.add_loan_event(self.ind_id, 1000, 1, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        loan_ref = loans[0]['ref']
        
        # Mark as paid
        self.db.update_loan_status(loans[0]['id'], 0, "2026-02-01", "Paid")
        
        # Now try to deduct
        with self.assertRaises(LoanInactiveError) as context:
            self.engine.deduct_single_loan(self.ind_id, loan_ref)
        
        self.assertIn("Paid", str(context.exception))


class TestConnectionManagement(unittest.TestCase):
    """Test database connection management."""

    def test_context_manager(self):
        """Test that DatabaseManager works as a context manager."""
        with DatabaseManager(":memory:") as db:
            db.add_individual("Context Test", "123", "ctx@test.com")
            inds = db.get_individuals()
            self.assertEqual(len(inds), 1)
        
        # Connection should be closed after exiting context
        self.assertTrue(db._closed)

    def test_explicit_close(self):
        """Test explicit close() method."""
        db = DatabaseManager(":memory:")
        db.add_individual("Close Test", "123", "close@test.com")
        
        self.assertFalse(db._closed)
        db.close()
        self.assertTrue(db._closed)
        
        # Calling close again should not raise
        db.close()


class TestTransactionContextManager(unittest.TestCase):
    """Test the transaction context manager."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")

    def tearDown(self):
        self.db.close()

    def test_transaction_commit_on_success(self):
        """Test that successful transactions are committed."""
        with self.db.transaction():
            cursor = self.db.conn.cursor()
            cursor.execute(
                "INSERT INTO individuals (name, phone, email, created_at) VALUES (?, ?, ?, ?)",
                ("Trans Test", "123", "trans@test.com", "2026-01-01")
            )
        
        # Verify the data persisted
        inds = self.db.get_individuals()
        self.assertEqual(len(inds), 1)
        self.assertEqual(inds[0][1], "Trans Test")

    def test_transaction_rollback_on_failure(self):
        """Test that failed transactions are rolled back."""
        try:
            with self.db.transaction():
                cursor = self.db.conn.cursor()
                cursor.execute(
                    "INSERT INTO individuals (name, phone, email, created_at) VALUES (?, ?, ?, ?)",
                    ("Rollback Test", "123", "rollback@test.com", "2026-01-01")
                )
                # Force an error
                raise ValueError("Simulated error")
        except ValueError:
            pass
        
        # Verify the data was rolled back
        inds = self.db.get_individuals()
        self.assertEqual(len(inds), 0)


class TestSQLInjectionFix(unittest.TestCase):
    """Test that SQL injection is prevented."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("SQL Test", "123", "sql@test.com")
        self.engine.add_loan_event(self.ind_id, 1000, 12, "2026-01-01", 0.15)

    def tearDown(self):
        self.db.close()

    def test_update_transaction_safe_with_special_chars(self):
        """Test that update_transaction handles special characters safely."""
        # Get a transaction
        df = self.db.get_ledger(self.ind_id)
        trans_id = int(df.iloc[0]['id'])
        
        # Try to update with potentially dangerous input
        # If SQL injection was possible, this might cause issues
        malicious_notes = "Test'; DROP TABLE ledger; --"
        
        self.db.update_transaction(
            trans_id,
            "2026-01-01",
            1000,
            0,
            malicious_notes,
            mark_edited=True,
            interest_amount=100
        )
        
        # Verify ledger still exists and note was saved as-is
        df = self.db.get_ledger(self.ind_id)
        self.assertGreater(len(df), 0)
        
        # Verify the note was stored correctly (escaped)
        trans = self.db.get_transaction(trans_id)
        self.assertEqual(trans['notes'], malicious_notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
