
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/dup_source.db"
DEST_DB = "tests/dup_dest.db"

def setup_source():
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    conn = sqlite3.connect(SOURCE_DB)
    c = conn.cursor()
    c.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    c.execute("CREATE TABLE loans (id INTEGER PRIMARY KEY, individual_id INTEGER, ref TEXT, principal REAL, total_amount REAL, balance REAL, installment REAL, start_date TEXT, next_due_date TEXT, status TEXT)")
    
    # John Doe (Source)
    c.execute("INSERT INTO individuals (name, phone) VALUES ('John Doe', '111-1234')")
    src_id = c.lastrowid
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-SRC', 1000)", (src_id,))
    
    # Alice (Source) - No conflict
    c.execute("INSERT INTO individuals (name, phone) VALUES ('Alice', '999')")
    alice_id = c.lastrowid
    
    conn.commit()
    conn.close()
    return src_id, alice_id

def setup_dest():
    if os.path.exists(DEST_DB):
        os.remove(DEST_DB)
    db = DatabaseManager(DEST_DB)
    
    # john doe (Dest) - Case difference conflict
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    c.execute("INSERT INTO individuals (name, phone) VALUES ('john doe', '222-5678')")
    dest_id = c.lastrowid
    conn.commit()
    conn.close()
    return db, dest_id

def test_duplicate_detection():
    print("--- START TEST ---")
    src_john_id, src_alice_id = setup_source()
    db, dest_john_id = setup_dest()
    
    # 1. Check Conflicts
    print("Checking Conflicts...")
    conflicts = db.check_import_conflicts(SOURCE_DB, [src_john_id, src_alice_id])
    
    # Expect 1 conflict for John
    if len(conflicts) == 1:
        c = conflicts[0]
        if c['src']['name'] == 'John Doe' and c['matches'][0]['id'] == dest_john_id:
            print("PASS: Detected name conflict 'John Doe' vs 'john doe'")
        else:
            print(f"FAIL: Conflict details mismatch: {c}")
    else:
        print(f"FAIL: Expected 1 conflict, found {len(conflicts)}")

    # 2. Test SKIP
    print("\nTest SKIP Action...")
    decision_map = {src_john_id: "skip"}
    res = db.import_selected_data(SOURCE_DB, [src_john_id], {}, decision_map=decision_map)
    
    # Verify John NOT imported (count should range)
    # Actually count from stats
    if res['stats']['individuals'] == 0:
        print("PASS: Skipped import")
    else:
        print(f"FAIL: Skipped import but stats > 0: {res['stats']}")
        
    # 3. Test MERGE
    print("\nTest MERGE Action...")
    decision_map = {src_john_id: dest_john_id}
    res = db.import_selected_data(SOURCE_DB, [src_john_id], {"import_loans": True}, decision_map=decision_map)
    
    # Verify stats
    # Individuals: 0 (merged, not new... wait, I commented out the increment for merge in database.py, need to verify behavior)
    # Loans: 1 (should be imported)
    if res['stats']['individuals'] == 0 and res['stats']['loans'] == 1:
        print("PASS: Merged successfully (loans imported, no new individual)")
        
        # Verify loan exists in Dest linked to dest_john_id
        conn = sqlite3.connect(DEST_DB)
        c = conn.cursor()
        c.execute("SELECT individual_id FROM loans WHERE ref='L-SRC'")
        row = c.fetchone()
        conn.close()
        if row and row[0] == dest_john_id:
            print("PASS: Loan linked to existing ID")
        else:
            print(f"FAIL: Loan not linked correctly. Found: {row}")
            
    else:
        print(f"FAIL: Merge stats mismatch: {res['stats']}")

    # 4. Test CREATE NEW
    print("\nTest CREATE NEW Action...")
    # Clean dest first or separate test?
    # Let's clean dest to avoid confusion or just check count.
    # If we create new, we expect a 2nd "John Doe" (with capital J).
    decision_map = {src_john_id: "new"}
    res = db.import_selected_data(SOURCE_DB, [src_john_id], {}, decision_map=decision_map)
    
    if res['stats']['individuals'] == 1:
        print("PASS: Created new individual")
    else:
        print(f"FAIL: Create new failed: {res['stats']}")

if __name__ == "__main__":
    test_duplicate_detection()
