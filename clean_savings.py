import sqlite3
from src.database import DatabaseManager
from src.services.savings_service import SavingsService

db = DatabaseManager('loan_master.db')
service = SavingsService(db)

cursor = db.conn.cursor()

# Find all individuals affected by the bad 'Monthly Increment (Auto)' entries
cursor.execute("SELECT DISTINCT individual_id FROM savings WHERE notes='Monthly Increment (Auto)'")
affected_ids = [row[0] for row in cursor.fetchall()]

# Delete all mass-generated entries
cursor.execute("DELETE FROM savings WHERE notes='Monthly Increment (Auto)'")
deleted_count = cursor.rowcount
print(f"Deleted {deleted_count} polluted auto-increments.")
db.conn.commit()

# Recalculate balances
for i_id in affected_ids:
    service.recalculate_user_savings(i_id)

print("Balances recalculated successfully.")
