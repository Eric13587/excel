import sqlite3
import pandas as pd
from src.database import DatabaseManager
from src.services.savings_service import SavingsService

db = DatabaseManager("loan_master.db")
service = SavingsService(db)

# Individual 6 had 179861 as balance, let's see what the fix suggests now
suggestion = service.get_suggested_increment(6)
print(f"Suggested increment for Individual 6: {suggestion}")

# Individual 7 had 386883.0 as balance
suggestion7 = service.get_suggested_increment(7)
print(f"Suggested increment for Individual 7: {suggestion7}")
