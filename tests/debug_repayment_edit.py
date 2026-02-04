import sys
import os
import math

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import DatabaseManager
from src.engine import LoanEngine

def test_repayment_edit():
    print("\n=== Testing Repayment Edit Logic ===")
    
    # 1. Setup
    db = DatabaseManager(":memory:")
    ind_id = db.add_individual("Test User", "123", "test@test.com")
    engine = LoanEngine(db)
    
    # Loan. Rate ~1200.
    engine.add_loan_event(ind_id, 200000, 25, "2026-01-01", 0.15)
    loan_ref = db.get_active_loans(ind_id)[0]['ref']
    
    # Interest Earned (Standard) -> 1200
    db.add_transaction(ind_id, "2026-02-01", "Interest Earned", loan_ref, 1200, 0, 0, "Int 1")
    
    # Repayment (Standard)
    # Payment 9200. Int 1200. Prin 8000.
    db.add_transaction(ind_id, "2026-02-01", "Repayment", loan_ref, 0, 9200, 0, "Repay 1")
    
    # FIX: Recalculate Balances so Interest Balance is correct for Edit Repayment to consume
    engine.recalculate_balances(ind_id)
    
    # Verify Initial State
    df = db.get_ledger(ind_id)
    rep_row = df[df['notes'] == "Repay 1"].iloc[0]
    print(df[['id', 'date', 'event_type', 'added', 'deducted', 'interest_balance', 'interest_portion']])
    print(f"Initial Split for ID {rep_row['id']}: I={rep_row['interest_portion']}, P={rep_row['principal_portion']}")
    
    # 2. Edit Repayment to 7000
    # Using engine.update_repayment_amount (simulating UI)
    print("Editing Repayment to 7000...")
    engine.update_repayment_amount(ind_id, rep_row['id'], 7000, "Repay 1 Edited")
    
    # 3. Check Persistence
    df_edited = db.get_ledger(ind_id)
    rep_edited = df_edited[df_edited['notes'] == "Repay 1 Edited"].iloc[0]
    
    print(f"Edited Split: I={rep_edited['interest_portion']}, P={rep_edited['principal_portion']}")
    print(f"Is Edited Flag: {rep_edited['is_edited']}")
    
    # Expectation: I=1200 (since 1200 available). P=5800.
    
    # 4. Trigger Recalculation (Delete Dummy)
    db.add_transaction(ind_id, "2026-03-01", "Repayment", loan_ref, 0, 100, 0, "Dummy")
    df_temp = db.get_ledger(ind_id)
    dummy_id = df_temp[df_temp['notes'] == "Dummy"].iloc[0]['id']
    engine.delete_transaction(ind_id, dummy_id)
    
    # 5. Check Persistence After Recalc
    df_final = db.get_ledger(ind_id)
    rep_final = df_final[df_final['notes'] == "Repay 1 Edited"].iloc[0]
    
    # Check Interest Earned Row (ID 2)
    # It should have turned into 918 and Stayed 918
    int_row = df_final[df_final['event_type'] == "Interest Earned"].iloc[0]
    print(f"Interest Earned Row: Added={int_row['added']}, IsEdited={int_row['is_edited']}")
    
    print(f"Final Split (After Recalc): I={rep_final['interest_portion']}, P={rep_final['principal_portion']}")
    
    # === NEW TEST: Future Deduction Stability ===
    # Add a Future Repayment (May 1) - simulates user clicking "Deduct" *after* the Edit
    # Since Loan Record Rate is 918 (updated by update_repayment_amount), this new transaction should use 918.
    print("Adding Future Deduction (May 1)...")
    db.add_transaction(ind_id, "2026-05-01", "Interest Earned", loan_ref, 0, 0, 0, "Int Future")
    # Note: Logic normally adds Int Earned then Repayment. 
    # But add_transaction logic is raw. 
    # Let's perform a Replay to simulate "Simulation logic producing rows".
    # Wait, real app uses deduct_single_loan which uses stored rate.
    # So if we simulate Replay, we are effectively testing recalculate_loan_history directly.
    
    # Force Replay again (triggered by Deletion or just calling it)
    print("Triggering Replay Again...")
    
    # Check DB State for Persisted Rate
    rep_check = db.get_transaction(int(rep_final['id']))
    print(f"DEBUG Check: Repayment ID {rep_final['id']} interest_amount={rep_check.get('interest_amount')}")
    
    engine.recalculate_loan_history(ind_id, loan_ref)
    
    # Check May 1 Interest Earned
    df_future = db.get_ledger(ind_id)
    future_int = df_future[df_future['notes'] == "Int Future"]
    
    # Note: add_transaction inserted 0. Replay should fill it with Rate.
    # If Replay fills 1200 -> FAIL.
    # If Replay fills 918 -> PASS.
    if not future_int.empty:
        val = float(future_int.iloc[0]['added'])
        print(f"Future Interest Value: {val}")
        if val == 918.0:
            print("PASS: Future Interest uses New Rate (918).")
        elif val == 1200.0:
            print("FAIL: Future Interest reverted to Standard Rate (1200).")
        else:
             print(f"FAIL: Future Interest is {val}")
    else:
        print("FAIL: Future Interest row missing?")

    # Existing Checks
    if float(int_row['added']) == 918.0 and float(rep_final['interest_portion']) == 918.0:
        print("PASS: Interest Row and Repayment Split matches 918.")
    elif float(int_row['added']) == 1200.0 and float(rep_final['interest_portion']) == 918.0:
        print("FAIL: Mismatch! Interest Reverted to 1200, Repay Locked at 918.")
    else:
        print(f"FAIL/Other: Int={int_row['added']}, RepayI={rep_final['interest_portion']}")

    # === NEW TEST: UNDO / DELETE Trans ===
    # Delete the Rate-Changing Repayment (ID 3)
    # This should trigger Replay.
    # Replay should see NO Valid Repayments (or only future ones if we didn't delete them?).
    # If we delete ID 3.
    # Replay runs. It hits Loan Issued. Rate 1200.
    # It hits nothing else.
    # Final Rate should be 1200.
    # Loan Record should be updated to 1200.
    
    print("\n=== Testing Undo / Deletion ===")
    engine.delete_transaction(ind_id, rep_final['id'])
    
    # Check Loan Record in DB
    loan = db.get_loan_by_ref(ind_id, loan_ref)
    final_rate = float(loan['monthly_interest'])
    print(f"Loan Monthly Interest after Undo: {final_rate}")
    
    if final_rate == 1200.0:
        print("PASS: Loan Terms reverted to Standard (1200).")
    elif final_rate == 918.0:
         print("FAIL: Loan Terms stuck at (918).")
    else:
         print(f"FAIL/Other: {final_rate}")
         
    # Check Future Transaction (May 1)
    df_undo = db.get_ledger(ind_id)
    may_int = df_undo[df_undo['notes'] == "Int Future"]
    if not may_int.empty:
        val = float(may_int.iloc[0]['added'])
        print(f"May Interest Value after Undo: {val}")
        if val == 1200.0:
            print("PASS: Future Interest successfully reverted to 1200.")
        elif val == 918.0:
            print("FAIL: Future Interest stuck at 918 (did not unlock).")
        else:
            print(f"FAIL: Value is {val}")
    else:
        print("FAIL: Future Interest missing.")

if __name__ == "__main__":
    test_repayment_edit()
