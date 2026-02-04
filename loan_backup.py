import sys
import pandas as pd
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, 
                             QTableWidget, QTableWidgetItem, QFileDialog, QMessageBox, QInputDialog)
from PyQt6.QtCore import Qt

class LoanEngine:
    """Handles the mathematical logic and data storage."""
    def __init__(self):
        self.file_path = "loan_data.csv"
        try:
            self.df = pd.read_csv(self.file_path)
        except FileNotFoundError:
            self.df = pd.DataFrame(columns=[
                "Date", "Event Type", "Loan ID", "Added", "Deducted", "Balance", "Notes"
            ])

    def add_loan_event(self, principal, duration, date_str):
        # 1. Logic: 15% Flat Interest
        interest = principal * 0.15
        total_loan = principal + interest
        
        # 2. Get current balance
        current_balance = self.df["Balance"].iloc[-1] if not self.df.empty else 0.0
        new_balance = current_balance + total_loan
        
        # 3. Create Entry
        loan_id = f"L-{len(self.df[self.df['Event Type'] == 'Loan Issued']) + 1:03d}"
        new_row = {
            "Date": date_str,
            "Event Type": "Loan Issued",
            "Loan ID": loan_id,
            "Added": total_loan,
            "Deducted": 0,
            "Balance": new_balance,
            "Notes": f"Principal: {principal}, Interest: {interest}"
        }
        
        new_df = pd.DataFrame([new_row])
        if self.df.empty:
            self.df = new_df
        else:
            self.df = pd.concat([self.df, new_df], ignore_index=True)
        self.save_data()
        return new_balance / duration # Return recalculated monthly deduction

    def add_repayment_event(self, amount, date_str):
        current_balance = self.df["Balance"].iloc[-1] if not self.df.empty else 0.0
        new_balance = max(0, current_balance - amount)
        
        new_row = {
            "Date": date_str,
            "Event Type": "Repayment",
            "Loan ID": "-",
            "Added": 0,
            "Deducted": amount,
            "Balance": new_balance,
            "Notes": "Monthly Deduction"
        }
        new_df = pd.DataFrame([new_row])
        if self.df.empty:
            self.df = new_df
        else:
            self.df = pd.concat([self.df, new_df], ignore_index=True)
        self.save_data()

    def save_data(self):
        self.df.to_csv(self.file_path, index=False)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.engine = LoanEngine()
        self.setWindowTitle("LoanMaster Ledger")
        self.resize(1000, 600)

        # Main Layout
        main_widget = QWidget()
        self.layout = QHBoxLayout(main_widget)
        
        # Left Panel: Entry Form
        self.init_sidebar()
        
        # Right Panel: Table View
        self.init_table()
        
        self.setCentralWidget(main_widget)
        self.refresh_table()

    def init_sidebar(self):
        sidebar = QVBoxLayout()
        
        sidebar.addWidget(QLabel("<b>Add New Loan</b>"))
        self.amount_input = QLineEdit(placeholderText="Principal Amount")
        self.duration_input = QLineEdit(placeholderText="Duration (Months)")
        self.date_input = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        
        sidebar.addWidget(QLabel("Principal:"))
        sidebar.addWidget(self.amount_input)
        sidebar.addWidget(QLabel("Duration (Months):"))
        sidebar.addWidget(self.duration_input)
        sidebar.addWidget(QLabel("Date (YYYY-MM-DD):"))
        sidebar.addWidget(self.date_input)
        
        loan_btn = QPushButton("Issue Loan")
        loan_btn.clicked.connect(self.process_loan)
        sidebar.addWidget(loan_btn)
        
        sidebar.addSpacing(20)
        sidebar.addWidget(QLabel("<b>Actions</b>"))
        
        repay_btn = QPushButton("Record Monthly Deduction")
        repay_btn.clicked.connect(self.process_repayment)
        sidebar.addWidget(repay_btn)
        
        export_btn = QPushButton("Export to Excel")
        export_btn.clicked.connect(self.export_excel)
        sidebar.addWidget(export_btn)
        
        sidebar.addStretch()
        self.layout.addLayout(sidebar, 1)

    def init_table(self):
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels([
            "Date", "Event", "ID", "Added", "Deducted", "Balance", "Notes"
        ])
        self.layout.addWidget(self.table, 3)

    def refresh_table(self):
        self.table.setRowCount(len(self.engine.df))
        for i, row in self.engine.df.iterrows():
            for j, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                self.table.setItem(i, j, item)

    def process_loan(self):
        try:
            p = float(self.amount_input.text())
            d = int(self.duration_input.text())
            dt = self.date_input.text()
            deduction = self.engine.add_loan_event(p, d, dt)
            self.refresh_table()
            QMessageBox.information(self, "Success", f"New Monthly Deduction: {deduction:.2f}")
        except ValueError:
            QMessageBox.critical(self, "Error", "Please enter valid numbers.")

    def process_repayment(self):
        if self.engine.df.empty or self.engine.df["Balance"].iloc[-1] <= 0:
            return
        
        # For simplicity, we grab the last suggested deduction or ask user
        # In a full system, you'd track the 'current_deduction' variable
        amount, ok = QInputDialog.getText(self, "Repayment", "Enter Deduction Amount:")
        if ok:
            self.engine.add_repayment_event(float(amount), datetime.now().strftime("%Y-%m-%d"))
            self.refresh_table()

    def export_excel(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save File", "", "Excel Files (*.xlsx)")
        if path:
            if not path.endswith('.xlsx'):
                path += '.xlsx'
            self.engine.df.to_excel(path, index=False)
            QMessageBox.information(self, "Export", "Statement exported successfully!")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
