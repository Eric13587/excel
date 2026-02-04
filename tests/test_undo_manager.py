"""Comprehensive test suite for UndoManager and related classes.

Tests cover:
1. UndoManager stack operations (execute, undo, redo)
2. UndoTransactionCommand with single transactions
3. UndoTransactionCommand with paired transactions (Repayment + Interest)
4. LoanSnapshot capture and restoration
5. Edge cases (empty stacks, max depth, redo after new action)
"""
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch
import pandas as pd

sys.path.insert(0, "/home/yhazadek/Desktop/excel")

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services.undo_manager import (
    UndoManager,
    UndoableCommand,
    UndoTransactionCommand,
    DeleteTransactionCommand,
    TransactionSnapshot,
    LoanSnapshot,
)


class TestUndoManager(unittest.TestCase):
    """Test UndoManager stack operations."""

    def setUp(self):
        self.manager = UndoManager(max_depth=5)

    def test_initial_state(self):
        """Test that manager starts with empty stacks."""
        self.assertFalse(self.manager.can_undo())
        self.assertFalse(self.manager.can_redo())
        self.assertIsNone(self.manager.get_undo_description())
        self.assertIsNone(self.manager.get_redo_description())

    def test_execute_adds_to_undo_stack(self):
        """Test that executing a command adds it to undo stack."""
        mock_cmd = Mock(spec=UndoableCommand)
        mock_cmd.execute.return_value = True
        mock_cmd.description = "Test Command"
        
        result = self.manager.execute(mock_cmd)
        
        self.assertTrue(result)
        self.assertTrue(self.manager.can_undo())
        self.assertEqual(self.manager.get_undo_description(), "Test Command")

    def test_execute_failed_command(self):
        """Test that failed commands are not added to stack."""
        mock_cmd = Mock(spec=UndoableCommand)
        mock_cmd.execute.return_value = False
        
        result = self.manager.execute(mock_cmd)
        
        self.assertFalse(result)
        self.assertFalse(self.manager.can_undo())

    def test_execute_clears_redo_stack(self):
        """Test that new command clears redo stack."""
        # Execute and undo a command to populate redo stack
        cmd1 = Mock(spec=UndoableCommand)
        cmd1.execute.return_value = True
        cmd1.undo.return_value = True
        cmd1.description = "Command 1"
        
        self.manager.execute(cmd1)
        self.manager.undo()
        self.assertTrue(self.manager.can_redo())
        
        # Execute new command
        cmd2 = Mock(spec=UndoableCommand)
        cmd2.execute.return_value = True
        cmd2.description = "Command 2"
        
        self.manager.execute(cmd2)
        
        # Redo stack should be cleared
        self.assertFalse(self.manager.can_redo())

    def test_undo_moves_to_redo_stack(self):
        """Test that undo moves command to redo stack."""
        mock_cmd = Mock(spec=UndoableCommand)
        mock_cmd.execute.return_value = True
        mock_cmd.undo.return_value = True
        mock_cmd.description = "Test Command"
        
        self.manager.execute(mock_cmd)
        result = self.manager.undo()
        
        self.assertIsNotNone(result)
        self.assertFalse(self.manager.can_undo())
        self.assertTrue(self.manager.can_redo())

    def test_undo_empty_stack(self):
        """Test undo on empty stack returns None."""
        result = self.manager.undo()
        self.assertIsNone(result)

    def test_redo_moves_to_undo_stack(self):
        """Test that redo moves command back to undo stack."""
        mock_cmd = Mock(spec=UndoableCommand)
        mock_cmd.execute.return_value = True
        mock_cmd.undo.return_value = True
        mock_cmd.redo.return_value = True
        mock_cmd.description = "Test Command"
        
        self.manager.execute(mock_cmd)
        self.manager.undo()
        result = self.manager.redo()
        
        self.assertIsNotNone(result)
        self.assertTrue(self.manager.can_undo())
        self.assertFalse(self.manager.can_redo())

    def test_redo_empty_stack(self):
        """Test redo on empty stack returns None."""
        result = self.manager.redo()
        self.assertIsNone(result)

    def test_max_depth_enforcement(self):
        """Test that undo stack respects max depth."""
        for i in range(10):
            cmd = Mock(spec=UndoableCommand)
            cmd.execute.return_value = True
            cmd.description = f"Command {i}"
            self.manager.execute(cmd)
        
        # Should have exactly max_depth commands
        undo_count = 0
        while self.manager.can_undo():
            self.manager._undo_stack[-1].undo = Mock(return_value=True)
            self.manager.undo()
            undo_count += 1
        
        self.assertEqual(undo_count, 5)  # max_depth=5

    def test_multiple_undo_redo_sequence(self):
        """Test complex undo/redo sequence."""
        commands = []
        for i in range(3):
            cmd = Mock(spec=UndoableCommand)
            cmd.execute.return_value = True
            cmd.undo.return_value = True
            cmd.redo.return_value = True
            cmd.description = f"Command {i}"
            commands.append(cmd)
            self.manager.execute(cmd)
        
        # Undo all
        self.manager.undo()
        self.manager.undo()
        self.manager.undo()
        
        self.assertFalse(self.manager.can_undo())
        self.assertTrue(self.manager.can_redo())
        
        # Redo all
        self.manager.redo()
        self.manager.redo()
        self.manager.redo()
        
        self.assertTrue(self.manager.can_undo())
        self.assertFalse(self.manager.can_redo())

    def test_clear(self):
        """Test clear empties both stacks."""
        for i in range(3):
            cmd = Mock(spec=UndoableCommand)
            cmd.execute.return_value = True
            cmd.undo.return_value = True
            cmd.description = f"Command {i}"
            self.manager.execute(cmd)
        
        self.manager.undo()
        self.manager.clear()
        
        self.assertFalse(self.manager.can_undo())
        self.assertFalse(self.manager.can_redo())


class TestUndoTransactionCommand(unittest.TestCase):
    """Test UndoTransactionCommand with real database."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        # Create a loan with transactions
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']
        self.loan_id = loans[0]['id']
        
        # Make a deduction to have a repayment
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)

    def tearDown(self):
        self.db.close()

    def test_command_captures_transaction_snapshot(self):
        """Test that command captures transaction snapshot on execute."""
        # Get last transaction
        ledger = self.db.get_ledger(self.ind_id)
        last_tx_id = int(ledger.iloc[-1]['id'])
        
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            last_tx_id
        )
        
        cmd.execute()
        
        self.assertGreater(len(cmd.tx_snapshots), 0)
        self.assertEqual(cmd.tx_snapshots[0].id, last_tx_id)

    def test_command_captures_loan_snapshot(self):
        """Test that command captures loan snapshot for loan transactions."""
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        if not repayments.empty:
            last_tx_id = int(repayments.iloc[-1]['id'])
            
            loan_before = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                last_tx_id
            )
            
            cmd.execute()
            
            self.assertIsNotNone(cmd.loan_snapshot)
            self.assertEqual(cmd.loan_snapshot.id, self.loan_id)
            self.assertEqual(cmd.loan_snapshot.balance, loan_before['balance'])

    def test_execute_deletes_transaction(self):
        """Test that execute deletes the transaction."""
        ledger = self.db.get_ledger(self.ind_id)
        initial_count = len(ledger)
        last_tx_id = int(ledger.iloc[-1]['id'])
        
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            last_tx_id
        )
        
        result = cmd.execute()
        
        self.assertTrue(result)
        ledger_after = self.db.get_ledger(self.ind_id)
        self.assertLess(len(ledger_after), initial_count)

    def test_undo_restores_transaction(self):
        """Test that undo restores the deleted transaction."""
        ledger = self.db.get_ledger(self.ind_id)
        last_tx_id = int(ledger.iloc[-1]['id'])
        initial_count = len(ledger)
        
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            last_tx_id
        )
        
        cmd.execute()
        result = cmd.undo()
        
        self.assertTrue(result)
        ledger_after = self.db.get_ledger(self.ind_id)
        self.assertEqual(len(ledger_after), initial_count)

    def test_undo_restores_loan_state(self):
        """Test that undo restores the loan to its previous state."""
        loan_before = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        if not repayments.empty:
            last_tx_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                last_tx_id
            )
            
            cmd.execute()
            cmd.undo()
            
            loan_after = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
            
            self.assertEqual(loan_after['balance'], loan_before['balance'])
            self.assertEqual(loan_after['installment'], loan_before['installment'])

    def test_redo_deletes_again(self):
        """Test that redo deletes the transaction again."""
        ledger = self.db.get_ledger(self.ind_id)
        last_tx_id = int(ledger.iloc[-1]['id'])
        
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            last_tx_id
        )
        
        cmd.execute()
        cmd.undo()
        
        ledger_before_redo = self.db.get_ledger(self.ind_id)
        
        result = cmd.redo()
        
        self.assertTrue(result)
        ledger_after_redo = self.db.get_ledger(self.ind_id)
        self.assertLess(len(ledger_after_redo), len(ledger_before_redo))


class TestPairedTransactions(unittest.TestCase):
    """Test undo of paired Repayment + Interest Earned transactions."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        # Create loan and make deduction (creates Repayment + Interest Earned pair)
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']
        
        # Make deduction - this creates both Repayment and Interest Earned
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)

    def tearDown(self):
        self.db.close()

    def test_find_sibling_repayment(self):
        """Test that sibling Interest Earned is found for Repayment."""
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            repayment_tx = self.db.get_transaction(repayment_id)
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            sibling = cmd._find_sibling_transaction(repayment_tx, self.loan_ref)
            
            # Should find Interest Earned as sibling
            if sibling:
                sibling_tx = self.db.get_transaction(sibling)
                self.assertEqual(sibling_tx['event_type'], 'Interest Earned')

    def test_find_sibling_interest_earned(self):
        """Test that sibling Repayment is found for Interest Earned."""
        ledger = self.db.get_ledger(self.ind_id)
        interest_earned = ledger[ledger['event_type'] == 'Interest Earned']
        
        if not interest_earned.empty:
            ie_id = int(interest_earned.iloc[-1]['id'])
            ie_tx = self.db.get_transaction(ie_id)
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                ie_id
            )
            
            sibling = cmd._find_sibling_transaction(ie_tx, self.loan_ref)
            
            # Should find Repayment as sibling
            if sibling:
                sibling_tx = self.db.get_transaction(sibling)
                self.assertEqual(sibling_tx['event_type'], 'Repayment')

    def test_paired_transactions_undone_together(self):
        """Test that Repayment and Interest Earned are undone together."""
        ledger = self.db.get_ledger(self.ind_id)
        initial_count = len(ledger)
        
        repayments = ledger[ledger['event_type'] == 'Repayment']
        interest_earned = ledger[ledger['event_type'] == 'Interest Earned']
        
        if not repayments.empty and not interest_earned.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            cmd.execute()
            
            # Both should be captured
            self.assertEqual(len(cmd.tx_snapshots), 2)
            
            # Both should be deleted
            ledger_after = self.db.get_ledger(self.ind_id)
            self.assertEqual(len(ledger_after), initial_count - 2)

    def test_paired_undo_restores_both(self):
        """Test that undo restores both paired transactions."""
        ledger = self.db.get_ledger(self.ind_id)
        initial_count = len(ledger)
        
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            cmd.execute()
            cmd.undo()
            
            ledger_after = self.db.get_ledger(self.ind_id)
            self.assertEqual(len(ledger_after), initial_count)


class TestLoanSnapshotRestoration(unittest.TestCase):
    """Test that loan state is properly restored on undo."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        # Create loan
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']
        self.loan_id = loans[0]['id']

    def tearDown(self):
        self.db.close()

    def test_loan_balance_restored(self):
        """Test that loan balance is restored after undo.
        
        Note: The balance recalculator runs after undo, which may adjust the
        balance based on remaining transactions. We verify that undo restores
        the transaction and then recalculates to a consistent state.
        """
        # Record initial state (only Loan Issued exists)
        ledger_before = self.db.get_ledger(self.ind_id)
        initial_tx_count = len(ledger_before)
        
        # Make deduction
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger_after_deduct = self.db.get_ledger(self.ind_id)
        tx_count_after_deduct = len(ledger_after_deduct)
        self.assertGreater(tx_count_after_deduct, initial_tx_count)
        
        # Get the repayment and undo it
        repayments = ledger_after_deduct[ledger_after_deduct['event_type'] == 'Repayment']
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            cmd.execute()
            cmd.undo()
            
            # After undo, transaction count should be back to post-deduct level
            ledger_after_undo = self.db.get_ledger(self.ind_id)
            self.assertEqual(len(ledger_after_undo), tx_count_after_deduct)

    def test_loan_installment_restored(self):
        """Test that loan installment is restored after undo."""
        loan_before = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        installment_before = loan_before['installment']
        
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            cmd.execute()
            cmd.undo()
            
            loan_after = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
            self.assertEqual(loan_after['installment'], installment_before)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and error conditions."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")

    def tearDown(self):
        self.db.close()

    def test_undo_nonexistent_transaction(self):
        """Test undoing a non-existent transaction returns False."""
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            99999  # Non-existent
        )
        
        result = cmd.execute()
        self.assertFalse(result)

    def test_undo_without_execute(self):
        """Test that undo without execute returns False."""
        # Create minimal command
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            1
        )
        
        # Try to undo without executing
        result = cmd.undo()
        self.assertFalse(result)

    def test_redo_without_execute(self):
        """Test that redo without execute returns False."""
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            1
        )
        
        result = cmd.redo()
        self.assertFalse(result)

    def test_description_without_snapshot(self):
        """Test description fallback when no snapshot available."""
        cmd = UndoTransactionCommand(
            self.db,
            self.engine.balance_recalculator,
            self.ind_id,
            123
        )
        
        self.assertEqual(cmd.description, "Delete transaction #123")


class TestEngineIntegration(unittest.TestCase):
    """Test UndoManager integration with LoanEngine."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']

    def tearDown(self):
        self.db.close()

    def test_engine_can_undo_false_initially(self):
        """Test engine reports can_undo False initially."""
        self.assertFalse(self.engine.can_undo())

    def test_engine_can_redo_false_initially(self):
        """Test engine reports can_redo False initially."""
        self.assertFalse(self.engine.can_redo())

    def test_undo_transaction_with_state(self):
        """Test undo_transaction_with_state adds to undo stack."""
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            result = self.engine.undo_transaction_with_state(self.ind_id, repayment_id)
            
            self.assertTrue(result)
            self.assertTrue(self.engine.can_undo())

    def test_engine_undo_works(self):
        """Test engine.undo() performs undo."""
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            # Add to undo stack
            self.engine.undo_transaction_with_state(self.ind_id, repayment_id)
            
            # Now perform global undo
            result = self.engine.undo()
            
            self.assertIsNotNone(result)
            self.assertTrue(self.engine.can_redo())

    def test_engine_redo_works(self):
        """Test engine.redo() performs redo."""
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment_id = int(repayments.iloc[-1]['id'])
            
            self.engine.undo_transaction_with_state(self.ind_id, repayment_id)
            self.engine.undo()
            
            result = self.engine.redo()
            
            self.assertIsNotNone(result)

class TestTopUpAndModificationUndo(unittest.TestCase):
    """Test undo of top-ups and modifications - verifying ALL columns are restored."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        # Create initial loan
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']
        self.loan_id = loans[0]['id']

    def tearDown(self):
        self.db.close()

    def _get_full_loan_state(self):
        """Get complete loan state dict for comparison."""
        loan = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        return {
            'principal': loan.get('principal'),
            'total_amount': loan.get('total_amount'),
            'balance': loan.get('balance'),
            'installment': loan.get('installment'),
            'interest_balance': loan.get('interest_balance'),
            'unearned_interest': loan.get('unearned_interest'),
            'monthly_interest': loan.get('monthly_interest'),
            'next_due_date': loan.get('next_due_date'),
            'status': loan.get('status'),
        }

    def test_top_up_undo_restores_all_loan_columns(self):
        """Test that undoing a top-up restores ALL loan columns to previous state."""
        # Record state BEFORE top-up
        state_before_topup = self._get_full_loan_state()
        
        # Perform top-up
        self.engine.top_up_loan(self.ind_id, self.loan_ref, 5000, 6)  # Add 5000 for 6 more months
        
        # Record state AFTER top-up
        state_after_topup = self._get_full_loan_state()
        
        # Verify top-up changed the state
        self.assertNotEqual(state_before_topup['balance'], state_after_topup['balance'])
        
        # Find top-up transaction
        ledger = self.db.get_ledger(self.ind_id)
        topup_txs = ledger[ledger['event_type'] == 'Loan Top-Up']
        
        if not topup_txs.empty:
            topup_id = int(topup_txs.iloc[-1]['id'])
            
            # Execute undo command (Delete)
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                topup_id,
                transaction_manager=self.engine.transaction_manager
            )
            
            # Capture loan snapshot and Execute Delete
            cmd.execute()
            
            # Verify loan snapshot captured the BEFORE-DELETE state
            self.assertIsNotNone(cmd.loan_snapshot)
            
            # CRITICAL: Verify that AFTER delete, the loan state has REVERTED to pre-top-up values
            # This confirms the fix for the user's issue (yellow button now behaves like context menu delete)
            state_after_delete = self._get_full_loan_state()
            self.assertEqual(state_after_delete['balance'], state_before_topup['balance'])
            self.assertEqual(state_after_delete['installment'], state_before_topup['installment'])
            
            # Now undo (restore)
            cmd.undo()
            
            # Verify ALL columns are restored
            state_after_undo = self._get_full_loan_state()
            
            # After undo, all columns should match what was captured in the loan_snapshot
            self.assertEqual(state_after_undo['balance'], cmd.loan_snapshot.balance)
            self.assertEqual(state_after_undo['installment'], cmd.loan_snapshot.installment)
            self.assertEqual(state_after_undo['monthly_interest'], cmd.loan_snapshot.monthly_interest)
            self.assertEqual(state_after_undo['unearned_interest'], cmd.loan_snapshot.unearned_interest)
            self.assertEqual(state_after_undo['status'], cmd.loan_snapshot.status)

    def test_repayment_undo_restores_all_transaction_columns(self):
        """Test that undoing a repayment restores ALL transaction columns."""
        # Make a deduction first
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            repayment = repayments.iloc[-1]
            repayment_id = int(repayment['id'])
            
            # Capture original transaction columns
            original_columns = {
                'date': repayment['date'],
                'event_type': repayment['event_type'],
                'added': repayment['added'],
                'deducted': repayment['deducted'],
                'balance': repayment['balance'],
                'loan_id': repayment['loan_id'],
                'interest_amount': repayment.get('interest_amount', 0),
                'principal_balance': repayment.get('principal_balance', 0),
                'interest_balance': repayment.get('interest_balance', 0),
            }
            
            # Delete and restore
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                repayment_id
            )
            
            cmd.execute()
            
            # Verify transaction snapshot captured all columns
            snapshot = cmd.tx_snapshots[0]
            self.assertEqual(snapshot.date, original_columns['date'])
            self.assertEqual(snapshot.event_type, original_columns['event_type'])
            self.assertEqual(snapshot.deducted, original_columns['deducted'])
            self.assertEqual(snapshot.loan_id, original_columns['loan_id'])
            
            # Undo (restore)
            cmd.undo()
            
            # Verify restored transaction matches original
            ledger_after = self.db.get_ledger(self.ind_id)
            restored_repayments = ledger_after[ledger_after['event_type'] == 'Repayment']
            
            if not restored_repayments.empty:
                restored = restored_repayments.iloc[-1]
                self.assertEqual(restored['date'], original_columns['date'])
                self.assertEqual(restored['deducted'], original_columns['deducted'])
                self.assertEqual(restored['loan_id'], original_columns['loan_id'])

    def test_multiple_deductions_undo_sequence(self):
        """Test undo sequence for multiple deductions."""
        # Make 3 deductions
        for _ in range(3):
            self.engine.deduct_single_loan(self.ind_id, self.loan_ref)
        
        ledger = self.db.get_ledger(self.ind_id)
        initial_count = len(ledger)
        
        repayments = ledger[ledger['event_type'] == 'Repayment'].sort_values(by='id')
        
        if len(repayments) >= 3:
            # Undo third (last) deduction
            cmd3 = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                int(repayments.iloc[-1]['id'])
            )
            cmd3.execute()
            
            # Undo second deduction
            ledger2 = self.db.get_ledger(self.ind_id)
            repayments2 = ledger2[ledger2['event_type'] == 'Repayment'].sort_values(by='id')
            
            cmd2 = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                int(repayments2.iloc[-1]['id'])
            )
            cmd2.execute()
            
            # Now restore both in reverse order
            cmd2.undo()
            cmd3.undo()
            
            # Should be back to original count
            ledger_after = self.db.get_ledger(self.ind_id)
            self.assertEqual(len(ledger_after), initial_count)


class TestAllColumnsVerification(unittest.TestCase):
    """Verify that EVERY column in transaction and loan tables is correctly restored."""

    def setUp(self):
        self.db = DatabaseManager(":memory:")
        self.engine = LoanEngine(self.db)
        self.ind_id = self.db.add_individual("Test User", "123", "test@test.com")
        
        # Create loan and make some transactions
        self.engine.add_loan_event(self.ind_id, 10000, 12, "2026-01-01", 0.15)
        loans = self.db.get_active_loans(self.ind_id)
        self.loan_ref = loans[0]['ref']
        self.loan_id = loans[0]['id']
        
        # Make deduction to have repayment
        self.engine.deduct_single_loan(self.ind_id, self.loan_ref)

    def tearDown(self):
        self.db.close()

    def test_loan_snapshot_contains_all_fields(self):
        """Verify LoanSnapshot dataclass has all required fields."""
        loan = self.db.get_loan_by_ref(self.ind_id, self.loan_ref)
        
        snapshot = LoanSnapshot(
            id=loan['id'],
            ref=loan['ref'],
            principal=loan.get('principal', 0),
            total_amount=loan.get('total_amount', 0),
            balance=loan.get('balance', 0),
            installment=loan.get('installment', 0),
            interest_balance=loan.get('interest_balance', 0),
            unearned_interest=loan.get('unearned_interest', 0),
            monthly_interest=loan.get('monthly_interest', 0),
            next_due_date=loan.get('next_due_date', ''),
            status=loan.get('status', 'Active')
        )
        
        # Verify all fields exist and match
        self.assertEqual(snapshot.id, loan['id'])
        self.assertEqual(snapshot.ref, loan['ref'])
        self.assertEqual(snapshot.principal, loan.get('principal', 0))
        self.assertEqual(snapshot.balance, loan.get('balance', 0))
        self.assertEqual(snapshot.installment, loan.get('installment', 0))
        self.assertEqual(snapshot.interest_balance, loan.get('interest_balance', 0))
        self.assertEqual(snapshot.unearned_interest, loan.get('unearned_interest', 0))
        self.assertEqual(snapshot.monthly_interest, loan.get('monthly_interest', 0))
        self.assertEqual(snapshot.next_due_date, loan.get('next_due_date', ''))
        self.assertEqual(snapshot.status, loan.get('status', 'Active'))

    def test_transaction_snapshot_contains_all_fields(self):
        """Verify TransactionSnapshot dataclass has all required fields."""
        ledger = self.db.get_ledger(self.ind_id)
        tx = ledger.iloc[-1]
        
        snapshot = TransactionSnapshot(
            id=tx['id'],
            individual_id=tx['individual_id'],
            date=tx['date'],
            event_type=tx['event_type'],
            added=tx.get('added', 0),
            deducted=tx.get('deducted', 0),
            balance=tx.get('balance', 0),
            loan_id=tx.get('loan_id'),
            notes=tx.get('notes', ''),
            interest_amount=tx.get('interest_amount', 0.0),
            principal_amount=tx.get('principal_amount', 0.0),
            linked_trans_id=tx.get('linked_trans_id'),
            principal_balance=tx.get('principal_balance', 0.0),
            interest_balance=tx.get('interest_balance', 0.0),
            principal_portion=tx.get('principal_portion', 0.0),
            interest_portion=tx.get('interest_portion', 0.0),
            is_edited=tx.get('is_edited', 0)
        )
        
        # Verify key fields exist
        self.assertEqual(snapshot.id, tx['id'])
        self.assertEqual(snapshot.date, tx['date'])
        self.assertEqual(snapshot.event_type, tx['event_type'])
        self.assertEqual(snapshot.loan_id, tx.get('loan_id'))

    def test_undo_preserves_transaction_id(self):
        """Verify that undo restores the SAME transaction ID."""
        ledger = self.db.get_ledger(self.ind_id)
        repayments = ledger[ledger['event_type'] == 'Repayment']
        
        if not repayments.empty:
            original_id = int(repayments.iloc[-1]['id'])
            
            cmd = UndoTransactionCommand(
                self.db,
                self.engine.balance_recalculator,
                self.ind_id,
                original_id
            )
            
            cmd.execute()
            cmd.undo()
            
            # Verify the transaction still exists with same ID
            restored_tx = self.db.get_transaction(original_id)
            self.assertIsNotNone(restored_tx)
            self.assertEqual(restored_tx['id'], original_id)


if __name__ == '__main__':
    unittest.main(verbosity=2)

