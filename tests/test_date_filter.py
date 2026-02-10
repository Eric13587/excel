
import sqlite3
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/date_filter_source.db"
DEST_DB = "tests/date_filter_dest.db"

def setup_databases():
    if os.path.exists(SOURCE_DB): os.remove(SOURCE_DB)
    if os.path.exists(DEST_DB): os.remove(DEST_DB)
    
    # Source
    conn = sqlite3.connect(SOURCE_DB)
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    conn.execute("INSERT INTO individuals (name) VALUES ('Date Subject')")
    
    # Loans Table
    conn.execute("""
        CREATE TABLE loans (
            id INTEGER PRIMARY KEY, individual_id INTEGER, ref TEXT, 
            principal REAL, total_amount REAL, balance REAL, installment REAL, 
            start_date TEXT, next_due_date TEXT, status TEXT, 
            monthly_interest REAL, unearned_interest REAL, interest_balance REAL, import_id INTEGER
        )
    """)
    conn.execute("INSERT INTO loans (individual_id, ref, total_amount) VALUES (1, 'L-001', 1000)")
    
    # Ledger Table
    conn.execute("""
        CREATE TABLE ledger (
            id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, event_type TEXT, 
            loan_id TEXT, added REAL, deducted REAL, balance REAL, notes TEXT,
            installment_amount REAL, batch_id TEXT, interest_amount REAL,
            principal_balance REAL, interest_balance REAL, principal_portion REAL, interest_portion REAL,
            previous_state TEXT, is_edited INTEGER, import_id INTEGER
        )
    """)
    # Savings Table
    conn.execute("""
        CREATE TABLE savings (
            id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, 
            transaction_type TEXT, amount REAL, balance REAL, notes TEXT, import_id INTEGER
        )
    """)
    
    # Insert Data (Jan, Feb, Mar 2023)
    # Ledger
    conn.execute("INSERT INTO ledger (individual_id, date, notes) VALUES (1, '2023-01-15', 'Jan Tx')")
    conn.execute("INSERT INTO ledger (individual_id, date, notes) VALUES (1, '2023-02-15', 'Feb Tx')")
    conn.execute("INSERT INTO ledger (individual_id, date, notes) VALUES (1, '2023-03-15', 'Mar Tx')")
    
    # Savings
    conn.execute("INSERT INTO savings (individual_id, date, amount, transaction_type) VALUES (1, '2023-01-15', 100, 'Deposit')")
    conn.execute("INSERT INTO savings (individual_id, date, amount, transaction_type) VALUES (1, '2023-02-15', 100, 'Deposit')")
    conn.execute("INSERT INTO savings (individual_id, date, amount, transaction_type) VALUES (1, '2023-03-15', 100, 'Deposit')")
    
    conn.commit()
    conn.close()
    
    # Dest
    return DatabaseManager(DEST_DB)

def test_date_filter():
    print("--- START DATE FILTER TEST ---")
    db = setup_databases()
    
    # Run Import with Filter (Feb only)
    print("Running Import (Filter: 2023-02-01 to 2023-02-28)...")
    options = {
        "import_loans": True, # Also imports ledger
        "import_savings": True,
        "date_range": ("2023-02-01", "2023-02-28")
    }
    db.import_selected_data(SOURCE_DB, [1], options)
    
    # Verify Ledger
    conn = sqlite3.connect(DEST_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT date, notes FROM ledger WHERE individual_id=1 ORDER BY date")
    ledger_rows = cursor.fetchall()
    
    print(f"Ledger Rows Imported: {len(ledger_rows)}")
    for date, notes in ledger_rows:
        print(f"  - {date}: {notes}")
        
    if len(ledger_rows) == 1 and ledger_rows[0][1] == 'Feb Tx':
        print("PASS: Ledger correctly filtered.")
    else:
        print("FAIL: Ledger filtering failed.")

    # Verify Savings
    cursor.execute("SELECT date, amount FROM savings WHERE individual_id=1 ORDER BY date")
    savings_rows = cursor.fetchall()
    
    print(f"Savings Rows Imported: {len(savings_rows)}")
    for date, amt in savings_rows:
        print(f"  - {date}: {amt}")
        
    if len(savings_rows) == 1 and savings_rows[0][0] == '2023-02-15':
        print("PASS: Savings correctly filtered.")
    else:
        print("FAIL: Savings filtering failed.")

    conn.close()
    
    # Cleanup
    if os.path.exists(SOURCE_DB): os.remove(SOURCE_DB)
    if os.path.exists(DEST_DB): os.remove(DEST_DB)

if __name__ == "__main__":
    test_date_filter()
