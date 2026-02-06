"""Dialog components for LoanMaster."""
import re
from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton, 
                             QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, 
                             QFileDialog, QGroupBox)
from PyQt6.QtCore import Qt


class IndividualDialog(QDialog):
    """Dialog for adding/editing individual details with input validation."""
    
    def __init__(self, parent=None, name="", phone="", email=""):
        super().__init__(parent)
        self.setWindowTitle("Individual Details")
        self.setMinimumWidth(350)
        self.layout = QFormLayout(self)
        
        # Name input with validation
        self.name_input = QLineEdit(name)
        self.name_input.setPlaceholderText("Enter full name")
        self.name_error = QLabel()
        self.name_error.setStyleSheet("color: #dc3545; font-size: 11px;")
        self.name_error.setWordWrap(True)
        self.name_error.hide()
        
        name_layout = QVBoxLayout()
        name_layout.setSpacing(2)
        name_layout.addWidget(self.name_input)
        name_layout.addWidget(self.name_error)
        self.layout.addRow("Name:", name_layout)
        
        # Phone input with validation
        self.phone_input = QLineEdit(phone)
        self.phone_input.setPlaceholderText("e.g., +254 712 345678")
        self.phone_error = QLabel()
        self.phone_error.setStyleSheet("color: #dc3545; font-size: 11px;")
        self.phone_error.setWordWrap(True)
        self.phone_error.hide()
        
        phone_layout = QVBoxLayout()
        phone_layout.setSpacing(2)
        phone_layout.addWidget(self.phone_input)
        phone_layout.addWidget(self.phone_error)
        self.layout.addRow("Phone:", phone_layout)
        
        # Email input with validation
        self.email_input = QLineEdit(email)
        self.email_input.setPlaceholderText("e.g., name@example.com")
        self.email_error = QLabel()
        self.email_error.setStyleSheet("color: #dc3545; font-size: 11px;")
        self.email_error.setWordWrap(True)
        self.email_error.hide()
        
        email_layout = QVBoxLayout()
        email_layout.setSpacing(2)
        email_layout.addWidget(self.email_input)
        email_layout.addWidget(self.email_error)
        self.layout.addRow("Email:", email_layout)
        
        # Real-time validation on text change
        self.name_input.textChanged.connect(self.validate_name)
        self.phone_input.textChanged.connect(self.validate_phone)
        self.email_input.textChanged.connect(self.validate_email)
        
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self.validate_and_accept)
        self.layout.addRow(self.save_btn)
    
    def validate_name(self):
        """Validate name field: required, max 100 characters."""
        name = self.name_input.text().strip()
        if not name:
            self.name_error.setText("Name is required")
            self.name_error.show()
            self.name_input.setStyleSheet("border: 1px solid #dc3545;")
            return False
        if len(name) > 100:
            self.name_error.setText("Name too long (max 100 characters)")
            self.name_error.show()
            self.name_input.setStyleSheet("border: 1px solid #dc3545;")
            return False
        self.name_error.hide()
        self.name_input.setStyleSheet("")
        return True
    
    def validate_phone(self):
        """Validate phone field: optional, but if provided must be valid format."""
        phone = self.phone_input.text().strip()
        if phone and not re.match(r'^[\d\s\-\+\(\)]{7,20}$', phone):
            self.phone_error.setText("Invalid phone format (use digits, spaces, +, -, parentheses)")
            self.phone_error.show()
            self.phone_input.setStyleSheet("border: 1px solid #dc3545;")
            return False
        self.phone_error.hide()
        self.phone_input.setStyleSheet("")
        return True
    
    def validate_email(self):
        """Validate email field: optional, but if provided must be valid format."""
        email = self.email_input.text().strip()
        if email and not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            self.email_error.setText("Invalid email format (e.g., name@example.com)")
            self.email_error.show()
            self.email_input.setStyleSheet("border: 1px solid #dc3545;")
            return False
        self.email_error.hide()
        self.email_input.setStyleSheet("")
        return True
    
    def validate_and_accept(self):
        """Validate all fields before accepting the dialog."""
        name_valid = self.validate_name()
        phone_valid = self.validate_phone()
        email_valid = self.validate_email()
        
        if all([name_valid, phone_valid, email_valid]):
            self.accept()

    def get_data(self):
        return self.name_input.text().strip(), self.phone_input.text().strip(), self.email_input.text().strip()


class ImportDialog(QDialog):
    """Dialog for selecting detailed import options."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Database")
        self.resize(400, 300)
        
        self.layout = QVBoxLayout(self)
        
        # File Selection
        file_group = QGroupBox("Source Database")
        file_layout = QVBoxLayout()
        
        self.file_label = QLabel("No file selected")
        self.file_label.setStyleSheet("color: gray; font-style: italic;")
        self.select_btn = QPushButton("Select Database File...")
        self.select_btn.clicked.connect(self.select_file)
        
        file_layout.addWidget(self.file_label)
        file_layout.addWidget(self.select_btn)
        file_group.setLayout(file_layout)
        self.layout.addWidget(file_group)
        
        # Options
        opts_group = QGroupBox("Import Options")
        opts_layout = QVBoxLayout()
        
        self.chk_individuals = QCheckBox("Import Individuals (Core Data)")
        self.chk_individuals.setChecked(True)
        self.chk_individuals.setEnabled(False) # Always required
        
        self.chk_loans = QCheckBox("Import Loans & Ledger History")
        self.chk_loans.setChecked(False)
        
        self.chk_savings = QCheckBox("Import Savings History")
        self.chk_savings.setChecked(False)
        
        opts_layout.addWidget(self.chk_individuals)
        opts_layout.addWidget(self.chk_loans)
        opts_layout.addWidget(self.chk_savings)
        opts_group.setLayout(opts_layout)
        self.layout.addWidget(opts_group)
        
        # Info Label
        self.info_lbl = QLabel("Duplicates will be matched by Name.\nNew IDs will be generated to prevent conflicts.")
        self.info_lbl.setStyleSheet("color: #666; font-size: 11px;")
        self.info_lbl.setWordWrap(True)
        self.layout.addWidget(self.info_lbl)
        
        self.layout.addStretch()
        
        # Buttons
        btn_layout = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        self.import_btn = QPushButton("Start Import")
        self.import_btn.clicked.connect(self.accept)
        self.import_btn.setEnabled(False) # Enable only when file selected
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.import_btn)
        self.layout.addLayout(btn_layout)
        
        self.selected_file_path = None

    def select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Source Database", 
            "", 
            "SQLite Database (*.db);;All Files (*)"
        )
        if file_path:
            self.selected_file_path = file_path
            self.file_label.setText(file_path)
            self.file_label.setStyleSheet("color: black;")
            self.import_btn.setEnabled(True)

    def get_data(self):
        return {
            "file_path": self.selected_file_path,
            "import_loans": self.chk_loans.isChecked(),
            "import_savings": self.chk_savings.isChecked()
        }


from .data_structures import StatementConfig
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QDateEdit, QDialogButtonBox
from PyQt6.QtCore import QDate

class StatementConfigDialog(QDialog):
    """Dialog for configuring statement generation options."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate Statements")
        self.setMinimumWidth(400)
        self.layout = QVBoxLayout(self)
        
        # 1. Date Range Section
        date_group = QGroupBox("Period")
        date_layout = QFormLayout()
        
        self.from_date = QDateEdit()
        self.from_date.setCalendarPopup(True)
        self.from_date.setDate(QDate.currentDate().addMonths(-1)) # Default 1 month back
        
        self.to_date = QDateEdit()
        self.to_date.setCalendarPopup(True)
        self.to_date.setDate(QDate.currentDate())
        
        date_layout.addRow("From:", self.from_date)
        date_layout.addRow("To:", self.to_date)
        date_group.setLayout(date_layout)
        self.layout.addWidget(date_group)
        
        # 2. Options Section
        opts_group = QGroupBox("Content Options")
        opts_layout = QVBoxLayout()
        
        self.chk_savings = QCheckBox("Include Savings Section")
        self.chk_savings.setChecked(True)
        
        self.chk_gross = QCheckBox("Show Gross Balance Column")
        self.chk_gross.setChecked(True)
        self.chk_gross.setToolTip("Show the running gross balance (Principal + Interest) if available")
        
        self.chk_notes = QCheckBox("Show Notes Column")
        self.chk_notes.setChecked(True)
        
        opts_layout.addWidget(self.chk_savings)
        opts_layout.addWidget(self.chk_gross)
        opts_layout.addWidget(self.chk_notes)
        opts_group.setLayout(opts_layout)
        self.layout.addWidget(opts_group)
        
        # 3. Column Selection (Advanced)
        # For now, let's keep it simple: Just checkboxes for optional columns?
        # The user asked for "specify which column...".
        # Let's add a list for columns to be explicit.
        
        col_group = QGroupBox("Visible Columns")
        col_layout = QVBoxLayout()
        self.col_list = QListWidget()
        self.col_list.setFixedHeight(120)
        
        # Default columns from StatementConfig
        # We need to manually define them here to allow re-ordering or selection
        # "Date", "Type", "Debit", "Interest", "Credit", "Balance", "Gross", "Notes"
        # "Gross" and "Notes" are controlled by checkboxes above? Or list? 
        # Overlap. Let's make the checkboxes control the *presence* of key optional columns,
        # and the list control the *entire* set if they want fine-grained control.
        # Actually, simplifies to just use the list.
        
        all_cols = ["Date", "Type", "Debit", "Interest", "Credit", "Balance", "Gross", "Notes"]
        for col in all_cols:
            item = QListWidgetItem(col)
            item.setCheckState(Qt.CheckState.Checked)
            self.col_list.addItem(item)
            
        col_layout.addWidget(self.col_list)
        col_group.setLayout(col_layout)
        
        # Sync logic: Unchecking "Show Gross" unchecks "Gross" in list, etc.
        self.chk_gross.stateChanged.connect(lambda s: self.set_col_state("Gross", s))
        self.chk_notes.stateChanged.connect(lambda s: self.set_col_state("Notes", s))
        
        # Also list change should update checkbox? Optional.
        
        self.layout.addWidget(col_group)
        
        # Buttons
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.layout.addWidget(btns)
        
    def set_col_state(self, col_name, state):
        for i in range(self.col_list.count()):
            item = self.col_list.item(i)
            if item.text() == col_name:
                item.setCheckState(Qt.CheckState.Checked if state else Qt.CheckState.Unchecked)
                
    def get_config(self):
        """Return (from_date, to_date, StatementConfig)."""
        f_date = self.from_date.date()
        t_date = self.to_date.date()
        
        # Get columns
        cols = []
        for i in range(self.col_list.count()):
            item = self.col_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                cols.append(item.text())
        
        config = StatementConfig(
            show_savings=self.chk_savings.isChecked(),
            show_gross_balance=self.chk_gross.isChecked(),
            show_notes=self.chk_notes.isChecked(),
            columns=cols
        )
        
        return f_date, t_date, config
