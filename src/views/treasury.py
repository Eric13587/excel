from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QTableWidget, QTableWidgetItem, QHeaderView, 
                             QMessageBox, QLabel, QGroupBox, QFormLayout, 
                             QComboBox, QLineEdit, QDateEdit, QDoubleSpinBox)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor

class TreasuryDialog(QDialog):
    """Dialog for Treasury and General Ledger management."""
    
    TYPES = ["Asset", "Liability/Equity", "Expense", "Income"]
    
    CATEGORIES = [
        "Initial Bank Capital",
        "Bank Deposit",
        "Bank Withdrawal",
        "Office Rent",
        "Staff Salaries",
        "Stationery / Supplies",
        "Utility Bills",
        "Bank Fees / Charges",
        "Marketing / PR",
        "Other Expenses",
        "Fines & Fees (Income)",
        "Bank Interest Earned",
        "Other Income",
        "External Loan Received",
        "External Loan Repayment"
    ]
    
    def __init__(self, db_manager, theme_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self.theme_manager = theme_manager
        
        self.setWindowTitle("Treasury & General Ledger")
        self.resize(1000, 600)
        self.init_ui()
        self.load_data()
        self.apply_theme()
        
    def init_ui(self):
        self.layout = QVBoxLayout(self)
        
        # Summary Header
        self.summary_group = QGroupBox("Cash & Bank Summary Snapshot")
        summary_layout = QHBoxLayout()
        summary_layout.setContentsMargins(15, 15, 15, 15)
        self.cash_balance_label = QLabel("Calculated Bank Cash: Loading...")
        self.cash_balance_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        summary_layout.addWidget(self.cash_balance_label)
        self.summary_group.setLayout(summary_layout)
        self.layout.addWidget(self.summary_group)
        
        # Main Split
        main_layout = QHBoxLayout()
        
        # Left Panel: Add Entry Form
        form_group = QGroupBox("Log New Entry")
        form_layout = QFormLayout()
        
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDate(QDate.currentDate())
        
        self.type_input = QComboBox()
        self.type_input.addItems(self.TYPES)
        
        self.category_input = QComboBox()
        self.category_input.setEditable(True)
        self.category_input.addItems(self.CATEGORIES)
        
        self.amount_input = QDoubleSpinBox()
        self.amount_input.setRange(0.01, 1000000000.0)
        self.amount_input.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        
        self.notes_input = QLineEdit()
        
        self.btn_add = QPushButton("Record Entry")
        self.btn_add.clicked.connect(self.add_entry)
        
        form_layout.addRow("Date:", self.date_input)
        form_layout.addRow("Category:", self.category_input)
        form_layout.addRow("Type:", self.type_input)
        form_layout.addRow("Amount:", self.amount_input)
        form_layout.addRow("Notes:", self.notes_input)
        form_layout.addRow("", self.btn_add)
        
        form_group.setLayout(form_layout)
        form_group.setFixedWidth(350)
        main_layout.addWidget(form_group)
        
        # Right Panel: Ledger Table
        table_group = QGroupBox("General Ledger History")
        table_layout = QVBoxLayout()
        
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["ID", "Date", "Category", "Type", "Amount", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        
        table_layout.addWidget(self.table)
        
        # Table actions
        t_actions_layout = QHBoxLayout()
        self.btn_delete = QPushButton("Delete Selected Entry")
        self.btn_delete.clicked.connect(self.delete_entry)
        t_actions_layout.addStretch()
        t_actions_layout.addWidget(self.btn_delete)
        table_layout.addLayout(t_actions_layout)
        
        table_group.setLayout(table_layout)
        main_layout.addWidget(table_group)
        
        self.layout.addLayout(main_layout)
        
        # Make type match category automatically as a helper
        self.category_input.currentTextChanged.connect(self.auto_match_type)
        
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        
        self.btn_report = QPushButton("Generate Balance Sheet & P&L")
        self.btn_report.setStyleSheet("background-color: #10b981; color: white;")
        self.btn_report.clicked.connect(self.generate_reports)
        
        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.btn_report)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_close)
        self.layout.addLayout(btn_layout)

    def apply_theme(self):
        t = self.theme_manager
        self.setStyleSheet(f"""
            QDialog {{ background-color: {t.get_color('bg_primary')}; color: {t.get_color('text_primary')}; }}
            QGroupBox {{ border: 1px solid {t.get_color('border')}; margin-top: 10px; border-radius: 5px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {t.get_color('text_secondary')}; }}
            QPushButton {{ background-color: {t.get_color('accent')}; color: white; padding: 8px 15px; border-radius: 5px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {t.get_color('accent_hover')}; }}
            QTableWidget {{ background-color: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; gridline-color: {t.get_color('border')}; }}
            QHeaderView::section {{ background-color: {t.get_color('bg_header')}; color: {t.get_color('text_secondary')}; font-weight: bold; padding: 4px; border: none; border-right: 1px solid {t.get_color('border')}; }}
            QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox {{ background-color: {t.get_color('input_bg')}; border: 1px solid {t.get_color('border')}; padding: 6px; border-radius: 4px; color: {t.get_color('text_primary')}; }}
        """)
        self.btn_delete.setStyleSheet(f"background-color: {t.get_color('danger_bg')}; color: {t.get_color('danger')}; padding: 6px 15px; border-radius: 4px;")

    def auto_match_type(self, category):
        cat = category.lower()
        if "expense" in cat or "rent" in cat or "salar" in cat or "utilit" in cat or "suppl" in cat:
            self.type_input.setCurrentText("Expense")
        elif "income" in cat or "fine" in cat or "earned" in cat:
            self.type_input.setCurrentText("Income")
        elif "deposit" in cat or "asset" in cat or "capital" in cat:
            self.type_input.setCurrentText("Asset")
        elif "withdrawal" in cat or "loan received" in cat:
            self.type_input.setCurrentText("Liability/Equity")

    def load_data(self):
        self.table.setRowCount(0)
        entries = self.db.get_gl_entries()
        
        bank_cash = 0.0
        
        for r, entry in enumerate(entries):
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(entry['id'])))
            self.table.setItem(r, 1, QTableWidgetItem(entry['date']))
            self.table.setItem(r, 2, QTableWidgetItem(entry['category']))
            
            t_item = QTableWidgetItem(entry['type'])
            if entry['type'] == 'Expense' or entry['type'] == 'Liability/Equity':
                t_item.setForeground(QColor("#ef4444"))
            else:
                t_item.setForeground(QColor("#10b981"))
            self.table.setItem(r, 3, t_item)
            
            a_item = QTableWidgetItem(f"{entry['amount']:,.2f}")
            a_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(r, 4, a_item)
            
            self.table.setItem(r, 5, QTableWidgetItem(entry['notes'] or ""))
            
            # Simple cash approximation loop for display only
            if entry['type'] in ['Income', 'Asset']:
                bank_cash += entry['amount']
            else:
                bank_cash -= entry['amount']
                
        # To get true bank cash, we would also need (Member Deposits + Repayments) - Member Loans Issued.
        # But for now we just show a static visual or a fully calculated dynamic value from DB.
        self.update_bank_cash_visual()
        
    def update_bank_cash_visual(self):
        # Calculate true bank cash
        # Cash = GL Assets + GL Income - GL Expenses 
        #        + Member Savings Deposits - Member Savings Withdrawals
        #        + Member Loan Principal Repaid + Member Interest Repaid
        #        - Member Loans Disbursed
        
        # For performance, this complex sum is better done in DB or via pandas. Let's do a quick DB sum.
        # However, for simplicity here, we'll leave it as "GL Cash" or calculate fully.
        
        # Let's write the query inline for precision.
        cursor = self.db.conn.cursor()
        
        # 1. Member Ledger (Loans)
        cursor.execute("SELECT SUM(principal_portion), SUM(interest_portion) FROM ledger WHERE event_type='Repayment'")
        rep_p, rep_i = cursor.fetchone()
        rep_p = rep_p or 0
        rep_i = rep_i or 0
        
        cursor.execute("SELECT SUM(added) FROM ledger WHERE event_type LIKE 'Loan Issued%' OR event_type='Loan Top-Up'")
        loans_issued = cursor.fetchone()[0] or 0
        
        # 2. Member Savings
        cursor.execute("SELECT SUM(amount) FROM savings WHERE transaction_type='Deposit'")
        sav_dep = cursor.fetchone()[0] or 0
        cursor.execute("SELECT SUM(amount) FROM savings WHERE transaction_type='Withdrawal'")
        sav_wd = cursor.fetchone()[0] or 0
        
        # 3. General Ledger
        cursor.execute("SELECT SUM(amount) FROM general_ledger WHERE type IN ('Asset', 'Income')")
        gl_in = cursor.fetchone()[0] or 0
        cursor.execute("SELECT SUM(amount) FROM general_ledger WHERE type IN ('Expense', 'Liability/Equity')")
        gl_out = cursor.fetchone()[0] or 0
        
        # Equation
        cash = (rep_p + rep_i + sav_dep + gl_in) - (loans_issued + sav_wd + gl_out)
        
        # Alternatively, if they log cash deposits implicitly, we just show GL balances. 
        # But integrating it provides a true "Cash Flow" insight!
        
        self.cash_balance_label.setText(f"Calculated Bank Cash: {cash:,.2f}")
        
    def add_entry(self):
        amount = self.amount_input.value()
        if amount <= 0:
            QMessageBox.warning(self, "Invalid", "Amount must be greater than 0.")
            return
            
        date_str = self.date_input.date().toString("yyyy-MM-dd")
        cat = self.category_input.currentText().strip()
        typ = self.type_input.currentText().strip()
        notes = self.notes_input.text().strip()
        
        if not cat:
            QMessageBox.warning(self, "Invalid", "Category cannot be empty.")
            return
            
        self.db.add_gl_entry(date_str, cat, typ, amount, notes)
        
        # Reset form
        self.amount_input.setValue(0.00)
        self.notes_input.clear()
        
        self.load_data()
        
    def delete_entry(self):
        selected = self.table.selectedItems()
        if not selected:
            return
            
        row = selected[0].row()
        entry_id = int(self.table.item(row, 0).text())
        cat = self.table.item(row, 2).text()
        
        reply = QMessageBox.question(self, "Confirm", f"Are you sure you want to delete general ledger entry: '{cat}'?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                                     
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_gl_entry(entry_id)
            self.load_data()

    def generate_reports(self):
        from PyQt6.QtWidgets import QFileDialog
        from src.reports import ReportGenerator
        from datetime import datetime
        import platform
        import subprocess
        import os
        
        path, _ = QFileDialog.getSaveFileName(self, "Save Institutional Reports", f"SACCO_Financials_{datetime.now().strftime('%Y%m%d')}.pdf", "PDF Files (*.pdf)")
        if not path: return
        
        # We need a printer_view_getter to get QWebEngineView for PDF generation
        main_dash = self.parent() if hasattr(self, 'parent') and hasattr(self.parent(), 'get_printer_view') else None
        
        if main_dash:
            getter = getattr(main_dash, 'get_printer_view', None)
        else:
            getter = None
        
        if not getter:
            QMessageBox.critical(self, "Error", "Cannot initialize PDF printer engine.")
            return
            
        generator = ReportGenerator(self.db, printer_view_getter=getter)
        
        target_date = self.date_input.date().toString("yyyy-MM-dd") # Generate up to selected date? Or just today? Let's use today.
        
        success, msg = generator.generate_financial_statements(path, target_date_str=datetime.now().strftime("%Y-%m-%d"))
        
        if success:
            QMessageBox.information(self, "Success", msg + f"\nSaved to: {path}")
            # Try to open it
            if platform.system() == 'Windows':
                os.startfile(path)
            elif platform.system() == 'Linux':
                subprocess.Popen(['xdg-open', path])
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', path])
        else:
            QMessageBox.critical(self, "Error", msg)

