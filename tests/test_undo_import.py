
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/undo_source.db"
DEST_DB = "tests/undo_dest.db"

def setup_source():
    if os.path.exists(SOURCE_DB):
        os.remove(SOURCE_DB)
    conn = sqlite3.connect(SOURCE_DB)
    c = conn.cursor()
    c.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    c.execute("CREATE TABLE loans (id INTEGER PRIMARY KEY, individual_id INTEGER, ref TEXT, principal REAL, total_amount REAL, balance REAL, installment REAL, start_date TEXT, next_due_date TEXT, status TEXT)")
    
    # 1. To be NEW: "Bob New"
    c.execute("INSERT INTO individuals (name) VALUES ('Bob New')")
    bob_id = c.lastrowid
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-BOB', 500)", (bob_id,))
    
    # 2. To be MERGED: "Alice Exists"
    c.execute("INSERT INTO individuals (name) VALUES ('Alice Exists')")
    alice_id = c.lastrowid
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-ALICE-NEW', 1000)", (alice_id,))
    
    conn.commit()
    conn.close()
    return bob_id, alice_id

def setup_dest():
    if os.path.exists(DEST_DB):
        os.remove(DEST_DB)
    db = DatabaseManager(DEST_DB)
    
    # Create "Alice Exists" beforehand
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    c.execute("INSERT INTO individuals (name) VALUES ('Alice Exists')")
    alice_dest_id = c.lastrowid
    
    # Create an old loan for Alice (should NOT be deleted by undo)
    c.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-ALICE-OLD', 200)", (alice_dest_id,))
    old_loan_id = c.lastrowid
    
    conn.commit()
    conn.close()
    return db, alice_dest_id, old_loan_id

def test_undo_import():
    print("--- START UNDO TEST ---")
    src_bob_id, src_alice_id = setup_source()
    db, dest_alice_id, dest_old_loan_id = setup_dest()
    
    # 1. Run Import
    print("Running Import...")
    decision_map = {
        src_bob_id: "new",
        src_alice_id: dest_alice_id # Explicit merge
    }
    res = db.import_selected_data(SOURCE_DB, [src_bob_id, src_alice_id], {"import_loans": True}, decision_map=decision_map)
    
    import_id = res.get("import_id")
    if not import_id:
        print("FAIL: No import_id returned")
        return
        
    print(f"Import ID: {import_id}")
    
    # Verify Import
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # Bob should exist
    c.execute("SELECT id FROM individuals WHERE name='Bob New'")
    bob = c.fetchone()
    if not bob: print("FAIL: Bob not imported")
    else: print("PASS: Bob imported")
    
    # Alice should have 2 loans (1 old, 1 new)
    c.execute("SELECT count(*) FROM loans WHERE individual_id=?", (dest_alice_id,))
    count = c.fetchone()[0]
    if count != 2: print(f"FAIL: Alice has {count} loans, expected 2")
    else: print("PASS: Alice has 2 loans (merged)")
    
    conn.close()
    
    # 2. Undo Import
    print("\nRunning Undo...")
    success = db.undo_import(import_id)
    if not success:
        print("FAIL: Undo reported failure")
        return
        
    # 3. Verify Undo
    conn = sqlite3.connect(DEST_DB)
    c = conn.cursor()
    
    # Bob should be GONE
    c.execute("SELECT id FROM individuals WHERE name='Bob New'")
    bob = c.fetchone()
    if bob: print("FAIL: Bob still exists after undo")
    else: print("PASS: Bob deleted")
    
    # Alice should exist
    c.execute("SELECT id FROM individuals WHERE name='Alice Exists'")
    alice = c.fetchone()
    if not alice: print("FAIL: Alice deleted (should remain)")
    else: print("PASS: Alice remains")
    
    # Alice should have ONLY 1 loan (the old one)
    c.execute("SELECT id, ref FROM loans WHERE individual_id=?", (dest_alice_id,))
    loans = c.fetchall()
    if len(loans) == 1 and loans[0][1] == 'L-ALICE-OLD':
        print("PASS: Alice loans reverted correctly (only old loan remains)")
    else:
        print(f"FAIL: Alice loans incorrect: {loans}")
        
    # Import History should be gone
    c.execute("SELECT * FROM import_history WHERE id=?", (import_id,))
    hist = c.fetchone()
    if hist: print("FAIL: Import history record not deleted")
    else: print("PASS: Import history deleted")
    
    conn.close()

if __name__ == "__main__":
    test_undo_import()
