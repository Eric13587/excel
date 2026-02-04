
"""
Report generation module for LoanMaster.
Handles calculation and export of Quarterly Interest Reports.
"""
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
import math

class ReportGenerator:
    def __init__(self, db_manager):
        self.db = db_manager

    def generate_quarterly_report(self, start_date_str, output_path):
        """
        Generate a quarterly interest report starting from the given date.
        
        Args:
            start_date_str (str): Start date in YYYY-MM-DD format (e.g. 2025-08-01).
            output_path (str): File path to save the Excel report.
            
        Returns:
            bool: True on success, False on failure.
        """
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            # Determine the three months of the quarter
            m1_start = start_date
            m2_start = start_date + relativedelta(months=1)
            m3_start = start_date + relativedelta(months=2)
            m3_end = start_date + relativedelta(months=3, days=-1) # End of the quarter
            
            # Format month names for column headers (e.g., Aug-25)
            m1_name = m1_start.strftime("%b-%y")
            m2_name = m2_start.strftime("%b-%y")
            m3_name = m3_start.strftime("%b-%y")
            
            bf_date_label = f"B/F at {m1_start.strftime('%b %Y')}"
            
            individuals = self.db.get_individuals()
            report_data = []
            
            for ind in individuals:
                ind_id = ind[0]
                name = ind[1]
                
                # Get all active loans (or all loans? User request implies tracking specific loans)
                # However, the table seems to list members, so we iterate members and their active/relevant loans.
                # If a member has multiple loans, we might need multiple rows or sum them up. 
                # The sample image shows "Loan Amount" and "Loan_ID". One row per loan seems appropriate.
                
                # loans = self.db.get_all_loans_for_individual(ind_id) # Need this method or filter locally
                
                # Fetch all loans for this individual manually since get_all_loans_for_individual might not exist
                # Using get_active_loans is safer for "current status", but we might miss paid off loans 
                # that had activity in this quarter.
                # Let's get ALL ledger info first to identify relevant loans.
                
                ledger_df = self.db.get_ledger(ind_id)
                if ledger_df.empty:
                    continue
                
                # Identify unique loans involved
                loan_refs = ledger_df['loan_id'].unique()
                
                for loan_ref in loan_refs:
                    if not loan_ref or loan_ref == '-':
                        continue
                        
                    loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref]
                    
                    # Interest Income based on Accrual ("Interest Earned" events)
                    # Use 'interest_amount' from 'Interest Earned' rows.
                    accrual_txs = loan_txs[loan_txs['event_type'] == 'Interest Earned']
                    
                    # If legacy data exists without "Interest Earned" events?
                    # We might need to fallback? 
                    # For now, assuming this report runs on new data or migrated data.
                    # If mixed, we might need to include 'Repayment' interest_portion if 'Interest Earned' is missing?
                    # Let's check if there are any Interest Earned events.
                    
                    # Strategy: If 'Interest Earned' events exist, use them. 
                    # If not, fallback to 'Repayment' interest_amount (Old logic, effectively cash basis/amortized).
                    use_accrual = not accrual_txs.empty
                    
                    if use_accrual:
                        income_txs = accrual_txs
                        col_to_sum = 'interest_amount' # This is accrual amount
                    else:
                        # Fallback to Repayment events (Old Logic)
                        income_txs = loan_txs[loan_txs['event_type'] == 'Repayment']
                        col_to_sum = 'interest_amount' # This was total interest portion in old logic? 
                        # In old logic `interest_amount` was 'monthly_interest' constant.
                        # Wait, in old logic `interest_amount` in ledger was just `loan['monthly_interest']`.
                        # Which was effectively the accrual amount (since flat rate).
                        pass

                    # Financial Year Logic
                    # Get FY start month from settings
                    fy_start_month_name = self.db.get_setting("fy_start_month", "January")
                    try:
                        fy_start_month_idx = datetime.strptime(fy_start_month_name, "%B").month
                    except ValueError:
                        fy_start_month_idx = 1
                    
                    # Calculate FY Start Date for the current report start date
                    # Logic: If report start month >= fy start month, FY started this year. Else last year.
                    # Note: We assume report start date matches a quarter start, which inherently aligns with FY quarters if used correctly.
                    # Even if not aligned, we just need to find the latest FY start <= report start date.
                    
                    r_year = start_date.year
                    r_month = start_date.month
                    
                    if r_month >= fy_start_month_idx:
                        fy_year = r_year
                    else:
                        fy_year = r_year - 1
                        
                    fy_start_date = datetime(fy_year, fy_start_month_idx, 1)
                    fy_start_str = fy_start_date.strftime("%Y-%m-%d")
                    
                    # Interest B/F: 
                    # If this is Q1 (start_date == fy_start_date), B/F is 0.
                    # Else, B/F is sum of interest from fy_start_date up to (excluding) start_date.
                    
                    if start_date == fy_start_date:
                        interest_bf = 0.0
                    else:
                        # Sum transactions from FY start (inclusive) to Report Start (exclusive)
                        bf_txs = income_txs[(income_txs['date'] >= fy_start_str) & 
                                          (income_txs['date'] < start_date_str)]
                        interest_bf = bf_txs[col_to_sum].sum() if not bf_txs.empty else 0.0
                    
                    # Interest for Month 1
                    m1_txs = income_txs[(income_txs['date'] >= m1_start.strftime("%Y-%m-%d")) & 
                                      (income_txs['date'] < m2_start.strftime("%Y-%m-%d"))]
                    interest_m1 = m1_txs[col_to_sum].sum() if not m1_txs.empty else 0.0
                    
                    # Interest for Month 2
                    m2_txs = income_txs[(income_txs['date'] >= m2_start.strftime("%Y-%m-%d")) & 
                                      (income_txs['date'] < m3_start.strftime("%Y-%m-%d"))]
                    interest_m2 = m2_txs[col_to_sum].sum() if not m2_txs.empty else 0.0
                    
                    # Interest for Month 3
                    m3_next = start_date + relativedelta(months=3)
                    m3_txs = income_txs[(income_txs['date'] >= m3_start.strftime("%Y-%m-%d")) & 
                                      (income_txs['date'] < m3_next.strftime("%Y-%m-%d"))]
                    interest_m3 = m3_txs[col_to_sum].sum() if not m3_txs.empty else 0.0
                    
                    # Determine Historical Principal Balance at end of Quarter
                    quarter_end_date = m3_next.strftime("%Y-%m-%d")
                    hist_txs = loan_txs[loan_txs['date'] < quarter_end_date]
                    
                    balance = 0.0
                    if not hist_txs.empty:
                        last_tx = hist_txs.sort_values(by=['date', 'id']).iloc[-1]
                        # Prioritize 'principal_balance' column if it exists and is non-zero (New Model)
                        if 'principal_balance' in last_tx and float(last_tx['principal_balance']) > 0:
                             balance = float(last_tx['principal_balance'])
                        elif 'balance' in last_tx:
                             # Legacy fallback or if principal_bal is 0 (Paid)
                             # In legacy, balance was Principal+Interest.
                             # We need net. 
                             # If we are in fallback mode (use_accrual=False), we calculate Net.
                             raw_bal = float(last_tx['balance'])
                             if not use_accrual and raw_bal > 0:
                                 # Old incomplete Net calculation logic
                                 current_loan = self.db.get_loan_by_ref(ind_id, loan_ref)
                                 if current_loan and current_loan['installment'] > 0:
                                     # Approximate
                                     principal_ratio = (current_loan['installment'] - current_loan['monthly_interest']) / current_loan['installment']
                                     if principal_ratio < 0: principal_ratio = 0
                                     balance = raw_bal * principal_ratio
                                 else:
                                     balance = raw_bal
                             else:
                                 # In new model, 'principal_balance' is 0 if not set? 
                                 # If new model checks out, balance should be from principal_balance.
                                 # If it is 0, maybe it is paid off.
                                 if 'principal_balance' in last_tx:
                                     balance = float(last_tx['principal_balance'])
                                 else:
                                     balance = raw_bal # Shouldn't happen if we updated schema

                    # Skip if no activity
                    if balance == 0 and interest_bf == 0 and interest_m1 == 0 and interest_m2 == 0 and interest_m3 == 0:
                        continue

                    # Net Loan Amount is simply the balance (Principal) in new model
                    net_loan_amount = balance
                    
                    # Rounding
                    net_loan_amount = math.ceil(net_loan_amount)
                    interest_bf = math.ceil(interest_bf)
                    interest_m1 = math.ceil(interest_m1)
                    interest_m2 = math.ceil(interest_m2)
                    interest_m3 = math.ceil(interest_m3)
                    
                    sub_total = interest_m1 + interest_m2 + interest_m3
                    grand_total = interest_bf + sub_total
                    
                    report_data.append({
                        "Name": name,
                        "Loan Amount": net_loan_amount,
                        bf_date_label: interest_bf,
                        m1_name: interest_m1,
                        m2_name: interest_m2,
                        m3_name: interest_m3,
                        "Sub Total": sub_total,
                        "Grand Total": grand_total
                    })
            
            # Create DataFrame
            df = pd.DataFrame(report_data)
            
            if df.empty:
                # create empty with columns if no data
                df = pd.DataFrame(columns=["Name", "Loan Amount", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Grand Total"])
            else:
                # Calculate Totals
                sums = df.select_dtypes(include=['number']).sum()
                total_row = {col: sums[col] if col in sums else '' for col in df.columns}
                total_row['Name'] = 'TOTAL'
                
                # Append Totals Row
                df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

            # Export to Excel
            writer = pd.ExcelWriter(output_path, engine='xlsxwriter')
            df.to_excel(writer, index=False, sheet_name='Quarterly Report')
            
            workbook = writer.book
            worksheet = writer.sheets['Quarterly Report']
            
            # Formats
            header_fmt = workbook.add_format({'bold': True, 'border': 1, 'bg_color': '#D7E4BC'})
            num_fmt = workbook.add_format({'num_format': '#,##0'})
            total_fmt = workbook.add_format({'bold': True, 'border': 1, 'num_format': '#,##0', 'bg_color': '#f0f0f0'})
            
            # Apply header format
            for col_num, value in enumerate(df.columns.values):
                worksheet.write(0, col_num, value, header_fmt)
                
            # Set column widths and number format
            worksheet.set_column('A:A', 25) # Name
            worksheet.set_column('B:H', 15, num_fmt)
            
            # Apply Totals Row Format (Last Row)
            if not df.empty:
                last_row = len(df) # 1-based index for header + data is len(df)+1? No. 
                # Excel rows are 0-indexed in xlsxwriter.
                # Header is row 0.
                # Data starts row 1.
                # Last row index is len(df). (e.g. 1 data row -> len=1. Header=0, Data=1. Total=Last item. So row idx = len(df))
                
                # Wait. len(df) includes Total row now.
                # If df has 2 rows (Normal + Total).
                # Row 0: Header.
                # Row 1: Normal.
                # Row 2: Total.
                # Index of Total row is indeed len(df) if we don't count header in len, but write starts at row 1.
                # df.to_excel writes header at 0. Rows at 1..N.
                # So indices are 1 to len(df).
                # Total row is at index `len(df)`.
                
                total_row_idx = len(df) 
                
                worksheet.set_row(total_row_idx, None, total_fmt)
                # Re-write the row to ensure format applies to cells?
                # set_row applies style to whole row but cell data might override?
                # Usually better to overwrite the range.
                
                for col_num, col_name in enumerate(df.columns):
                    value = df.iloc[-1][col_name]
                    # Format Name as string if needed, numbers as numbers
                    fmt = total_fmt
                    worksheet.write(total_row_idx, col_num, value, fmt)

            writer.close()
            return True
            
        except Exception as e:
            print(f"Error generating report: {e}")
            import traceback
            traceback.print_exc()
            return False

