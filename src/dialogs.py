import json
from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton, 
                             QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, 
                             QFileDialog, QGroupBox, QListWidget, QListWidgetItem, QDateEdit, QDialogButtonBox,
                             QTableWidget, QTableWidgetItem, QComboBox, QHeaderView)
from PyQt6.QtCore import Qt, QDate
from .data_structures import StatementConfig
import re


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
        
        # Date Filter
        self.chk_date_filter = QCheckBox("Filter Transactions by Date")
        self.chk_date_filter.stateChanged.connect(self.toggle_date_inputs)
        
        date_layout = QHBoxLayout()
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDate(QDate.currentDate().addMonths(-1))
        self.start_date.setEnabled(False)
        
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(QDate.currentDate())
        self.end_date.setEnabled(False)
        
        date_layout.addWidget(QLabel("Start:"))
        date_layout.addWidget(self.start_date)
        date_layout.addWidget(QLabel("End:"))
        date_layout.addWidget(self.end_date)
        
        opts_layout.addWidget(self.chk_date_filter)
        opts_layout.addLayout(date_layout)
        
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

    def toggle_date_inputs(self, state):
        enabled = (state == 2) # Checked
        self.start_date.setEnabled(enabled)
        self.end_date.setEnabled(enabled)

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
        date_range = None
        if self.chk_date_filter.isChecked():
            start = self.start_date.date().toString("yyyy-MM-dd")
            end = self.end_date.date().toString("yyyy-MM-dd")
            date_range = (start, end)
            
        return {
            "file_path": self.selected_file_path,
            "import_loans": self.chk_loans.isChecked(),
            "import_savings": self.chk_savings.isChecked(),
            "date_range": date_range
        }


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
        col_group = QGroupBox("Visible Columns")
        col_layout = QVBoxLayout()
        self.col_list = QListWidget()
        self.col_list.setFixedHeight(120)
        
        all_cols = ["Date", "Type", "Debit", "Interest", "Credit", "Balance", "Gross", "Notes"]
        for col in all_cols:
            item = QListWidgetItem(col)
            item.setCheckState(Qt.CheckState.Checked)
            self.col_list.addItem(item)
            
        col_layout.addWidget(self.col_list)
        col_group.setLayout(col_layout)
        
        # Sync logic
        self.chk_gross.stateChanged.connect(lambda s: self.set_col_state("Gross", s))
        self.chk_notes.stateChanged.connect(lambda s: self.set_col_state("Notes", s))
        
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


class DuplicateResolutionDialog(QDialog):
    """Dialog to resolve duplicates during import."""
    
    def __init__(self, conflicts, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Potential Duplicates")
        self.resize(700, 500)
        self.conflicts = conflicts
        self.decision_map = {}
        
        layout = QVBoxLayout(self)
        
        info = QLabel(f"Found {len(conflicts)} potential duplicates. Please select an action for each.")
        layout.addWidget(info)
        
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Source Individual", "Matched Against", "Reason", "Action"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setRowCount(len(conflicts))
        
        for row, conflict in enumerate(conflicts):
            src = conflict['src']
            matches = conflict['matches']
            
            # Source Info
            src_text = f"{src['name']}\n(ID: {src['id']})"
            self.table.setItem(row, 0, QTableWidgetItem(src_text))
            
            # Match Info (Multi-line if multiple matches)
            match_texts = []
            reasons = []
            for m in matches:
                match_texts.append(f"{m['name']} (ID: {m['id']})")
                reasons.append(m.get('reason', 'Match'))
            
            self.table.setItem(row, 1, QTableWidgetItem("\n".join(match_texts)))
            self.table.setItem(row, 2, QTableWidgetItem("\n".join(reasons)))
            
            # Action Dropdown
            combo = QComboBox()
            combo.addItem("Create New (Keep Both)", "new")
            combo.addItem("Skip (Do Not Import)", "skip")
            
            # Add merge options for each match
            for m in matches:
                combo.addItem(f"Merge with {m['name']} (ID: {m['id']})", m['id'])
                
            # Default to Merge if exactly 1 match
            if len(matches) == 1:
                combo.setCurrentIndex(2) # 0=New, 1=Skip, 2=Merge
            
            combo.setProperty("src_id", src['id'])
            self.table.setCellWidget(row, 3, combo)
            
        self.table.resizeRowsToContents()
        layout.addWidget(self.table)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
    def get_decisions(self):
        """Return a map of {src_id: action}."""
        decisions = {}
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 3)
            src_id = combo.property("src_id")
            action = combo.currentData()
            decisions[src_id] = action
        return decisions


class ImportHistoryDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Import History")
        self.resize(800, 500)
        self.layout = QVBoxLayout(self)
        
        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(["ID", "Date", "Source File", "Items", "Details"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.layout.addWidget(self.table)
        
        # Keys to display
        self.keys = ['id', 'timestamp', 'source_file', 'item_count', 'details']
        
        # Buttons
        btn_layout = QHBoxLayout()
        self.undo_btn = QPushButton("Undo Selected Import")
        self.undo_btn.clicked.connect(self.undo_import)
        self.undo_btn.setStyleSheet("background-color: #d9534f; color: white; padding: 5px;")
        
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        
        btn_layout.addStretch()
        btn_layout.addWidget(self.undo_btn)
        btn_layout.addWidget(close_btn)
        self.layout.addLayout(btn_layout)
        
        self.load_history()
        
    def load_history(self):
        self.table.setRowCount(0)
        history = self.db.get_import_history()
        self.table.setRowCount(len(history))
        for i, row in enumerate(history):
            for j, key in enumerate(self.keys):
                raw_val = row.get(key, "")
                display_val = str(raw_val)
                tooltip = display_val

                if key == 'details':
                    try:
                        # Try to parse JSON
                        data = json.loads(raw_val)
                        if isinstance(data, dict):
                            # Format as "Ind: 5 | Loans: 2 ..."
                            parts = []
                            if data.get('individuals', 0) > 0: parts.append(f"Ind: {data['individuals']}")
                            if data.get('loans', 0) > 0: parts.append(f"Loans: {data['loans']}")
                            if data.get('savings', 0) > 0: parts.append(f"Sav: {data['savings']}")
                            if data.get('ledger', 0) > 0: parts.append(f"Ledger: {data['ledger']}")
                            
                            display_val = " | ".join(parts) if parts else "No items imported"
                            tooltip = json.dumps(data, indent=2)
                    except (json.JSONDecodeError, TypeError):
                        # Fallback for old string format or invalid json
                        pass

                item = QTableWidgetItem(display_val)
                item.setToolTip(str(tooltip))
                item.setFlags(item.flags() ^ Qt.ItemFlag.ItemIsEditable) 
                self.table.setItem(i, j, item)
                
    def undo_import(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select an import to undo.")
            return
            
        row = selected_rows[0].row()
        import_id = int(self.table.item(row, 0).text())
        date = self.table.item(row, 1).text()
        source = self.table.item(row, 2).text()
        
        reply = QMessageBox.question(
            self, "Confirm Undo",
            f"Are you sure you want to UNDO the import from {date}?\n\n"
            f"Source: {source}\n\n"
            "This will delete all individuals, loans, and transactions created by this import.\n"
            "Merged data will be partially reverted (loans/savings deleted).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if self.db.undo_import(import_id):
                QMessageBox.information(self, "Success", "Import undone successfully.")
                self.load_history()
            else:
                QMessageBox.critical(self, "Error", "Failed to undo import. Check logs for details.")


class ImportPreviewDialog(QDialog):
    def __init__(self, preview_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Preview")
        self.resize(500, 400)
        self.preview = preview_data
        self.decision_map = {}
        
        self.layout = QVBoxLayout(self)
        
        # Summary Group
        grp = QGroupBox("Import Summary")
        form = QFormLayout()
        
        s = self.preview['summary']
        self.individuals_new_count = s['individuals_new']
        self.individuals_merged_count = s['individuals_merged']
        
        self.lbl_new = QLabel(str(self.individuals_new_count))
        self.lbl_merged = QLabel(str(self.individuals_merged_count))
        self.lbl_conflicts = QLabel(str(s['conflicts']))
        if s['conflicts'] > 0:
            self.lbl_conflicts.setStyleSheet("color: red; font-weight: bold;")
            
        form.addRow("New Individuals:", self.lbl_new)
        form.addRow("Merged Individuals:", self.lbl_merged)
        form.addRow("Potential Conflicts:", self.lbl_conflicts)
        form.addRow(QLabel("---"))
        form.addRow("Loans to Import:", QLabel(str(s['loans'])))
        
        # Loan Renames
        if s.get('loans_renamed', 0) > 0:
            lbl_renames = QLabel(str(s['loans_renamed']))
            lbl_renames.setStyleSheet("color: orange; font-weight: bold;")
            form.addRow("Loans to Rename:", lbl_renames)
            
        form.addRow("Ledger Entries:", QLabel(str(s['ledger'])))
        form.addRow("Savings Entries:", QLabel(str(s['savings'])))
        
        grp.setLayout(form)
        self.layout.addWidget(grp)
        
        # Conflict Resolution Area
        self.conflict_layout = QVBoxLayout()
        if s['conflicts'] > 0:
            self.res_btn = QPushButton(f"Resolve {s['conflicts']} Conflicts")
            self.res_btn.clicked.connect(self.resolve_conflicts)
            self.res_btn.setStyleSheet("background-color: #f0ad4e; color: white; font-weight: bold;")
            self.conflict_layout.addWidget(self.res_btn)
            
            self.conflict_lbl = QLabel("Conflicts must be resolved before importing.")
            self.conflict_lbl.setStyleSheet("color: #666; font-style: italic;")
            self.conflict_lbl.setWordWrap(True)
            self.conflict_layout.addWidget(self.conflict_lbl)
        else:
            self.conflict_layout.addWidget(QLabel("No conflicts detected. Ready to import."))
            
        self.layout.addLayout(self.conflict_layout)
        
        self.layout.addStretch()
        
        # Buttons
        btns = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        self.import_btn = QPushButton("Start Import")
        self.import_btn.clicked.connect(self.accept)
        # Disable import if conflicts exist and not resolved
        if s['conflicts'] > 0:
            self.import_btn.setEnabled(False)
            
        btns.addStretch()
        btns.addWidget(self.cancel_btn)
        btns.addWidget(self.import_btn)
        self.layout.addLayout(btns)
        
    def resolve_conflicts(self):
        dlg = DuplicateResolutionDialog(self.preview['conflicts'], self)
        if dlg.exec():
            self.decision_map = dlg.get_decisions()
            
            # Calculate impact of decisions
            new_add = 0
            merged_add = 0
            skipped = 0
            
            for action in self.decision_map.values():
                if action == 'new': 
                    new_add += 1
                elif action == 'skip': 
                    skipped += 1
                else: 
                    merged_add += 1
            
            # Update labels
            total_resolved = len(self.decision_map)
            self.lbl_conflicts.setText("Resolved")
            self.lbl_conflicts.setStyleSheet("color: green; font-weight: bold;")
            
            self.lbl_new.setText(f"{self.individuals_new_count} + {new_add} (from conflicts)")
            self.lbl_merged.setText(f"{self.individuals_merged_count} + {merged_add} (from conflicts)")
            
            self.conflict_lbl.setText(f"Decisions made: {new_add} New, {merged_add} Merged, {skipped} Skipped.")
            
            self.res_btn.setEnabled(False)
            self.res_btn.setText("Conflicts Resolved")
            self.import_btn.setEnabled(True)
            
    def get_decisions(self):
        return self.decision_map

