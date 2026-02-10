
"""
Report generation module for LoanMaster.
Handles calculation and export of Quarterly Interest Reports.
"""
import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta
import math

class ReportGenerator:
    def __init__(self, db_manager, printer_view_getter=None):
        self.db = db_manager
        self.printer_view_getter = printer_view_getter

    
    def _get_quarter_dates(self, start_date):
        """Calculate start and end dates for the 3 months in the quarter."""
        m1_start = start_date
        m2_start = start_date + relativedelta(months=1)
        m3_start = start_date + relativedelta(months=2)
        m3_end = start_date + relativedelta(months=3, days=-1)
        m3_next = start_date + relativedelta(months=3)
        return m1_start, m2_start, m3_start, m3_end, m3_next

    def _get_fy_start_date(self, report_start_date):
        """Determine the Financial Year start date based on report date and settings."""
        fy_start_month_name = self.db.get_setting("fy_start_month", "January")
        try:
            fy_start_month_idx = datetime.strptime(fy_start_month_name, "%B").month
        except ValueError:
            fy_start_month_idx = 1
        
        r_year = report_start_date.year
        r_month = report_start_date.month
        
        if r_month >= fy_start_month_idx:
            fy_year = r_year
        else:
            fy_year = r_year - 1
            
        return datetime(fy_year, fy_start_month_idx, 1)

    def get_fy_start_month_index(self):
        """Get 1-based index of the Financial Year start month from settings."""
        fy_start_month_name = self.db.get_setting("fy_start_month", "January")
        try:
            return datetime.strptime(fy_start_month_name, "%B").month
        except ValueError:
            return 1

    def get_recent_quarters(self, ref_date=None):
        """
        Get a list of recent and upcoming quarter start dates based on FY settings.
        Returns a list of QDate objects (for UI compatibility) or datetime objects? 
        Let's return datetime objects, UI can convert.
        """
        if ref_date is None:
            ref_date = datetime.now()
            
        fy_start_idx = self.get_fy_start_month_index()
        
        # Look back 1 year and forward 1 year
        current_year = ref_date.year
        candidates = []
        
        # Check last year, current year, next year
        for year in [current_year - 1, current_year, current_year + 1]:
            # FY Quarters: start, start+3, start+6, start+9
            for i in range(4):
                m = fy_start_idx + (i * 3)
                y = year
                if m > 12:
                    m -= 12
                    y += 1 # overflow to next calendar year
                
                # Create date
                try:
                    q_start = datetime(y, m, 1)
                    candidates.append(q_start)
                except ValueError:
                    continue
                    
        # Filter to reasonable range (e.g. +/- 15 months from now)
        # deduplicate and sort
        candidates = sorted(list(set(candidates)))
        
        # Return strict range if needed, or just return all generated?
        # Let's return all generated, UI can filter.
        return candidates

    def get_default_quarter_date(self):
        """Get the default quarter start date (most recent past quarter)."""
        candidates = self.get_recent_quarters()
        now = datetime.now()
        
        # Find latest candidate <= now
        past_candidates = [d for d in candidates if d <= now]
        if past_candidates:
            return past_candidates[-1]
        return now.replace(day=1) # Fallback

    def _validate_start_date(self, start_date):
        """
        Validate that the chosen start date is valid for a quarterly report.
        Criteria:
        1. Must be the 1st day of the month.
        2. Must align with a quarter start based on FY settings.
        """
        if start_date.day != 1:
            return False, "Start date must be the 1st day of the month."
            
        fy_start_month_name = self.db.get_setting("fy_start_month", "January")
        try:
            fy_start = datetime.strptime(fy_start_month_name, "%B").month
        except ValueError:
            fy_start = 1
            
        # Allowed months: start, start+3, start+6, start+9 (modulo 12)
        allowed_months = []
        for i in range(4):
            m = fy_start + (i * 3)
            if m > 12: m -= 12
            allowed_months.append(m)
            
        if start_date.month not in allowed_months:
            months_str = ", ".join([datetime(2000, m, 1).strftime("%B") for m in sorted(allowed_months)])
            return False, f"Invalid quarter start month ({start_date.strftime('%B')}).\nBased on FY start ({fy_start_month_name}), quarters must start in: {months_str}."
            
        return True, ""

    def _calculate_loan_interest(self, loan_ref, ledger_df, q_dates, fy_start_date, report_start_date_str):
        """Calculate interest components for a single loan."""
        m1_start, m2_start, m3_start, _, m3_next = q_dates
        
        loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref]
        if loan_txs.empty:
            return None

        # Determine Interest Source (Accrual vs Repayment fallback)
        accrual_txs = loan_txs[loan_txs['event_type'] == 'Interest Earned']
        use_accrual = not accrual_txs.empty
        
        if use_accrual:
            income_txs = accrual_txs
            col_to_sum = 'interest_amount'
        else:
            income_txs = loan_txs[loan_txs['event_type'] == 'Repayment']
            col_to_sum = 'interest_amount'

        # Interest B/F
        fy_start_str = fy_start_date.strftime("%Y-%m-%d")
        if report_start_date_str == fy_start_str:
            interest_bf = 0.0
        else:
            bf_txs = income_txs[(income_txs['date'] >= fy_start_str) & 
                              (income_txs['date'] < report_start_date_str)]
            interest_bf = bf_txs[col_to_sum].sum() if not bf_txs.empty else 0.0

        # Monthly Interest
        def sum_period(start_dt, end_dt):
            mask = (income_txs['date'] >= start_dt.strftime("%Y-%m-%d")) & \
                   (income_txs['date'] < end_dt.strftime("%Y-%m-%d"))
            filtered = income_txs[mask]
            return filtered[col_to_sum].sum() if not filtered.empty else 0.0

        int_m1 = sum_period(m1_start, m2_start)
        int_m2 = sum_period(m2_start, m3_start)
        int_m3 = sum_period(m3_start, m3_next)

        # Balance at Quarter End
        quarter_end_str = m3_next.strftime("%Y-%m-%d")
        hist_txs = loan_txs[loan_txs['date'] < quarter_end_str]
        
        balance = 0.0
        if not hist_txs.empty:
            last_tx = hist_txs.sort_values(by=['date', 'id']).iloc[-1]
            balance = self._get_balance_from_tx(last_tx, use_accrual)

        # Check for zero activity
        if balance == 0 and interest_bf == 0 and int_m1 == 0 and int_m2 == 0 and int_m3 == 0:
            return None

        return {
            "balance": balance,
            "bf": interest_bf,
            "m1": int_m1,
            "m2": int_m2,
            "m3": int_m3
        }

    def _get_balance_from_tx(self, tx, use_accrual):
        """Extract historical balance from transaction row."""
        # Prioritize 'principal_balance' column if present and valid
        if 'principal_balance' in tx and float(tx['principal_balance']) > 0:
             return float(tx['principal_balance'])
        
        # Legacy fallback logic
        raw_bal = float(tx.get('balance', 0))
        if 'balance' not in tx: return 0.0

        if not use_accrual and raw_bal > 0:
             # Attempt to estimate principal from total balance if no accrual data (old model)
             # This requires loan details which are not in tx row easily.
             # Simplification: Assume raw_bal is close enough or return it directly as per old code logic
             # The old code fetched loan details here, which is expensive inside loop.
             # We will stick to the simplified logic or raw_bal if detailed info missing.
             # For improved performance/correctness, we'd need loan terms.
             # Given refactor scope, let's keep it safe:
             return raw_bal 
        
        # If 'principal_balance' exists but is 0, trust it (paid off)
        if 'principal_balance' in tx:
            return float(tx['principal_balance'])
            
        return raw_bal

    def generate_quarterly_report(self, start_date_str, output_path, progress_callback=None):
        """
        Generate a quarterly interest report starting from the given date.
        
        Args:
            start_date_str (str): Start date in YYYY-MM-DD format (e.g. 2025-08-01).
            output_path (str): File path to save the Excel report.
            progress_callback (callable, optional): function(current, total, message)
            
        Returns:
            tuple: (bool, str) - (Success status, Result message or Error details).
        """
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            
            # Validation
            is_valid, err_msg = self._validate_start_date(start_date)
            if not is_valid:
                return False, err_msg
            
            # 1. Prepare Dates
            q_dates = self._get_quarter_dates(start_date)
            m1_start, m2_start, m3_start, _, _ = q_dates
            
            # Format headers
            m1_name = m1_start.strftime("%b-%y")
            m2_name = m2_start.strftime("%b-%y")
            m3_name = m3_start.strftime("%b-%y")
            bf_date_label = f"B/F at {m1_start.strftime('%b %Y')}"
            
            fy_start_date = self._get_fy_start_date(start_date)
            
            # 2. Collect Data
            individuals = self.db.get_individuals()
            total_individuals = len(individuals)
            report_data = []
            warnings = set()
            
            # Optimization: Pre-fetch settings or loan details if needed?
            # For now, keep DB calls as is but structured cleaner.
            
            for i, ind in enumerate(individuals):
                ind_id, name = ind[0], ind[1]
                
                # Check for cancellation (if callback returns False? or strict stop flag?)
                # Simple progress update
                if progress_callback:
                    progress_callback(i + 1, total_individuals, f"Processing {name}...")
                
                ledger_df = self.db.get_ledger(ind_id)
                if ledger_df.empty:
                    continue

                # Check for Legacy Schema
                if 'principal_balance' not in ledger_df.columns:
                    warnings.add(f"Legacy data schema detected for {name} (using Total Balance).")

                
                loan_refs = ledger_df['loan_id'].unique()
                
                for loan_ref in loan_refs:
                    if not loan_ref or loan_ref == '-':
                        continue
                        
                    res = self._calculate_loan_interest(loan_ref, ledger_df, q_dates, fy_start_date, start_date_str)
                    if not res:
                        continue

                    # Rounding (ceil per requirement)
                    net_loan = math.ceil(res['balance'])
                    bf = math.ceil(res['bf'])
                    m1 = math.ceil(res['m1'])
                    m2 = math.ceil(res['m2'])
                    m3 = math.ceil(res['m3'])
                    
                    sub_total = m1 + m2 + m3
                    grand_total = bf + sub_total
                    
                    report_data.append({
                        "Name": name,
                        "Loan Amount": net_loan,
                        bf_date_label: bf,
                        m1_name: m1,
                        m2_name: m2,
                        m3_name: m3,
                        "Sub Total": sub_total,
                        "Grand Total": grand_total
                    })
            
            # 3. Build DataFrame
            df = pd.DataFrame(report_data)
            
            if df.empty:
                df = pd.DataFrame(columns=["Name", "Loan Amount", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Grand Total"])
            else:
                # Calculate Totals
                sums = df.select_dtypes(include=['number']).sum()
                total_row = {col: sums[col] if col in sums else '' for col in df.columns}
                total_row['Name'] = 'TOTAL'
                df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

            # 4. Export
            # Infer format from extensions or use argument logic
            # Simplified: output_path extension is primary driver
            success = False
            msg = ""
            
            if output_path.endswith('.csv'):
                success, msg = self._export_to_csv(df, output_path)
            elif output_path.endswith('.pdf'):
                success, msg = self._export_to_pdf(df, output_path, m1_start, m3_next)
            else:
                success, msg = self._export_to_excel(df, output_path)
                
            if success and warnings:
                msg += "\n\nWarnings:\n" + "\n".join(sorted(list(warnings)))
                
            return success, msg
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error generating report: {error_msg}")
            import traceback
            traceback.print_exc()
            return False, error_msg

    def _export_to_excel(self, df, output_path):
        """Export DataFrame to Excel with formatting."""
        try:
            # Get colors from settings
            header_bg = self.db.get_setting("excel_header_bg", "#D7E4BC")
            total_bg = self.db.get_setting("excel_total_bg", "#F0F0F0")
            
            with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False, sheet_name='Quarterly Report')
                workbook = writer.book
                worksheet = writer.sheets['Quarterly Report']
                
                # Formats
                header_fmt = workbook.add_format({'bold': True, 'border': 1, 'bg_color': header_bg})
                num_fmt = workbook.add_format({'num_format': '#,##0'})
                total_fmt = workbook.add_format({'bold': True, 'border': 1, 'num_format': '#,##0', 'bg_color': total_bg})
                
                # Apply header format
                for col_num, value in enumerate(df.columns.values):
                    worksheet.write(0, col_num, value, header_fmt)
                    
                # Set column widths
                worksheet.set_column('A:A', 25) # Name
                worksheet.set_column('B:H', 15, num_fmt)
                
                # Apply Totals Row Format
                if not df.empty:
                    total_row_idx = len(df)
                    worksheet.set_row(total_row_idx, None, total_fmt)
                    for col_num, col_name in enumerate(df.columns):
                        val = df.iloc[-1][col_name]
                        worksheet.write(total_row_idx, col_num, val, total_fmt)
                        
            return True, "Report generated successfully."
        except Exception as e:
            return False, f"Excel Export Failed: {e}"

    def _export_to_csv(self, df, output_path):
        """Export DataFrame to CSV."""
        try:
            df.to_csv(output_path, index=False)
            return True, "Report generated successfully (CSV)."
        except Exception as e:
            return False, f"CSV Export Failed: {e}"

    def _export_to_pdf(self, df, output_path, start_date, end_date):
        """Export DataFrame to PDF via HTML and QWebEngineView."""
        if not self.printer_view_getter:
            return False, "PDF printing not available (UI dependency missing)."

        try:
            # 1. Generate HTML Table
            # Pandas can do basic HTML table
            html_table = df.to_html(index=False, classes='report-table', float_format=lambda x: "{:,.0f}".format(x) if isinstance(x, (int, float)) else str(x))
            
            # 2. Wrap in proper HTML with Styles
            period_str = f"{start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')}"
            
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; padding: 20px; }}
                    h1 {{ color: #2b5797; }}
                    .period {{ color: #666; margin-bottom: 20px; }}
                    table.report-table {{ 
                        width: 100%; border-collapse: collapse; font-size: 10px; 
                    }}
                    table.report-table th {{ 
                        background-color: #D7E4BC; border: 1px solid #ccc; padding: 5px; text-align: left;
                    }}
                    table.report-table td {{ 
                        border: 1px solid #ddd; padding: 4px; 
                    }}
                    /* Last row bold (Total) */
                    table.report-table tr:last-child {{ 
                        font-weight: bold; background-color: #f0f0f0; 
                    }}
                </style>
            </head>
            <body>
                <h1>Quarterly Interest Report</h1>
                <div class="period">Period: {period_str}</div>
                {html_table}
                <div style="margin-top:20px; font-size: 9px; color: #999;">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
            </body>
            </html>
            """
            
            # 3. Print
            from PyQt6.QtCore import QMarginsF, QEventLoop
            from PyQt6.QtGui import QPageLayout, QPageSize
            
            web_view = self.printer_view_getter()
            if not web_view:
                 return False, "Printer View not initialized."
                 
            web_view.setHtml(html_content)
            
            loop = QEventLoop()
            
            # Wait for load
            def load_finished(ok):
                loop.quit()
            
            try: web_view.loadFinished.disconnect()
            except: pass
            
            web_view.loadFinished.connect(load_finished)
            # No URL load triggered, setHtml is async? Usually instantaneous but triggers loadFinished.
            # Wait, setHtml triggers loadFinished only if base url provided? 
            # In StatementGenerator we used check:
            # web_view.loadFinished.connect(loop.quit)
            # loop.exec() 
            # This seems correct.
            
            # However, setHtml might not fire loadFinished reliably in all Qt versions if already loaded?
            # It should.
            
            # Let's trust StatementGenerator pattern
            # But wait, QTimer based timeout for safety?
            
            # StatementGenerator implementation:
            # web_view.loadFinished.connect(loop.quit)
            # loop.exec() 
            # ...
            # web_view.page().printToPdf(...)
            
            # Replicate:
            try: web_view.loadFinished.disconnect()
            except: pass
            web_view.loadFinished.connect(loop.quit)
            
            # Wait a bit just in case setHtml is fast/synchronous in some loop (unlikely)
            # Actually running loop.exec() might hang if signal already fired?
            # QWebEngineView setHtml is asynchronous.
            
            # Timeout safety
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(2000, loop.quit) # 2s max wait for load
            
            loop.exec()
            
            # Print
            page_layout = QPageLayout(
                QPageSize(QPageSize.PageSizeId.A4),
                QPageLayout.Orientation.Landscape,
                QMarginsF(10, 10, 10, 10)
            )
            
            def print_finished(path, success):
                loop.quit()
                
            try: web_view.page().pdfPrintingFinished.disconnect()
            except: pass
            
            web_view.page().pdfPrintingFinished.connect(print_finished)
            web_view.page().printToPdf(output_path, page_layout)
            
            loop.exec() # Wait for print
            
            if pdf_print_success:
                msg = "Report generated successfully."
                if warnings:
                    msg += "\n\nWarnings:\n" + "\n".join(sorted(list(warnings)))
                return True, msg
            else:
                return False, f"Export Failed: {pdf_print_message}"
                
        except Exception as e:
            return False, f"Report Generation Failed: {e}"
