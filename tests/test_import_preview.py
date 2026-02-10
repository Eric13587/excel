
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/preview_source.db"
DEST_DB = "tests/preview_dest.db"

def setup_source():
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    conn = sqlite3.connect(SOURCE_DB)
    c = conn.cursor()
    c.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    c.execute("CREATE TABLE loans (id INTEGER PRIMARY KEY, individual_id INTEGER, ref TEXT, principal REAL, total_amount REAL, balance REAL, installment REAL, start_date TEXT, next_due_date TEXT, status TEXT)")
    c.execute("CREATE TABLE ledger (id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, event_type TEXT, loan_id TEXT, added REAL, deducted REAL, balance REAL, notes TEXT, installment_amount REAL, batch_id TEXT, interest_amount REAL, principal_balance REAL, interest_balance REAL, principal_portion REAL, interest_portion REAL)")
    c.execute("CREATE TABLE savings (id INTEGER PRIMARY KEY, individual_id INTEGER, date TEXT, transaction_type TEXT, amount REAL, balance REAL, notes TEXT)")
    
    # 1. New: "Bob New"
    c.execute("INSERT INTO individuals (name) VALUES ('Bob New')")
    bob_id = c.lastrowid
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-BOB', 500)", (bob_id,))
    
    # 2. Merged: "Alice Exists" (Exact Match)
    c.execute("INSERT INTO individuals (name) VALUES ('Alice Exists')")
    alice_id = c.lastrowid
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-ALICE-NEW', 1000)", (alice_id,))
    
    # 3. Conflict: "Charlie Conflict" (Name diff case, but phone match)
    c.execute("INSERT INTO individuals (name, phone) VALUES ('Charlie Conflict', '1234567890')")
    charlie_id = c.lastrowid
    
    conn.commit()
    conn.close()
    return bob_id, alice_id, charlie_id

def setup_dest():
    if os.path.exists(DEST_DB):
        os.remove(DEST_DB)
    db = DatabaseManager(DEST_DB)
    
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # Alice exists exactly
    c.execute("INSERT INTO individuals (name) VALUES ('Alice Exists')")
    
    # Charlie exists with different name but same phone
    c.execute("INSERT INTO individuals (name, phone) VALUES ('Charlie Existing', '1234567890')")
    
    conn.commit()
    conn.close()
    return db

def test_preview():
    print("--- START PREVIEW TEST ---")
    bob_id, alice_id, charlie_id = setup_source()
    db = setup_dest()
    
    print("Generating Preview...")
    # Select all 3
    selected_ids = [bob_id, alice_id, charlie_id]
    options = {"import_loans": True}
    
    preview = db.generate_import_preview(SOURCE_DB, selected_ids, options)
    
    if not preview:
        print("FAIL: Preview returned None")
        return
        
    s = preview['summary']
    print(f"Summary: {s}")
    
    # Verifications
    if s['individuals_new'] == 1:
        print("PASS: Correct New Count (1 - Bob)")
    else:
        print(f"FAIL: Expected 1 New, got {s['individuals_new']}")
        
    if s['individuals_merged'] == 1:
        print("PASS: Correct Merged Count (1 - Alice)")
    else:
        print(f"FAIL: Expected 1 Merged, got {s['individuals_merged']}")
        
    if s['conflicts'] == 1:
        print("PASS: Correct Conflict Count (1 - Charlie)")
    else:
        print(f"FAIL: Expected 1 Conflict, got {s['conflicts']}")
        
    # Check Details
    if 'Bob New' in preview['details']['new_names']:
        print("PASS: Bob identified as New")
    else:
        print("FAIL: Bob not in new_names")
        
    if 'Alice Exists' in preview['details']['merged_names']:
        print("PASS: Alice identified as Merged (Auto-merge)")
    else:
        print("FAIL: Alice not in merged_names")
        
    # Check Conflict Logic
    conflicts = preview['conflicts']
    if len(conflicts) == 1 and conflicts[0]['src']['name'] == 'Charlie Conflict':
        print("PASS: Charlie identified as Conflict")
        # Check reason
        matches = conflicts[0]['matches']
        if len(matches) > 0 and 'Phone' in matches[0]['reason']:
            print("PASS: Conflict reason includes Phone")
        else:
            print(f"FAIL: Conflict reason mismatch: {matches}")
    else:
        print(f"FAIL: Conflict list incorrect: {conflicts}")
        
    # Check Loans Count
    # Bob has 1, Alice has 1. Charlie has 0. Total = 2.
    if s['loans'] == 2:
        print("PASS: Correct Loan Count (2)")
    else:
        print(f"FAIL: Expected 2 Loans, got {s['loans']}")

if __name__ == "__main__":
    test_preview()
