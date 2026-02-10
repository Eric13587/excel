
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/savings_source.db"
DEST_DB = "tests/savings_dest.db"

def setup_source():
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    conn = sqlite3.connect(SOURCE_DB)
    c = conn.cursor()
    c.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    c.execute("CREATE TABLE savings (id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, transaction_type TEXT, amount REAL, balance REAL, notes TEXT)")
    
    # 1. New Individual "Saver"
    c.execute("INSERT INTO individuals (name) VALUES ('Saver')")
    ind_id = c.lastrowid
    
    # 2. Backdated Transaction (Date: 2023-01-01, Amount: 50)
    # Note: Balance here doesn't matter as we ignore source balance and recalc.
    c.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) VALUES (?, '2023-01-01', 'Deposit', 50, 50)", (ind_id,))
    
    conn.commit()
    conn.close()
    return ind_id

def setup_dest():
    if os.path.exists(DEST_DB):
        os.remove(DEST_DB)
    db = DatabaseManager(DEST_DB)
    
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # 1. Existing Individual (Same Name = Merge)
    c.execute("INSERT INTO individuals (name) VALUES ('Saver')")
    dest_ind_id = c.lastrowid
    
    # 2. Existing Transaction (Date: 2023-01-05, Amount: 100)
    # Initial Balance: 100 
    c.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) VALUES (?, '2023-01-05', 'Deposit', 100, 100)", (dest_ind_id,))
    
    conn.commit()
    conn.close()
    return db, dest_ind_id

def test_recalc():
    print("--- START SAVINGS RECALC TEST ---")
    src_ind_id = setup_source()
    db, dest_ind_id = setup_dest()
    
    # Run Import
    print("Running Import...")
    selected_ids = [src_ind_id]
    options = {"import_savings": True}
    
    res = db.import_selected_data(SOURCE_DB, selected_ids, options)
    
    if res['status'] != 'success':
        print(f"FAIL: Import failed with {res['errors']}")
        return
        
    # Verify Balances
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # Fetch all transactions ordered by date
    c.execute("SELECT date, amount, balance FROM savings WHERE individual_id=? ORDER BY date ASC, id ASC", (dest_ind_id,))
    txs = c.fetchall()
    
    print("Transactions found:", txs)
    
    # Expected:
    # 1. 2023-01-01: Deposit 50. Balance -> 50.
    # 2. 2023-01-05: Deposit 100. Balance -> 150 (Previously was 100).
    
    if len(txs) != 2:
        print(f"FAIL: Expected 2 transactions, got {len(txs)}")
        return

    # Check T1 (Imported)
    t1 = txs[0]
    if t1[0] == '2023-01-01' and t1[2] == 50.0:
        print("PASS: T1 (Imported) correct")
    else:
        print(f"FAIL: T1 incorrect: {t1}")
        
    # Check T2 (Existing, Recalculated)
    t2 = txs[1]
    if t2[0] == '2023-01-05' and t2[2] == 150.0:
        print("PASS: T2 (Existing) correctly recalculated to 150.0")
    else:
        print(f"FAIL: T2 incorrect (Expected Balance 150.0): {t2}")

    conn.close()

if __name__ == "__main__":
    test_recalc()
