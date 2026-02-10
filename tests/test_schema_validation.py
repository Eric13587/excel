
import sqlite3
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager

VALID_DB = "tests/valid_schema.db"
INVALID_FILE = "tests/invalid.txt"
EMPTY_FILE = "tests/empty.db"
MISSING_TABLE_DB = "tests/missing_table.db"
MISSING_COL_DB = "tests/missing_col.db"

def setup_files():
    # Cleanup
    for f in [VALID_DB, INVALID_FILE, EMPTY_FILE, MISSING_TABLE_DB, MISSING_COL_DB]:
        if os.path.exists(f):
            os.remove(f)
            
    # 1. Valid DB
    conn = sqlite3.connect(VALID_DB)
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT)")
    conn.close()
    
    # 2. Invalid File
    with open(INVALID_FILE, "w") as f:
        f.write("This is not a database.")
        
    # 3. Empty File
    open(EMPTY_FILE, "w").close()
    
    # 4. Missing Table DB
    conn = sqlite3.connect(MISSING_TABLE_DB)
    conn.execute("CREATE TABLE other_table (id INTEGER)")
    conn.close()
    
    # 5. Missing Column DB
    conn = sqlite3.connect(MISSING_COL_DB)
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, phone TEXT)")
    conn.close()

def test_validation():
    print("--- START SCHEMA VALIDATION TEST ---")
    setup_files()
    db = DatabaseManager(":memory:") # Dummy manager
    
    # 1. Valid DB
    valid, msg = db.validate_source_schema(VALID_DB)
    if valid:
        print("PASS: Valid DB passed validation.")
    else:
        print(f"FAIL: Valid DB failed validation: {msg}")
        
    # 2. Invalid File
    valid, msg = db.validate_source_schema(INVALID_FILE)
    if not valid and "valid SQLite database" in msg:
        print("PASS: Invalid file correctly rejected.")
    else:
        print(f"FAIL: Invalid file not rejected as expected. Result: {valid}, Msg: {msg}")
        
    # 3. Empty File
    valid, msg = db.validate_source_schema(EMPTY_FILE)
    if not valid and "File is empty" in msg:
        print("PASS: Empty file correctly rejected.")
    else:
        print(f"FAIL: Empty file not rejected as expected. Result: {valid}, Msg: {msg}")
        
    # 4. Missing Table
    valid, msg = db.validate_source_schema(MISSING_TABLE_DB)
    if not valid and "Missing required table: 'individuals'" in msg:
        print("PASS: Missing table correctly rejected.")
    else:
        print(f"FAIL: Missing table not rejected as expected. Result: {valid}, Msg: {msg}")

    # 5. Missing Column
    valid, msg = db.validate_source_schema(MISSING_COL_DB)
    if not valid and "missing required column: 'name'" in msg:
        print("PASS: Missing column correctly rejected.")
    else:
        print(f"FAIL: Missing column not rejected as expected. Result: {valid}, Msg: {msg}")
        
    # Cleanup
    for f in [VALID_DB, INVALID_FILE, EMPTY_FILE, MISSING_TABLE_DB, MISSING_COL_DB]:
        if os.path.exists(f):
            os.remove(f)

if __name__ == "__main__":
    test_validation()
