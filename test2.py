import sqlite3
import pandas as pd
from src.database import DatabaseManager

db = DatabaseManager('loan_master.db')
savings = db.get_savings_transactions(6)
print(savings[['id', 'date', 'transaction_type', 'amount', 'balance']].head(15))
