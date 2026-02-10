
import sqlite3
import os
import sys
import json

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

SOURCE_DB = "tests/audit_source.db"
DEST_DB = "tests/audit_dest.db"

def setup_databases():
    if os.path.exists(SOURCE_DB): os.remove(SOURCE_DB)
    if os.path.exists(DEST_DB): os.remove(DEST_DB)
    
    # Source
    conn = sqlite3.connect(SOURCE_DB)
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, default_deduction REAL)")
    conn.execute("INSERT INTO individuals (name) VALUES ('Audit Subject')")
    conn.commit()
    conn.close()
    
    # Dest
    return DatabaseManager(DEST_DB)

def test_audit_log():
    print("--- START AUDIT LOG TEST ---")
    db = setup_databases()
    
    # Run Import
    print("Running Import...")
    # Just import individuals
    db.import_selected_data(SOURCE_DB, [1], {"import_loans": False})
    
    # Verify History
    conn = sqlite3.connect(DEST_DB)
    cursor = conn.cursor()
    cursor.execute("SELECT details FROM import_history ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    
    if not row:
        print("FAIL: No import history record found.")
        return
        
    details_str = row[0]
    print(f"Details stored: {details_str}")
    
    try:
        data = json.loads(details_str)
        print("PASS: Details column is valid JSON.")
        
        if data.get('individuals') == 1 and data.get('loans') == 0:
             print("PASS: JSON content matches expected stats.")
        else:
             print(f"FAIL: JSON content mismatch: {data}")
             
    except json.JSONDecodeError:
        print(f"FAIL: Details column is NOT valid JSON: {details_str}")
        
    conn.close()
    
    # Cleanup
    if os.path.exists(SOURCE_DB): os.remove(SOURCE_DB)
    if os.path.exists(DEST_DB): os.remove(DEST_DB)

if __name__ == "__main__":
    test_audit_log()
