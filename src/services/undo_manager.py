"""Undo/Redo management service for LoanMaster.

This module implements a command pattern for supporting multi-step undo/redo
operations. Each undoable action is encapsulated as a command that can be
executed, undone, and redone.
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Any, Dict
from dataclasses import dataclass
from datetime import datetime
import json


@dataclass
class TransactionSnapshot:
    """Snapshot of a transaction for restoration."""
    id: int
    individual_id: int
    date: str
    event_type: str
    added: float
    deducted: float
    balance: float
    loan_id: Optional[str]
    notes: str
    interest_amount: float = 0.0
    principal_amount: float = 0.0
    linked_trans_id: Optional[int] = None
    principal_balance: float = 0.0
    interest_balance: float = 0.0
    principal_portion: float = 0.0
    interest_portion: float = 0.0
    is_edited: int = 0


@dataclass
class LoanSnapshot:
    """Snapshot of a loan's state for restoration.
    
    Captures all loan fields that may change when a transaction is undone,
    especially for top-ups, restructures, or manually edited deductions.
    """
    id: int
    ref: str
    principal: float
    total_amount: float
    balance: float
    installment: float
    interest_balance: float
    unearned_interest: float
    monthly_interest: float
    next_due_date: str
    status: str


class UndoableCommand(ABC):
    """Abstract base class for undoable commands."""
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of the command."""
        pass
    
    @abstractmethod
    def execute(self) -> bool:
        """Execute the command. Returns True on success."""
        pass
    
    @abstractmethod
    def undo(self) -> bool:
        """Undo the command. Returns True on success."""
        pass
    
    def redo(self) -> bool:
        """Redo the command. Default implementation re-executes."""
        return self.execute()


class UndoTransactionCommand(UndoableCommand):
    """Command for undoing a transaction with full loan state restoration.
    
    This command handles:
    1. Paired transactions (Repayment + Interest Earned) as a single operation
    2. Full loan state capture and restoration
    3. Proper balance recalculation after undo/redo
    
    On undo, both transaction(s) AND loan state are restored to their previous values.
    """
    
    def __init__(self, db_manager, balance_recalculator, individual_id: int, trans_id: int, transaction_manager=None):
        self.db = db_manager
        self.balance_recalculator = balance_recalculator
        self.individual_id = individual_id
        self.trans_id = trans_id
        self.transaction_manager = transaction_manager
        
        self.snapshot: Optional[TransactionSnapshot] = None
        self.tx_snapshots: List[TransactionSnapshot] = [] # Supports multiple (main + sibling)
        self.loan_snapshot: Optional[LoanSnapshot] = None
        self.sibling_id: Optional[int] = None
        
        self._was_executed = False
    
    @property
    def description(self) -> str:
        if self.tx_snapshots:
            # Describe the primary transaction
            return f"Delete {self.tx_snapshots[0].event_type} ({self.tx_snapshots[0].date})"
        return f"Delete transaction #{self.trans_id}"
    
    def _capture_transaction_snapshot(self, tx: dict) -> TransactionSnapshot:
        """Helper to create snapshot from transaction dict."""
        return TransactionSnapshot(
            id=tx['id'],
            individual_id=self.individual_id,
            date=tx['date'],
            event_type=tx['event_type'],
            added=tx['added'],
            deducted=tx['deducted'],
            balance=tx['balance'],
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
    
    def _capture_loan_snapshot(self, loan: dict) -> LoanSnapshot:
        """Helper to create loan state snapshot."""
        return LoanSnapshot(
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
    
    def _find_sibling_transaction(self, tx: dict, loan_ref: str) -> Optional[int]:
        """Find paired transaction (Repayment <-> Interest Earned)."""
        event_type = tx['event_type']
        
        if event_type not in ("Repayment", "Interest Earned"):
            return None
        
        # Get all transactions for this loan
        ledger_df = self.db.get_ledger(self.individual_id)
        if ledger_df.empty:
            return None
        
        loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
        
        # Find this transaction's position
        my_idx_list = loan_txs.index[loan_txs['id'] == int(tx['id'])].tolist()
        if not my_idx_list:
            return None
        
        idx = loan_txs.index.get_loc(my_idx_list[0])
        
        if event_type == "Repayment":
            # Interest Earned should be BEFORE the Repayment (idx - 1)
            if idx > 0:
                prev = loan_txs.iloc[idx - 1]
                if prev['event_type'] == "Interest Earned" and prev['date'] == tx['date']:
                    return int(prev['id'])
        elif event_type == "Interest Earned":
            # Repayment should be AFTER the Interest Earned (idx + 1)
            if idx < len(loan_txs) - 1:
                nxt = loan_txs.iloc[idx + 1]
                if nxt['event_type'] == "Repayment" and nxt['date'] == tx['date']:
                    return int(nxt['id'])
        
        return None
    
    def execute(self) -> bool:
        """Delete the transaction using TransactionManager logic, capturing snapshots for undo."""
        # Get main transaction
        tx = self.db.get_transaction(self.trans_id)
        if not tx:
            return False
        
        loan_ref = tx.get('loan_id')
        
        # Capture main transaction snapshot
        self.tx_snapshots = [self._capture_transaction_snapshot(tx)]
        
        # Capture loan snapshot BEFORE any deletion
        if loan_ref and loan_ref != "-":
            loan = self.db.get_loan_by_ref(self.individual_id, loan_ref)
            if loan:
                self.loan_snapshot = self._capture_loan_snapshot(loan)
            
            # Find and capture sibling transaction for SNAPSHOT purposes
            # TransactionManager will find and delete it independently, but we need
            # the data to restore it if user Undoes.
            self.sibling_id = self._find_sibling_transaction(tx, loan_ref)
            if self.sibling_id:
                sibling_tx = self.db.get_transaction(self.sibling_id)
                if sibling_tx:
                    self.tx_snapshots.append(self._capture_transaction_snapshot(sibling_tx))
        
        # Delegate actual deletion to TransactionManager if available
        # This ensures detailed logic (like reverting top-up terms) is applied
        if self.transaction_manager:
            self.transaction_manager.delete_transaction(self.individual_id, self.trans_id)
        else:
            # Fallback for tests or legacy calls (though we should avoid this path)
            cursor = self.db.conn.cursor()
            if self.sibling_id:
                cursor.execute("DELETE FROM ledger WHERE id = ?", (self.sibling_id,))
            cursor.execute("DELETE FROM ledger WHERE id = ?", (self.trans_id,))
            self.db.conn.commit()
            
            # Recalculate balances (TransactionManager handles this internally if called)
            if self.balance_recalculator:
                if loan_ref and loan_ref != "-":
                    self.balance_recalculator.recalculate_loan_history(self.individual_id, loan_ref)
                self.balance_recalculator.recalculate_balances(self.individual_id)
        
        self._was_executed = True
        return True
    
    def undo(self) -> bool:
        """Restore the transaction(s) AND loan state."""
        if not self.tx_snapshots:
            return False
        
        cursor = self.db.conn.cursor()
        
        # Restore loan state FIRST (before transactions)
        if self.loan_snapshot:
            cursor.execute("""
                UPDATE loans SET
                    principal = ?, total_amount = ?, balance = ?, installment = ?,
                    interest_balance = ?, unearned_interest = ?, monthly_interest = ?,
                    next_due_date = ?, status = ?
                WHERE id = ?
            """, (
                self.loan_snapshot.principal,
                self.loan_snapshot.total_amount,
                self.loan_snapshot.balance,
                self.loan_snapshot.installment,
                self.loan_snapshot.interest_balance,
                self.loan_snapshot.unearned_interest,
                self.loan_snapshot.monthly_interest,
                self.loan_snapshot.next_due_date,
                self.loan_snapshot.status,
                self.loan_snapshot.id
            ))
        
        # Restore transactions (in correct order - sibling/Interest first, then main)
        # Sort by ID to ensure proper order (Interest Earned typically has lower ID)
        sorted_snapshots = sorted(self.tx_snapshots, key=lambda s: s.id)
        
        for snapshot in sorted_snapshots:
            cursor.execute("""
                INSERT INTO ledger (
                    id, individual_id, date, event_type, added, deducted, 
                    balance, loan_id, notes, interest_amount, 
                    principal_balance, interest_balance, principal_portion, interest_portion, is_edited
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.id,
                snapshot.individual_id,
                snapshot.date,
                snapshot.event_type,
                snapshot.added,
                snapshot.deducted,
                snapshot.balance,
                snapshot.loan_id,
                snapshot.notes,
                snapshot.interest_amount,
                snapshot.principal_balance,
                snapshot.interest_balance,
                snapshot.principal_portion,
                snapshot.interest_portion,
                snapshot.is_edited
            ))
        
        self.db.conn.commit()
        
        # Recalculate balances
        if self.balance_recalculator:
            loan_ref = self.tx_snapshots[0].loan_id
            if loan_ref and loan_ref != "-":
                self.balance_recalculator.recalculate_loan_history(self.individual_id, loan_ref)
            self.balance_recalculator.recalculate_balances(self.individual_id)
        
        self._was_executed = False
        return True
    
    def redo(self) -> bool:
        """Re-execute the delete (redo)."""
        if not self.tx_snapshots:
            return False
        
        main_snapshot = self.tx_snapshots[0]
        loan_ref = main_snapshot.loan_id
        
        # Re-capture loan state before re-deleting
        if loan_ref and loan_ref != "-":
            loan = self.db.get_loan_by_ref(self.individual_id, loan_ref)
            if loan:
                self.loan_snapshot = self._capture_loan_snapshot(loan)
        
        # Delete the transaction(s) again
        cursor = self.db.conn.cursor()
        for snapshot in self.tx_snapshots:
            cursor.execute("DELETE FROM ledger WHERE id = ?", (snapshot.id,))
        self.db.conn.commit()
        
        # Recalculate balances
        if self.balance_recalculator:
            if loan_ref and loan_ref != "-":
                self.balance_recalculator.recalculate_loan_history(self.individual_id, loan_ref)
            self.balance_recalculator.recalculate_balances(self.individual_id)
        
        self._was_executed = True
        return True


class DeleteTransactionCommand(UndoableCommand):
    """Legacy command for deleting a transaction (can be undone by restoring).
    
    Note: For new code, prefer UndoTransactionCommand which also captures loan state.
    """
    
    def __init__(self, db_manager, individual_id: int, trans_id: int, 
                 balance_recalculator=None, snapshot: Optional[TransactionSnapshot] = None):
        self.db = db_manager
        self.individual_id = individual_id
        self.trans_id = trans_id
        self.balance_recalculator = balance_recalculator
        self.snapshot = snapshot
        self._was_executed = False
    
    @property
    def description(self) -> str:
        if self.snapshot:
            return f"Delete {self.snapshot.event_type} ({self.snapshot.date})"
        return f"Delete transaction #{self.trans_id}"
    
    def execute(self) -> bool:
        """Delete the transaction, saving snapshot for undo."""
        if not self.snapshot:
            # Capture snapshot before deletion
            tx = self.db.get_transaction(self.trans_id)
            if not tx:
                return False
            
            self.snapshot = TransactionSnapshot(
                id=tx['id'],
                individual_id=self.individual_id,
                date=tx['date'],
                event_type=tx['event_type'],
                added=tx['added'],
                deducted=tx['deducted'],
                balance=tx['balance'],
                loan_id=tx.get('loan_id'),
                notes=tx.get('notes', ''),
                interest_amount=tx.get('interest_amount', 0.0),
                principal_amount=tx.get('principal_amount', 0.0),
                linked_trans_id=tx.get('linked_trans_id')
            )
        
        # Delete the transaction
        cursor = self.db.conn.cursor()
        cursor.execute("DELETE FROM ledger WHERE id = ?", (self.trans_id,))
        self.db.conn.commit()
        
        # Recalculate balances
        if self.balance_recalculator:
            self.balance_recalculator.recalculate(self.individual_id)
        
        self._was_executed = True
        return True
    
    def undo(self) -> bool:
        """Restore the deleted transaction."""
        if not self.snapshot:
            return False
        
        cursor = self.db.conn.cursor()
        cursor.execute("""
            INSERT INTO ledger (id, individual_id, date, event_type, added, deducted, 
                               balance, loan_id, notes, interest_amount, principal_amount, linked_trans_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            self.snapshot.id,
            self.snapshot.individual_id,
            self.snapshot.date,
            self.snapshot.event_type,
            self.snapshot.added,
            self.snapshot.deducted,
            self.snapshot.balance,
            self.snapshot.loan_id,
            self.snapshot.notes,
            self.snapshot.interest_amount,
            self.snapshot.principal_amount,
            self.snapshot.linked_trans_id
        ))
        self.db.conn.commit()
        
        # Recalculate balances
        if self.balance_recalculator:
            self.balance_recalculator.recalculate(self.individual_id)
        
        self._was_executed = False
        return True


class UndoManager:
    """Manages undo/redo stacks for multi-step operations.
    
    Maintains separate stacks for undo and redo operations with a configurable
    maximum depth to prevent unbounded memory usage.
    """
    
    def __init__(self, max_depth: int = 20):
        self._undo_stack: List[UndoableCommand] = []
        self._redo_stack: List[UndoableCommand] = []
        self._max_depth = max_depth
    
    def execute(self, command: UndoableCommand) -> bool:
        """Execute a command and add it to the undo stack.
        
        Clears the redo stack since the command history has diverged.
        """
        if command.execute():
            self._undo_stack.append(command)
            if len(self._undo_stack) > self._max_depth:
                self._undo_stack.pop(0)  # Remove oldest
            self._redo_stack.clear()  # New action clears redo history
            return True
        return False
    
    def undo(self) -> Optional[UndoableCommand]:
        """Undo the last command.
        
        Returns the undone command on success, None if stack is empty.
        """
        if not self._undo_stack:
            return None
        
        command = self._undo_stack.pop()
        if command.undo():
            self._redo_stack.append(command)
            return command
        else:
            # Undo failed, put it back
            self._undo_stack.append(command)
            return None
    
    def redo(self) -> Optional[UndoableCommand]:
        """Redo the last undone command.
        
        Returns the redone command on success, None if stack is empty.
        """
        if not self._redo_stack:
            return None
        
        command = self._redo_stack.pop()
        if command.redo():
            self._undo_stack.append(command)
            return command
        else:
            # Redo failed, put it back
            self._redo_stack.append(command)
            return None
    
    def can_undo(self) -> bool:
        """Check if undo is available."""
        return len(self._undo_stack) > 0
    
    def can_redo(self) -> bool:
        """Check if redo is available."""
        return len(self._redo_stack) > 0
    
    def get_undo_description(self) -> Optional[str]:
        """Get description of the next undo action."""
        if self._undo_stack:
            return self._undo_stack[-1].description
        return None
    
    def get_redo_description(self) -> Optional[str]:
        """Get description of the next redo action."""
        if self._redo_stack:
            return self._redo_stack[-1].description
        return None
    
    def clear(self):
        """Clear both undo and redo stacks."""
        self._undo_stack.clear()
        self._redo_stack.clear()
