from unittest.mock import MagicMock, patch
from datetime import datetime
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'src'))
from src.reports import ReportGenerator

def test_fy_logic():
    print("\n--- Testing FY Logic ---")
    mock_db = MagicMock()
    mock_db.get_setting.return_value = "November" # FY starts Nov
    
    gen = ReportGenerator(mock_db)
    
    # 1. Test get_fy_start_month_index
    idx = gen.get_fy_start_month_index()
    print(f"FY Start Month Index (Nov): {idx}")
    assert idx == 11
    
    # 2. Test get_recent_quarters
    # Should include Nov previous year, Feb, May, Aug, Nov current year...
    # Let's freeze time conceptually or just check membership
    quarters = gen.get_recent_quarters()
    print(f"Generated {len(quarters)} recent quarters.")
    for q in quarters[:5]:
        print(f" - {q.strftime('%Y-%m-%d')}")
        
    # Check alignment
    for q in quarters:
        # With Nov start, months must be 11, 2, 5, 8
        assert q.month in [11, 2, 5, 8]
        assert q.day == 1
        
    # 3. Test get_default_quarter_date
    # If today is Feb 10, 2026. 
    # Nov 2025 started. Feb 2026 started.
    # Default should be Feb 1, 2026 (most recent started).
    
    # We can't easily mock datetime.now() without patching, 
    # but we can check if the result is sensible (<= now, matches valid quarter)
    def_q = gen.get_default_quarter_date()
    print(f"Default Quarter Date selected: {def_q.strftime('%Y-%m-%d')}")
    
    assert def_q <= datetime.now()
    assert def_q.month in [11, 2, 5, 8]
    assert def_q.day == 1

def test_validation():
    print("\n--- Testing Validation Logic ---")
    mock_db = MagicMock()
    # Mock settings: FY starts in January
    mock_db.get_setting.return_value = "January"
    
    gen = ReportGenerator(mock_db)
    
    # Test 1: Valid Date (Jan 1)
    d1 = datetime(2025, 1, 1)
    valid, msg = gen._validate_start_date(d1)
    print(f"Test 1 (Jan 1, FY=Jan): valid={valid}, msg='{msg}'")
    assert valid
    
    # Test 2: Invalid Day (Jan 15)
    d2 = datetime(2025, 1, 15)
    valid, msg = gen._validate_start_date(d2)
    print(f"Test 2 (Jan 15): valid={valid}, msg='{msg}'")
    assert not valid
    assert "1st day" in msg
    
    # Test 3: Invalid Month (Feb 1, FY=Jan -> quarters are Jan, Apr, Jul, Oct)
    d3 = datetime(2025, 2, 1)
    valid, msg = gen._validate_start_date(d3)
    print(f"Test 3 (Feb 1): valid={valid}, msg='{msg}'")
    assert not valid
    assert "Invalid quarter start month" in msg
    
    # Test 4: November FY (Nov, Feb, May, Aug)
    mock_db.get_setting.return_value = "November"
    d4 = datetime(2025, 11, 1) # Valid
    valid, msg = gen._validate_start_date(d4)
    print(f"Test 4 (Nov 1, FY=Nov): valid={valid}, msg='{msg}'")
    assert valid
    
    print("Validation tests passed!")

def test_export_logic():
    print("\n--- Testing Export Logic ---")
    import pandas as pd
    
    # Setup dummy data
    data = {
        "Name": ["Alice", "Bob"],
        "Loan Amount": [1000, 2000],
        "Sub Total": [100, 200],
        "Grand Total": [1100, 2200]
    }
    df = pd.DataFrame(data)
    
    mock_db = MagicMock()
    
    # 1. Test CSV
    # We use a temporary file
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = tmp.name
        
    try:
        gen = ReportGenerator(mock_db)
        success, msg = gen._export_to_csv(df, csv_path)
        print(f"CSV Export: success={success}, msg={msg}")
        assert success
        assert os.path.exists(csv_path)
        # Verify content
        df_read = pd.read_csv(csv_path)
        assert len(df_read) == 2
        assert "Alice" in df_read["Name"].values
    finally:
        if os.path.exists(csv_path):
            os.remove(csv_path)

    # 2. Test PDF (Mocked)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        pdf_path = tmp.name
    
    try:
        mock_view = MagicMock()
        mock_getter = MagicMock(return_value=mock_view)
        
        gen = ReportGenerator(mock_db, printer_view_getter=mock_getter)
        
        # We need to mock QEventLoop and QPageLayout imports inside the method, 
        # or the method will fail if no QApplication etc.
        # Since we can't easily mock imports inside the function without patching sys.modules,
        # checking if we can run it might be hard without PyQt6 installed in test env.
        # But user has PyQt6.
        
        # Let's try to run it and expect failure on QEventLoop if no app, 
        # OR mock the imports if possible.
        # Given complexity, let's just test that it fails gracefully if printer_view is None
        
        gen_no_printer = ReportGenerator(mock_db, printer_view_getter=None)
        success, msg = gen_no_printer._export_to_pdf(df, pdf_path, datetime.now(), datetime.now())
        print(f"PDF Export (No Printer): success={success}, msg={msg}")
        assert not success
        assert "printing not available" in msg
        
        # We won't test full PDF generation in this script to avoid GUI dependencies crashing headless test
        
    finally:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)

    print("Export tests passed!")

def test_excel_formatting():
    print("\n--- Testing Excel Formatting Logic ---")
    import pandas as pd
    
    # Setup dummy data
    df = pd.DataFrame({"Col1": [1, 2], "Col2": [3, 4]})
    
    mock_db = MagicMock()
    # Mock settings calls
    # Sequence: get_setting called for header, then total
    mock_db.get_setting.side_effect = ["#FF0000", "#00FF00"] 
    
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        xlsx_path = tmp.name
        
    try:
        gen = ReportGenerator(mock_db)
        # We can't easily check the xlsx file content colors without openpyxl or similar,
        # but we can verify that get_setting was called with expected keys.
        
        success, msg = gen._export_to_excel(df, xlsx_path)
        print(f"Excel Export with Custom Colors: success={success}, msg={msg}")
        assert success
        
        # Verify db calls
        # We expect get_setting to be called for "excel_header_bg" and "excel_total_bg"
        # Note: ReportGenerator might call get_setting for other things (e.g. FY start) if initialized differently? 
        # But _export_to_excel calls it directly.
        
        # Check args of calls
        calls = mock_db.get_setting.call_args_list
        print(f"DB Calls: {calls}")
        
        # Filter for our keys
        header_call = any("excel_header_bg" in str(c) for c in calls)
        total_call = any("excel_total_bg" in str(c) for c in calls)
        
        assert header_call
        assert total_call
        
    finally:
        if os.path.exists(xlsx_path):
            os.remove(xlsx_path)
            
    print("Formatting tests passed!")

def test_quarter_labels():
    print("\n--- Testing Quarter Label Generation ---")
    from dateutil.relativedelta import relativedelta
    from datetime import datetime
    
    # Simulate the logic used in dashboard.py
    # We want to ensure the "end date" (m3_end) is calculated correctly
    
    # Test case 1: Jan 1 start -> End should be Mar 31
    q_start = datetime(2025, 1, 1)
    m3_end = q_start + relativedelta(months=3, days=-1)
    label = f"{q_start.strftime('%b %Y')} - {m3_end.strftime('%b %Y')}"
    print(f"Test 1: {label}")
    assert m3_end.month == 3
    assert m3_end.day == 31
    assert label == "Jan 2025 - Mar 2025"

    # Test case 2: Nov 1 start (cross-year) -> End should be Jan 31 next year
    q_start = datetime(2025, 11, 1)
    m3_end = q_start + relativedelta(months=3, days=-1)
    label = f"{q_start.strftime('%b %Y')} - {m3_end.strftime('%b %Y')}"
    print(f"Test 2: {label}")
    assert m3_end.year == 2026
    assert m3_end.month == 1
    assert m3_end.day == 31
    assert label == "Nov 2025 - Jan 2026"

    print("Label generation tests passed!")

def test_progress_callback():
    print("\n--- Testing Progress Callback ---")
    import pandas as pd
    mock_db = MagicMock()
    # Mock individuals: 3 people
    mock_db.get_individuals.return_value = [
        (1, "Alice"), (2, "Bob"), (3, "Charlie")
    ]
    # Mock ledger (empty is fine, we just want to check the loop)
    mock_db.get_ledger.return_value = pd.DataFrame()
    
    # Track callbacks
    callbacks = []
    def on_progress(curr, total, msg):
        callbacks.append((curr, total, msg))
        
    gen = ReportGenerator(mock_db)
    
    # Run helper (mock validation to pass)
    with patch.object(gen, '_validate_start_date', return_value=(True, "")):
        with patch.object(gen, '_get_fy_start_date', return_value=datetime(2025, 1, 1)):
             # We need a valid start date string
             gen.generate_quarterly_report("2025-01-01", "dummy.xlsx", progress_callback=on_progress)
             
    # Verify callbacks
    print(f"Callbacks received: {len(callbacks)}")
    assert len(callbacks) == 3
    # Check values
    assert callbacks[0] == (1, 3, "Processing Alice...")
    assert callbacks[1] == (2, 3, "Processing Bob...")
    assert callbacks[2] == (3, 3, "Processing Charlie...")
    
    print("Progress callback tests passed!")

def test_legacy_warnings():
    print("\n--- Testing Legacy Data Warnings ---")
    mock_db = MagicMock()
    mock_db.get_individuals.return_value = [(1, "Alice")]
    
    # Mock ledger WITHOUT principal_balance
    import pandas as pd
    data = {
        "id": [1],
        "date": ["2025-01-01"],
        "loan_id": [101],
        "amount": [1000],
        "balance": [1000],
        "event_type": ["Disbursement"], # Needed for interest calc
        "details": ["Test"]
    }
    mock_db.get_ledger.return_value = pd.DataFrame(data)
    
    # Mock export to succeed
    gen = ReportGenerator(mock_db)
    # Patch export methods to return success
    with patch.object(gen, '_export_to_excel', return_value=(True, "Exported")):
        with patch.object(gen, '_validate_start_date', return_value=(True, "")):
             with patch.object(gen, '_get_fy_start_date', return_value=datetime(2025, 1, 1)):
                 # Mock get_balance_from_tx to avoid errors since schema is missing
                 # We need to make sure the code doesn't crash before checking warnings.
                 # Actually, the code might crash if `principal_balance` is missing in other parts.
                 # Let's see if our change handles it.
                 # The change was just to add to warnings. The existing code uses logical fallback?
                 # Wait, existing code might try to access it. 
                 # Let's look at `_collect_loan_report_row` or `_get_balance_from_tx`.
                 # For this test, we just want to verify the WARNING is added.
                 # We assume the rest of the code handles missing columns via .get() or checks (which is what we are improving, but the warning is step 1).
                 
                 # To ensure it runs, let's mock _collect_loan_report_row?
                 # No, better to let it run and see if it crashes or warns.
                 # If it crashes, we found another bug to fix!
                 
                 # But wait, `_get_balance_from_tx` generally checks columns.
                 # Let's run it.
                 success, msg = gen.generate_quarterly_report("2025-01-01", "dummy.xlsx")
                 
    print(f"Result: success={success}, msg='{msg}'")
    assert success
    assert "Legacy data schema detected" in msg
    assert "Alice" in msg
    
    print("Legacy warning tests passed!")

if __name__ == "__main__":
    try:
        test_validation()
        test_fy_logic()
        test_export_logic()
        test_excel_formatting()
        test_quarter_labels()
        test_progress_callback()
        test_legacy_warnings()
        print("\nALL TESTS PASSED SUCCESSFULLY")
    except Exception as e:
        print(f"\nTEST FAILED: {e}")
        import traceback
        traceback.print_exc()
