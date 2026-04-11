import sqlite3
import pandas as pd
from src.database import DatabaseManager
from src.reports import ReportGenerator

db = DatabaseManager('loan_master.db')

# Identify user 'test' or create one
cursor = db.conn.cursor()
cursor.execute("SELECT id FROM individuals WHERE name='test'")
row = cursor.fetchone()
if not row:
    cursor.execute("INSERT INTO individuals (name) VALUES ('test')")
    db.conn.commit()
    user_id = cursor.lastrowid
else:
    user_id = row[0]

# Add a Deposit of 2500 on 2025-08-15
db.add_savings_transaction(user_id, '2025-08-15', 'Deposit', 2500, 'Test Deposit')

# Add a Withdrawal of 5000 on 2025-08-15
db.add_savings_transaction(user_id, '2025-08-15', 'Withdrawal', 5000, 'Test Withdrawal')

# Generate Report
g = ReportGenerator(db)
g.generate_quarterly_savings_report('2025-08-01', 'Quarterly_Savings_Report_Verify.csv')

# Print the specific row for 'test'
df = pd.read_csv('Quarterly_Savings_Report_Verify.csv')
print(df[df['Name'] == 'test'])
