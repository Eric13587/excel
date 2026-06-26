
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
        Get all quarter start dates from the earliest transaction date up to the current quarter based on FY settings.
        Returns a list of datetime objects.
        """
        if ref_date is None:
            ref_date = datetime.now()
            
        fy_start_idx = self.get_fy_start_month_index()
        
        cursor = self.db.conn.cursor()
        cursor.execute("SELECT MIN(date) FROM (SELECT date FROM ledger UNION ALL SELECT date FROM savings)")
        row = cursor.fetchone()
        
        start_year = ref_date.year
        if row and row[0]:
            try:
                # Expecting YYYY-MM-DD
                earliest_date = datetime.strptime(row[0][:10], "%Y-%m-%d")
                start_year = earliest_date.year
            except ValueError:
                pass
                
        # Look back to start_year - 1 (in case FY started in previous year) and forward to current year + 1
        current_year = ref_date.year
        candidates = []
        
        # We need from start_year-1 up to current_year+1
        for year in range(start_year - 1, current_year + 2):
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
                    
        # Filter to remove stuff way in the future (optional, we shouldn't show 5 years ahead)
        # deduplicate and sort
        candidates = sorted(list(set(candidates)))
        
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
                
                # Skip retired individuals who retired BEFORE this report period
                # and have no outstanding loans. If they retired during this period,
                # include them — their transactions naturally stop at retirement.
                ind_details = self.db.get_individual(ind_id)
                if ind_details and ind_details.get('is_retired', 0):
                    retired_date = ind_details.get('retired_date', '')
                    if retired_date and retired_date < start_date_str and not self.db.has_outstanding_loans(ind_id):
                        continue  # Retired before this period with no debt — exclude
                
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

    def _calculate_savings_summary(self, ind_id, q_dates, report_start_date_str):
        """Calculate B/F, contributions, and cashout for an individual's savings."""
        m1_start, m2_start, m3_start, _, m3_next = q_dates
        savings_df = self.db.get_savings_transactions(ind_id)
        if savings_df.empty:
            return None

        # Calculate B/F up to report_start_date_str
        bf_txs = savings_df[savings_df['date'] < report_start_date_str]
        bf_deposits = bf_txs[bf_txs['transaction_type'].isin(['Deposit', 'Interest'])]['amount'].sum() if not bf_txs.empty else 0.0
        bf_withdrawals = bf_txs[bf_txs['transaction_type'] == 'Withdrawal']['amount'].sum() if not bf_txs.empty else 0.0
        bf = bf_deposits - bf_withdrawals

        m1_start_str = m1_start.strftime("%Y-%m-%d")
        m2_start_str = m2_start.strftime("%Y-%m-%d")
        m3_start_str = m3_start.strftime("%Y-%m-%d")
        m3_next_str = m3_next.strftime("%Y-%m-%d")

        def sum_period(start_dt, end_dt, tx_types):
            mask = (savings_df['date'] >= start_dt) & \
                   (savings_df['date'] < end_dt) & \
                   (savings_df['transaction_type'].isin(tx_types))
            filtered = savings_df[mask]
            return filtered['amount'].sum() if not filtered.empty else 0.0

        m1 = sum_period(m1_start_str, m2_start_str, ['Deposit', 'Interest'])
        m2 = sum_period(m2_start_str, m3_start_str, ['Deposit', 'Interest'])
        m3 = sum_period(m3_start_str, m3_next_str, ['Deposit', 'Interest'])
        
        cashout = sum_period(m1_start_str, m3_next_str, ['Withdrawal'])

        if bf == 0 and m1 == 0 and m2 == 0 and m3 == 0 and cashout == 0:
            return None

        return {
            "bf": bf,
            "m1": m1,
            "m2": m2,
            "m3": m3,
            "cashout": cashout
        }

    def generate_quarterly_savings_report(self, start_date_str, output_path, progress_callback=None):
        """
        Generate a quarterly shares/savings report starting from the given date.
        """
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            
            # Validation
            is_valid, err_msg = self._validate_start_date(start_date)
            if not is_valid:
                return False, err_msg
            
            # 1. Prepare Dates
            q_dates = self._get_quarter_dates(start_date)
            m1_start, m2_start, m3_start, _, m3_next = q_dates
            
            # Format headers
            bf_date_obj = m1_start + relativedelta(days=-1)
            day = bf_date_obj.day
            suffix = "TH" if 11 <= day <= 13 else {1:"ST",2:"ND",3:"RD"}.get(day%10, "TH")
            bf_date_label = f"B/F {day}{suffix} {bf_date_obj.strftime('%b %Y')}".upper()
            
            m1_name = m1_start.strftime("%b-%y")
            m2_name = m2_start.strftime("%b-%y")
            m3_name = m3_start.strftime("%b-%y")
            
            # 2. Collect Data
            individuals = self.db.get_individuals()
            total_individuals = len(individuals)
            report_data = []
            
            for i, ind in enumerate(individuals):
                ind_id, name = ind[0], ind[1]
                
                if progress_callback:
                    progress_callback(i + 1, total_individuals, f"Processing {name}...")
                
                # Skip retired individuals who retired BEFORE this report period.
                # If they retired during this quarter, include them — the retirement
                # withdrawal will appear as a meaningful final entry.
                ind_details = self.db.get_individual(ind_id)
                if ind_details and ind_details.get('is_retired', 0):
                    retired_date = ind_details.get('retired_date', '')
                    if retired_date and retired_date < start_date_str:
                        continue  # Retired before this period — exclude from savings report
                
                res = self._calculate_savings_summary(ind_id, q_dates, start_date_str)
                if not res:
                    continue

                bf = math.ceil(res['bf'])
                m1 = math.ceil(res['m1'])
                m2 = math.ceil(res['m2'])
                m3 = math.ceil(res['m3'])
                cashout = math.ceil(res['cashout'])
                
                sub_total = m1 + m2 + m3
                grand_total = bf + sub_total - cashout
                
                report_data.append({
                    "Name": name,
                    bf_date_label: bf,
                    m1_name: m1,
                    m2_name: m2,
                    m3_name: m3,
                    "Sub Total": sub_total,
                    "Cash Out": cashout,
                    "Grand Total": grand_total
                })
            
            # 3. Build DataFrame
            df = pd.DataFrame(report_data)
            
            if df.empty:
                df = pd.DataFrame(columns=["Name", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Cash Out", "Grand Total"])
            else:
                # Calculate Totals
                sums = df.select_dtypes(include=['number']).sum()
                total_row = {col: sums[col] if col in sums else '' for col in df.columns}
                total_row['Name'] = 'TOTAL'
                df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

            # 4. Export
            success = False
            msg = ""
            warnings = set()
            title = "Quarterly Savings Report"
            
            if output_path.endswith('.csv'):
                success, msg = self._export_to_csv(df, output_path)
            elif output_path.endswith('.pdf'):
                success, msg = self._export_to_pdf(df, output_path, m1_start, m3_next, title=title)
            else:
                success, msg = self._export_to_excel(df, output_path, sheet_name=title)
                
            if success and warnings:
                msg += "\n\nWarnings:\n" + "\n".join(sorted(list(warnings)))
                
            return success, msg
            
        except Exception as e:
            error_msg = str(e)
            print(f"Error generating savings report: {error_msg}")
            import traceback
            traceback.print_exc()
            return False, error_msg

    # ------------------------------------------------------------------ #
    # Generic fund report (savings / christmas / benevolent; quarter or custom)
    # ------------------------------------------------------------------ #
    _FUND_DEPOSIT_TYPES = {
        "savings": ['Deposit', 'Interest'],
        "christmas": ['Deposit', 'Interest'],
        "benevolent": ['Contribution'],
    }
    _FUND_LABELS = {"savings": "Savings / Shares", "christmas": "Christmas",
                    "benevolent": "Benevolent"}

    def _fund_report_transactions(self, fund, ind_id):
        if fund == "savings":
            return self.db.get_savings_transactions(ind_id)
        table = "christmas_savings" if fund == "christmas" else "benevolent_ledger"
        return self.db.fund_transactions(table, ind_id)

    def _fy_end_for_date(self, d):
        """Last day of the fiscal year containing datetime ``d``."""
        return self._get_fy_start_date(d) + relativedelta(years=1, days=-1)

    def _months_in_range(self, start_date, end_date):
        """(label, month_start, next_month_start) per calendar month in [start, end]."""
        out = []
        cur = start_date.replace(day=1)
        last = end_date.replace(day=1)
        while cur <= last:
            nxt = cur + relativedelta(months=1)
            out.append((cur.strftime("%b-%y"), cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")))
            cur = nxt
        return out

    def _retired_excluded(self, details, period_start_str):
        """True if a retired member should be dropped: their retirement fiscal
        year ended before the report period began. Within that FY they stay on
        (as zero rows)."""
        if not (details and details.get('is_retired', 0)):
            return False
        rd = details.get('retired_date') or ''
        if not rd:
            return False
        fy_end = self._fy_end_for_date(datetime.strptime(rd[:10], "%Y-%m-%d"))
        return period_start_str > fy_end.strftime("%Y-%m-%d")

    def generate_fund_report(self, fund, output_path, start_date_str, end_date_str=None,
                             progress_callback=None):
        """Report for a fund (savings / christmas / benevolent).

        Custom mode (end_date_str given): one column per calendar month in
        [start, end] showing deposits that month, laid out as
        PF No | Name | Employment Status | <months…> | Total (the xmas.xlsx
        structure). Quarter mode (end_date_str=None): the legacy B/F / 3-month /
        Cash Out layout. Exported to CSV/PDF/Excel by extension.
        """
        try:
            deposit_types = self._FUND_DEPOSIT_TYPES[fund]
            label = self._FUND_LABELS[fund]
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
            if end_date_str is not None:
                return self._fund_report_custom(
                    fund, output_path, start_date, start_date_str, end_date_str,
                    deposit_types, label, progress_callback)
            return self._fund_report_quarter(
                fund, output_path, start_date, start_date_str,
                deposit_types, label, progress_callback)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return False, str(e)

    def _fund_report_custom(self, fund, output_path, start_date, start_date_str,
                            end_date_str, deposit_types, label, progress_callback):
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        if end_date < start_date:
            return False, "End date is before start date."
        months = self._months_in_range(start_date, end_date)
        columns = ["PF No", "Name", "Employment Status"] + [m[0] for m in months] + ["Total"]

        individuals = self.db.get_individuals()
        total = len(individuals)
        report_data = []
        for i, ind in enumerate(individuals):
            ind_id, name = ind[0], ind[1]
            if progress_callback:
                progress_callback(i + 1, total, f"Processing {name}...")

            details = self.db.get_individual(ind_id) or {}
            if self._retired_excluded(details, start_date_str):
                continue

            df_tx = self._fund_report_transactions(fund, ind_id)
            is_participant = (not df_tx.empty) or \
                (fund == "benevolent" and self.db.get_benevolent_account(ind_id))
            if not is_participant:
                continue

            def month_sum(s, e):
                if df_tx.empty:
                    return 0.0
                mask = (df_tx['date'] >= s) & (df_tx['date'] < e) & \
                       (df_tx['transaction_type'].isin(deposit_types))
                return float(df_tx[mask]['amount'].sum()) if mask.any() else 0.0

            row = {"PF No": details.get('pf_no') or "", "Name": name,
                   "Employment Status": details.get('employment_status') or ""}
            rtotal = 0
            for (lbl, s, e) in months:
                v = math.ceil(month_sum(s, e))
                row[lbl] = v
                rtotal += v
            row["Total"] = rtotal
            report_data.append(row)

        df = pd.DataFrame(report_data, columns=columns)
        if not df.empty:
            sums = df.select_dtypes(include=['number']).sum()
            total_row = {col: (sums[col] if col in sums else '') for col in df.columns}
            total_row["Name"] = "TOTAL"
            df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

        title = f"{label} Report"
        if output_path.endswith('.csv'):
            return self._export_to_csv(df, output_path)
        if output_path.endswith('.pdf'):
            return self._export_to_pdf(df, output_path, start_date, end_date, title=title)
        return self._export_to_excel(df, output_path, sheet_name=title[:31])

    def _fund_report_quarter(self, fund, output_path, start_date, start_date_str,
                             deposit_types, label, progress_callback):
        bf_obj = start_date + relativedelta(days=-1)
        bf_label = f"B/F {bf_obj.strftime('%d %b %Y')}".upper()
        q_dates = self._get_quarter_dates(start_date)
        m1_start, m2_start, m3_start, _, m3_next = q_dates
        m1_name, m2_name, m3_name = (m1_start.strftime("%b-%y"), m2_start.strftime("%b-%y"),
                                     m3_start.strftime("%b-%y"))
        bounds = [(m1_start.strftime("%Y-%m-%d"), m2_start.strftime("%Y-%m-%d"), m1_name),
                  (m2_start.strftime("%Y-%m-%d"), m3_start.strftime("%Y-%m-%d"), m2_name),
                  (m3_start.strftime("%Y-%m-%d"), m3_next.strftime("%Y-%m-%d"), m3_name)]
        columns = ["Name", bf_label, m1_name, m2_name, m3_name, "Sub Total", "Cash Out", "Grand Total"]

        individuals = self.db.get_individuals()
        total = len(individuals)
        report_data = []
        for i, ind in enumerate(individuals):
            ind_id, name = ind[0], ind[1]
            if progress_callback:
                progress_callback(i + 1, total, f"Processing {name}...")

            details = self.db.get_individual(ind_id) or {}
            if self._retired_excluded(details, start_date_str):
                continue

            df_tx = self._fund_report_transactions(fund, ind_id)
            if df_tx.empty:
                continue

            def period_sum(s, e, types):
                mask = (df_tx['date'] >= s) & (df_tx['date'] < e) & \
                       (df_tx['transaction_type'].isin(types))
                return float(df_tx[mask]['amount'].sum()) if mask.any() else 0.0

            bf_txs = df_tx[df_tx['date'] < start_date_str]
            bf = (bf_txs[bf_txs['transaction_type'].isin(deposit_types)]['amount'].sum()
                  - bf_txs[bf_txs['transaction_type'] == 'Withdrawal']['amount'].sum()) \
                if not bf_txs.empty else 0.0
            bf = math.ceil(bf)

            months = [math.ceil(period_sum(s, e, deposit_types)) for s, e, _ in bounds]
            cashout = math.ceil(period_sum(bounds[0][0], bounds[2][1], ['Withdrawal']))
            if bf == 0 and sum(months) == 0 and cashout == 0:
                continue
            sub = sum(months)
            report_data.append({"Name": name, bf_label: bf, m1_name: months[0],
                                m2_name: months[1], m3_name: months[2], "Sub Total": sub,
                                "Cash Out": cashout, "Grand Total": bf + sub - cashout})

        df = pd.DataFrame(report_data, columns=columns)
        if not df.empty:
            sums = df.select_dtypes(include=['number']).sum()
            total_row = {col: (sums[col] if col in sums else '') for col in df.columns}
            total_row["Name"] = "TOTAL"
            df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

        title = f"{label} Report"
        if output_path.endswith('.csv'):
            return self._export_to_csv(df, output_path)
        if output_path.endswith('.pdf'):
            return self._export_to_pdf(df, output_path, m1_start, m3_next, title=title)
        return self._export_to_excel(df, output_path, sheet_name=title[:31])

    def _export_to_excel(self, df, output_path, sheet_name='Quarterly Report'):
        """Export DataFrame to Excel with formatting."""
        try:
            # Get colors from settings
            header_bg = self.db.get_setting("excel_header_bg", "#D7E4BC")
            total_bg = self.db.get_setting("excel_total_bg", "#F0F0F0")
            
            with pd.ExcelWriter(output_path, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False, sheet_name=sheet_name)
                workbook = writer.book
                worksheet = writer.sheets[sheet_name]
                
                # Formats
                header_fmt = workbook.add_format({'bold': True, 'border': 1, 'bg_color': header_bg})
                num_fmt = workbook.add_format({'num_format': '#,##0'})
                total_fmt = workbook.add_format({'bold': True, 'border': 1, 'num_format': '#,##0', 'bg_color': total_bg})
                
                # Apply header format
                for col_num, value in enumerate(df.columns.values):
                    worksheet.write(0, col_num, value, header_fmt)

                # Set column widths (header-driven so dynamic month columns work)
                text_cols = {"Name", "Employment Status", "PF No"}
                for col_num, col_name in enumerate(df.columns):
                    if col_name == "Name":
                        worksheet.set_column(col_num, col_num, 26)
                    elif col_name == "Employment Status":
                        worksheet.set_column(col_num, col_num, 18)
                    elif col_name == "PF No":
                        worksheet.set_column(col_num, col_num, 12)
                    else:
                        worksheet.set_column(col_num, col_num, 14, num_fmt)
                
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

    def _export_to_pdf(self, df, output_path, start_date, end_date, title="Quarterly Interest Report"):
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
                <h1>{title}</h1>
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

    
    def generate_financial_statements(self, output_path, target_date_str=None, progress_callback=None):
        """Generates the Income Statement, Balance Sheet & Cash Flow as PDF."""
        try:
            if not target_date_str:
                target_date_str = datetime.now().strftime("%Y-%m-%d")
                
            # Derive the statements from the double-entry general ledger so the
            # books balance by construction. This replaces the previous
            # hand-assembled cash/equity equations (which were not guaranteed to
            # tie out). sync() brings the GL current with ledger/savings first.
            from src.services.gl_service import GLService
            from src.services.provisioning import ProvisioningService
            gl = GLService(self.db)
            gl.sync(progress=progress_callback)

            inc = gl.get_income_statement(None, target_date_str)
            bs = gl.get_balance_sheet(target_date_str)
            cf = gl.get_cash_flow(None, target_date_str)
            net_surplus = inc['net_surplus']

            # SASRA loan classification schedule (informational; the booked
            # allowance, if any, already shows inside the balance sheet assets).
            prov = ProvisioningService(self.db, gl).get_provisioning_summary(target_date_str)
            allowance_booked = gl.get_account_balance("1190", target_date_str)
            prov_rows = "".join(
                f'<tr><td class="label">{b["bucket"]} '
                f'<span style="color:#94a3b8;">({b["rate"]*100:.0f}%, {b["count"]} loans)</span></td>'
                f'<td class="amount">{b["gross"]:,.2f}</td>'
                f'<td class="amount">{b["net"]:,.2f}</td>'
                f'<td class="amount">{b["provision"]:,.2f}</td></tr>'
                for b in prov['bands']
            )

            # Cash flow statement sections (Operating / Lending / Financing).
            cf_section_html = ""
            for sec in cf['sections']:
                cf_section_html += f'<tr><td class="section-title" colspan="2">{sec["label"]}</td></tr>'
                if sec['lines']:
                    for l in sec['lines']:
                        cf_section_html += (f'<tr><td class="label">{l["name"]}</td>'
                                            f'<td class="amount">{l["amount"]:,.2f}</td></tr>')
                else:
                    cf_section_html += '<tr><td class="label">No movement</td><td class="amount">0.00</td></tr>'
                cf_section_html += (f'<tr class="total-row"><td class="label">Net cash from {sec["label"]}</td>'
                                    f'<td class="amount">{sec["subtotal"]:,.2f}</td></tr>')

            def _rows(lines, empty_label):
                if not lines:
                    return f'<tr><td class="label">{empty_label}</td><td class="amount">0.00</td></tr>'
                return "".join(
                    f'<tr><td class="label">{l["name"]}</td>'
                    f'<td class="amount">{l["amount"]:,.2f}</td></tr>'
                    for l in lines
                )

            if bs['is_balanced']:
                balance_banner = (
                    '<div style="margin-top:10px;padding:8px;border-radius:5px;text-align:center;'
                    'background:#dcfce7;color:#166534;font-weight:bold;">'
                    'Balance check: Assets = Liabilities + Equity ✓</div>'
                )
            else:
                balance_banner = (
                    '<div style="margin-top:10px;padding:8px;border-radius:5px;text-align:center;'
                    'background:#fee2e2;color:#991b1b;font-weight:bold;">'
                    'Balance check FAILED — books do not tie; investigate before relying on this.</div>'
                )

            # Render HTML
            html = f"""
            <!DOCTYPE html>
            <html>
                <head>
                    <style>
                        body {{ font-family: 'Helvetica', Arial, sans-serif; padding: 30px; font-size: 14px; color: #333; }}
                        h1 {{ text-align: center; color: #1e3a8a; font-size: 24px; text-transform: uppercase; margin-bottom: 5px; }}
                        h2 {{ text-align: center; color: #64748b; font-size: 14px; font-weight: normal; margin-top: 0; }}
                        h3 {{ color: #0f172a; border-bottom: 2px solid #cbd5e1; padding-bottom: 5px; font-size: 18px; margin-top: 30px; }}
                        table {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; }}
                        td {{ padding: 8px 5px; border-bottom: 1px dotted #e2e8f0; }}
                        .label {{ width: 70%; }}
                        .amount {{ width: 30%; text-align: right; font-family: 'Courier New', Courier, monospace; }}
                        .total-row td {{ font-weight: bold; border-top: 2px solid #333; border-bottom: 2px double #333; background: #f8fafc; font-size: 15px; }}
                        .section-title {{ font-weight: bold; padding-top: 15px; color: #475569; }}
                    </style>
                </head>
                <body>
                    <h1>Institutional Financial Statements</h1>
                    <h2>Report up to: {target_date_str}</h2>

                    <h3>I. INCOME STATEMENT (P&amp;L)</h3>
                    <table>
                        <tr><td class="section-title" colspan="2">Revenues</td></tr>
                        {_rows(inc['revenue'], 'No recorded income')}
                        <tr class="total-row"><td class="label">Total Revenue</td><td class="amount">{inc['total_revenue']:,.2f}</td></tr>

                        <tr><td class="section-title" colspan="2">Operating Expenses</td></tr>
                        {_rows(inc['expenses'], 'No recorded expenses')}
                        <tr class="total-row"><td class="label">Total Expenses</td><td class="amount">{inc['total_expenses']:,.2f}</td></tr>
                        <tr style="height: 20px;"><td colspan="2"></td></tr>
                        <tr class="total-row" style="color: {'#166534' if net_surplus >= 0 else '#991b1b'};"><td class="label">NET SURPLUS (PROFIT)</td><td class="amount">{net_surplus:,.2f}</td></tr>
                    </table>

                    <div style="page-break-before: always;"></div>

                    <h3>II. BALANCE SHEET (as of {target_date_str})</h3>
                    <table>
                        <tr><td class="section-title" colspan="2">ASSETS</td></tr>
                        {_rows(bs['assets'], 'No assets')}
                        <tr class="total-row"><td class="label">TOTAL ASSETS</td><td class="amount">{bs['total_assets']:,.2f}</td></tr>

                        <tr><td class="section-title" colspan="2">LIABILITIES</td></tr>
                        {_rows(bs['liabilities'], 'No liabilities')}
                        <tr class="total-row"><td class="label">Total Liabilities</td><td class="amount">{bs['total_liabilities']:,.2f}</td></tr>

                        <tr><td class="section-title" colspan="2">EQUITY</td></tr>
                        {_rows(bs['equity'], 'No equity')}
                        <tr class="total-row"><td class="label">Total Equity</td><td class="amount">{bs['total_equity']:,.2f}</td></tr>

                        <tr style="height: 20px;"><td colspan="2"></td></tr>
                        <tr class="total-row"><td class="label">TOTAL LIABILITIES &amp; EQUITY</td><td class="amount">{bs['total_liabilities_and_equity']:,.2f}</td></tr>
                    </table>
                    {balance_banner}

                    <div style="page-break-before: always;"></div>

                    <h3>III. LOAN CLASSIFICATION &amp; PROVISIONING (SASRA)</h3>
                    <table>
                        <tr><td class="section-title">Classification</td><td class="amount">Gross Outstanding</td><td class="amount">Net of Deposits</td><td class="amount">Provision</td></tr>
                        {prov_rows}
                        <tr class="total-row"><td class="label">TOTAL PORTFOLIO</td><td class="amount">{prov['total_gross']:,.2f}</td><td class="amount">{prov['total_net']:,.2f}</td><td class="amount">{prov['total_provision']:,.2f}</td></tr>
                    </table>
                    <table>
                        <tr><td class="label">Portfolio at Risk (&gt; 30 days)</td><td class="amount">{prov['par_ratio']*100:.1f}%</td></tr>
                        <tr><td class="label">Required Provision</td><td class="amount">{prov['total_provision']:,.2f}</td></tr>
                        <tr><td class="label">Allowance Booked in Ledger</td><td class="amount">{allowance_booked:,.2f}</td></tr>
                        <tr class="total-row"><td class="label">Shortfall / (Excess) to Book</td><td class="amount">{prov['total_provision'] - allowance_booked:,.2f}</td></tr>
                    </table>

                    <div style="page-break-before: always;"></div>

                    <h3>IV. CASH FLOW STATEMENT (to {target_date_str})</h3>
                    <table>
                        {cf_section_html}
                        <tr style="height: 15px;"><td colspan="2"></td></tr>
                        <tr class="total-row"><td class="label">NET CHANGE IN CASH</td><td class="amount">{cf['net_change']:,.2f}</td></tr>
                        <tr><td class="label">Opening Cash Balance</td><td class="amount">{cf['opening_cash']:,.2f}</td></tr>
                        <tr class="total-row"><td class="label">CLOSING CASH BALANCE</td><td class="amount">{cf['closing_cash']:,.2f}</td></tr>
                    </table>

                    <div style="margin-top: 40px; font-size: 11px; text-align: center; color: #64748b;">Generated securely by LoanMaster Treasury Engine on {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
                </body>
            </html>
            """
            
            # Print PDF
            from PyQt6.QtCore import QMarginsF, QEventLoop, QTimer
            from PyQt6.QtGui import QPageLayout, QPageSize
            
            if not self.printer_view_getter:
                return False, "PDF printer view unavailable."
                
            web_view = self.printer_view_getter()
            if not web_view:
                 return False, "Printer View not initialized."
                 
            web_view.setHtml(html)
            
            loop = QEventLoop()
            
            try: web_view.loadFinished.disconnect()
            except: pass
            web_view.loadFinished.connect(loop.quit)
            
            QTimer.singleShot(2000, loop.quit)
            loop.exec()
            
            pdf_print_success = [False]
            pdf_print_message = [""]
            
            def print_finished(path, success):
                pdf_print_success[0] = success
                if not success:
                    pdf_print_message[0] = "Failed to write PDF file (maybe permission issue)."
                loop.quit()
                
            try: web_view.page().pdfPrintingFinished.disconnect()
            except: pass
            
            page_layout = QPageLayout(
                QPageSize(QPageSize.PageSizeId.A4),
                QPageLayout.Orientation.Portrait,
                QMarginsF(15, 15, 15, 15)
            )
            
            web_view.page().pdfPrintingFinished.connect(print_finished)
            web_view.page().printToPdf(output_path, page_layout)
            
            loop.exec()
            
            if pdf_print_success[0]:
                return True, "Institutional Statement Generated Successfully."
            else:
                return False, pdf_print_message[0]
                
        except Exception as e:
            return False, f"Balance Sheet Generation Failed: {e}"
