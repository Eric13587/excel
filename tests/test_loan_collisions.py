
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/collision_source.db"
DEST_DB = "tests/collision_dest.db"

def setup_source():
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    conn = sqlite3.connect(SOURCE_DB)
    c = conn.cursor()
    c.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    c.execute("CREATE TABLE loans (id INTEGER PRIMARY KEY, individual_id INTEGER, ref TEXT, principal REAL, total_amount REAL, balance REAL, installment REAL, start_date TEXT, next_due_date TEXT, status TEXT)")
    c.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, event_type TEXT, loan_id TEXT, added REAL, deducted REAL, balance REAL, notes TEXT, installment_amount REAL, batch_id TEXT, interest_amount REAL, principal_balance REAL, interest_balance REAL, principal_portion REAL, interest_portion REAL)")
    c.execute("CREATE TABLE savings (id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, transaction_type TEXT, amount REAL, balance REAL, notes TEXT)")
    
    # 1. Individual "Collision Tester"
    c.execute("INSERT INTO individuals (name) VALUES ('Collision Tester')")
    ind_id = c.lastrowid
    
    # 2. Loan with REF "L-100" (Will collide)
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-100', 500)", (ind_id,))
    
    # 3. Ledger Entry for L-100
    c.execute("INSERT INTO ledger (individual_id, loan_id, event_type, added) VALUES (?, 'L-100', 'Loan Disbursed', 500)", (ind_id,))
    
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
    c.execute("INSERT INTO individuals (name) VALUES ('Collision Tester')")
    dest_ind_id = c.lastrowid
    
    # 2. Existing Loan "L-100" (Collision!)
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-100', 1000)", (dest_ind_id,))
    
    # 3. Existing Loan "L-100-Import" (Collision Level 2!)
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-100-Import', 2000)", (dest_ind_id,))
    
    conn.commit()
    conn.close()
    return db, dest_ind_id

def test_collision():
    print("--- START COLLISION TEST ---")
    src_ind_id = setup_source()
    db, dest_ind_id = setup_dest()
    
    # 1. Generate Preview First
    print("Generating Preview...")
    selected_ids = [src_ind_id]
    options = {"import_loans": True}
    
    preview = db.generate_import_preview(SOURCE_DB, selected_ids, options)
    
    if preview['summary']['loans_renamed'] == 1:
        print("PASS: Preview detected 1 Loan to Rename")
    else:
        print(f"FAIL: Preview detected {preview['summary']['loans_renamed']} renames (Expected 1)")
        
    if 'L-100' in preview['details']['loan_renames']:
         print("PASS: Preview identified 'L-100' for renaming")
    else:
         print(f"FAIL: Preview details missing L-100: {preview['details']['loan_renames']}")

    # 2. Run Import
    print("Running Import...")
    res = db.import_selected_data(SOURCE_DB, selected_ids, options)
    
    if res['status'] != 'success':
        print(f"FAIL: Import failed with {res['errors']}")
        return
        
    # 3. Verify Collision Handling
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # Expectation: 
    # L-100 exists (Original, Principal 1000)
    # L-100-Import exists (Original Level 2, Principal 2000)
    # L-100-Import-1 Should stand for the imported L-100 (Principal 500)
    
    c.execute("SELECT ref, principal FROM loans WHERE individual_id=?", (dest_ind_id,))
    loans = c.fetchall() # List of tuples
    print(f"Loans Found: {loans}")
    
    refs = {l[0]: l[1] for l in loans}
    
    if 'L-100-Import-1' in refs:
        print("PASS: New loan successfully renamed to 'L-100-Import-1'")
        if refs['L-100-Import-1'] == 500:
            print("PASS: Imported loan has correct principal (500)")
        else:
            print(f"FAIL: Imported loan principal mismatch: {refs['L-100-Import-1']}")
    else:
        print("FAIL: 'L-100-Import-1' not found!")
        
    # 4. Verify Ledger Linking
    c.execute("SELECT loan_id FROM ledger WHERE individual_id=? AND added=500", (dest_ind_id,))
    ledger_row = c.fetchone()
    
    if ledger_row:
        lid = ledger_row[0]
        if lid == 'L-100-Import-1':
            print("PASS: Ledger entry correctly linked to renamed loan 'L-100-Import-1'")
        else:
            print(f"FAIL: Ledger entry linked to '{lid}' instead of 'L-100-Import-1'")
    else:
        print("FAIL: Ledger entry not found")

    conn.close()

if __name__ == "__main__":
    test_collision()
