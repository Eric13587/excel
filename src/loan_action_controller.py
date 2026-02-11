"""Loan Action Controller for LoanMaster.

This module provides a controller for loan-related actions in the Dashboard,
extracted for better separation of concerns.
"""
from datetime import datetime
from typing import Callable, Optional, Tuple

from PyQt6.QtWidgets import QMessageBox, QDialog, QFileDialog


class LoanActionController:
    """Controller for loan-related actions in the Dashboard.
    
    Handles button actions and dialogs for individual/loan operations,
    delegating to the appropriate services and managers.
    
    Attributes:
        db_manager: DatabaseManager for data access.
        ui_state: UIStateManager for selection state.
        on_refresh: Callback to refresh the UI after changes.
    """
    
    def __init__(self, db_manager, ui_state, parent_widget, 
                 on_refresh: Callable = None,
                 on_open_ledger: Callable[[int, str], None] = None):
        """Initialize LoanActionController.
        
        Args:
            db_manager: DatabaseManager instance.
            ui_state: UIStateManager for selection state.
            parent_widget: Parent widget for dialogs.
            on_refresh: Callback to refresh the individual list.
            on_open_ledger: Callback to open ledger view for an individual.
        """
        self.db = db_manager
        self.ui_state = ui_state
        self.parent = parent_widget
        self.on_refresh = on_refresh
        self.on_open_ledger = on_open_ledger
    
    def _require_selection(self) -> bool:
        """Check if there is a selection, showing warning if not.
        
        Returns:
            True if there is a selection, False otherwise.
        """
        if not self.ui_state.has_selection():
            QMessageBox.warning(self.parent, "No Selection", 
                              "Please select an individual first.")
            return False
        return True
    
    def add_individual(self, dialog_class) -> bool:
        """Add a new individual.
        
        Args:
            dialog_class: Dialog class to use for input (IndividualDialog).
            
        Returns:
            True if individual was added, False otherwise.
        """
        dialog = dialog_class(self.parent)
        if dialog.exec():
            name, phone, email = dialog.get_data()
            if name:
                # Efficient SQL duplicate check
                if self.db.individual_name_exists(name):
                    confirm = QMessageBox.question(
                        self.parent, "Duplicate Name",
                        f"The name '{name}' already exists. Add anyway?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    )
                    if confirm == QMessageBox.StandardButton.No:
                        return False

                new_id = self.db.add_individual(name, phone, email)
                if self.on_refresh:
                    self.on_refresh(select_id=new_id)
                
                QMessageBox.information(
                    self.parent, "Success",
                    f"'{name}' has been added successfully."
                )
                return True
        return False
    
    def edit_individual(self, dialog_class) -> bool:
        """Edit the selected individual.
        
        Args:
            dialog_class: Dialog class to use for input (IndividualDialog).
            
        Returns:
            True if individual was edited, False otherwise.
        """
        if not self._require_selection():
            return False
        
        ind_id = self.ui_state.get_selected_id()
        card = self.ui_state.selected_card
        
        dialog = dialog_class(
            self.parent, card.name, card.phone, 
            getattr(card, 'email', ''),
            mode="edit"
        )
        if dialog.exec():
            name, phone, email = dialog.get_data()
            if name:
                self.db.update_individual(ind_id, name, phone, email)
                if self.on_refresh:
                    self.on_refresh(select_id=ind_id)
                return True
        return False
    
    def delete_individual(self) -> bool:
        """Delete the selected individual.
        
        Returns:
            True if individual was deleted, False otherwise.
        """
        if not self._require_selection():
            return False
        
        ind_id = self.ui_state.get_selected_id()
        name = self.ui_state.get_selected_name()
        
        confirm = QMessageBox.question(
            self.parent, "Delete Individual",
            f"Are you sure you want to delete '{name}' and all their records?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if confirm == QMessageBox.StandardButton.Yes:
            self.db.delete_individual(ind_id)
            self.ui_state.clear_selection()
            if self.on_refresh:
                self.on_refresh()
            return True
        return False
    
    def open_ledger(self) -> bool:
        """Open the ledger for the selected individual.
        
        Returns:
            True if ledger was opened, False otherwise.
        """
        if not self._require_selection():
            return False
        
        ind_id = self.ui_state.get_selected_id()
        name = self.ui_state.get_selected_name()
        
        if self.on_open_ledger:
            self.on_open_ledger(ind_id, name)
            return True
        return False
    
    def get_date_range_dialog(self, title: str = "Select Date Range") -> Optional[Tuple[str, str]]:
        """Show a dialog to select a date range.
        
        Args:
            title: Dialog title.
            
        Returns:
            Tuple of (from_date, to_date) in YYYY-MM-DD format, or None if cancelled.
        """
        from PyQt6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, 
                                     QDateEdit, QPushButton, QSpacerItem, 
                                     QSizePolicy)
        from PyQt6.QtCore import QDate
        
        dialog = QDialog(self.parent)
        dialog.setWindowTitle(title)
        dialog.setMinimumWidth(350)
        
        layout = QVBoxLayout(dialog)
        
        # From date
        from_layout = QHBoxLayout()
        from_layout.addWidget(QLabel("From:"))
        from_date_edit = QDateEdit()
        from_date_edit.setCalendarPopup(True)
        from_date_edit.setDate(QDate.currentDate().addMonths(-12))
        from_layout.addWidget(from_date_edit)
        layout.addLayout(from_layout)
        
        # To date
        to_layout = QHBoxLayout()
        to_layout.addWidget(QLabel("To:"))
        to_date_edit = QDateEdit()
        to_date_edit.setCalendarPopup(True)
        to_date_edit.setDate(QDate.currentDate())
        to_layout.addWidget(to_date_edit)
        layout.addLayout(to_layout)
        
        # Spacer
        layout.addSpacerItem(QSpacerItem(0, 10, QSizePolicy.Policy.Minimum, 
                                         QSizePolicy.Policy.Expanding))
        
        # Buttons
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            from_date = from_date_edit.date().toString("yyyy-MM-dd")
            to_date = to_date_edit.date().toString("yyyy-MM-dd")
            return (from_date, to_date)
        
        return None
    
    def select_output_folder(self, title: str = "Select Output Folder") -> Optional[str]:
        """Show a folder selection dialog.
        
        Args:
            title: Dialog title.
            
        Returns:
            Selected folder path, or None if cancelled.
        """
        folder = QFileDialog.getExistingDirectory(self.parent, title)
        return folder if folder else None
