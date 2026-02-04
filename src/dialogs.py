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
