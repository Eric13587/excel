
"""
Report generation module for LoanMaster.
Handles calculation and export of Quarterly Interest Reports.
"""
import logging
import math

import pandas as pd
from datetime import datetime
from dateutil.relativedelta import relativedelta

from src import branding

logger = logging.getLogger(__name__)

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
        fy_start_month_name = self.db.settings.fy_start_month
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
        fy_start_month_name = self.db.settings.fy_start_month
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
        
        earliest = self.db.ledger.earliest_date_overall()

        start_year = ref_date.year
        if earliest:
            try:
                # Expecting YYYY-MM-DD
                earliest_date = datetime.strptime(earliest[:10], "%Y-%m-%d")
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
            
        fy_start_month_name = self.db.settings.fy_start_month
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

    def generate_quarterly_report(self, start_date_str, output_path, columns=None, progress_callback=None):
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
            m1_start, m2_start, m3_start, _, m3_next = q_dates
            
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
            if columns:
                df = pd.DataFrame(report_data, columns=columns)
            else:
                default_cols = ["Name", "Loan Amount", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Grand Total"]
                df = pd.DataFrame(report_data, columns=default_cols)
            
            if df.empty:
                df = pd.DataFrame(columns=columns or ["Name", "Loan Amount", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Grand Total"])
            else:
                # Calculate Totals
                sums = df.select_dtypes(include=['number']).sum()
                total_row = {col: sums[col] if col in sums else '' for col in df.columns}
                if 'Name' in df.columns:
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
            logger.exception("Error generating report")
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

    def generate_quarterly_savings_report(self, start_date_str, output_path, columns=None, progress_callback=None):
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
            if columns:
                df = pd.DataFrame(report_data, columns=columns)
            else:
                default_cols = ["Name", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Cash Out", "Grand Total"]
                df = pd.DataFrame(report_data, columns=default_cols)
            
            if df.empty:
                df = pd.DataFrame(columns=columns or ["Name", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Cash Out", "Grand Total"])
            else:
                # Calculate Totals
                sums = df.select_dtypes(include=['number']).sum()
                total_row = {col: sums[col] if col in sums else '' for col in df.columns}
                if 'Name' in df.columns:
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
            logger.exception("Error generating savings report")
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
                             columns=None, progress_callback=None):
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
                    deposit_types, label, chosen_columns=columns, progress_callback=progress_callback)
            return self._fund_report_quarter(
                fund, output_path, start_date, start_date_str,
                deposit_types, label, chosen_columns=columns, progress_callback=progress_callback)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return False, str(e)

    def _fund_report_custom(self, fund, output_path, start_date, start_date_str,
                            end_date_str, deposit_types, label, chosen_columns=None, progress_callback=None):
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
        if end_date < start_date:
            return False, "End date is before start date."
        months = self._months_in_range(start_date, end_date)
        default_cols = ["PF No", "Name", "Employment Status"] + [m[0] for m in months] + ["Total"]
        cols_to_use = chosen_columns if chosen_columns is not None else default_cols

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

            def month_sum(s, e, df_tx=df_tx):
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

        df = pd.DataFrame(report_data, columns=cols_to_use)
        if not df.empty:
            sums = df.select_dtypes(include=['number']).sum()
            total_row = {col: (sums[col] if col in sums else '') for col in df.columns}
            if 'Name' in df.columns:
                total_row["Name"] = "TOTAL"
            df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

        title = f"{label} Report"
        if output_path.endswith('.csv'):
            return self._export_to_csv(df, output_path)
        if output_path.endswith('.pdf'):
            return self._export_to_pdf(df, output_path, start_date, end_date, title=title)
        return self._export_to_excel(df, output_path, sheet_name=title[:31])

    def _fund_report_quarter(self, fund, output_path, start_date, start_date_str,
                             deposit_types, label, chosen_columns=None, progress_callback=None):
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

            def period_sum(s, e, types, df_tx=df_tx):
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

        df = pd.DataFrame(report_data, columns=chosen_columns if chosen_columns is not None else columns)
        if not df.empty:
            sums = df.select_dtypes(include=['number']).sum()
            total_row = {col: (sums[col] if col in sums else '') for col in df.columns}
            if 'Name' in df.columns:
                total_row["Name"] = "TOTAL"
            df = pd.concat([df, pd.DataFrame([total_row])], ignore_index=True)

        title = f"{label} Report"
        if output_path.endswith('.csv'):
            return self._export_to_csv(df, output_path)
        if output_path.endswith('.pdf'):
            return self._export_to_pdf(df, output_path, m1_start, m3_next, title=title)
        return self._export_to_excel(df, output_path, sheet_name=title[:31])

    # ------------------------------------------------------------------ #
    # Members list (directory) with user-selected columns
    # ------------------------------------------------------------------ #
    # (key, label) in display order. The dialog lets the user pick any subset.
    MEMBER_LIST_COLUMNS = [
        ("name", "Name"),
        ("pf_no", "PF No"),
        ("id_no", "ID No"),
        ("phone", "Phone"),
        ("email", "Email"),
        ("employment_status", "Employment Status"),
        ("is_retired", "Retired"),
        ("retired_date", "Retired Date"),
        ("default_deduction", "Monthly Deduction"),
        ("savings", "Savings Balance"),
        ("loan_balance", "Loan Balance"),
        ("christmas", "Christmas Balance"),
        ("benevolent", "Benevolent Total"),
        ("created_at", "Created"),
    ]

    def _member_column_value(self, key, ind_id, d):
        if key == "name":
            return d.get("name", "")
        if key in ("pf_no", "id_no", "phone", "email", "retired_date", "created_at"):
            return d.get(key) or ""
        if key == "employment_status":
            return d.get("employment_status") or "Active"
        if key == "is_retired":
            return "Yes" if d.get("is_retired") else "No"
        if key == "default_deduction":
            return math.ceil(d.get("default_deduction") or 0)
        if key == "savings":
            return math.ceil(self.db.get_savings_balance(ind_id))
        if key == "loan_balance":
            return math.ceil(sum((l.get("balance") or 0) for l in self.db.get_active_loans(ind_id)))
        if key == "christmas":
            return math.ceil(self.db.fund_balance("christmas_savings", ind_id))
        if key == "benevolent":
            return math.ceil(self.db.fund_balance("benevolent_ledger", ind_id))
        return ""

    def generate_members_list(self, output_path, columns, progress_callback=None):
        """Export a members directory with the chosen columns (keys from
        MEMBER_LIST_COLUMNS, in the given order). CSV/PDF/Excel by extension."""
        labels = dict(self.MEMBER_LIST_COLUMNS)
        columns = [c for c in columns if c in labels] or ["name"]
        headers = [labels[c] for c in columns]

        individuals = self.db.get_individuals()
        total = len(individuals)
        rows = []
        for i, ind in enumerate(individuals):
            ind_id = ind[0]
            if progress_callback:
                progress_callback(i + 1, total, f"Processing {ind[1]}...")
            d = self.db.get_individual(ind_id) or {}
            rows.append({labels[c]: self._member_column_value(c, ind_id, d) for c in columns})

        df = pd.DataFrame(rows, columns=headers)
        df = df.sort_values(by="Name") if "Name" in df.columns and not df.empty else df

        title = "Members List"
        if output_path.endswith(".csv"):
            return self._export_to_csv(df, output_path)
        if output_path.endswith(".pdf"):
            return self._export_to_pdf(df, output_path, None, None, title=title, has_total_row=False)
        return self._export_to_excel(df, output_path, sheet_name=title, has_total_row=False)

    def _export_to_excel(self, df, output_path, sheet_name='Quarterly Report', has_total_row=True):
        """Export DataFrame to Excel with formatting.

        has_total_row: when True the last row is styled as a bold/shaded TOTAL
        (the financial reports append one); set False for plain listings like the
        members directory so the last member isn't darkened.
        """
        try:
            # Get colors from settings
            header_bg = self.db.settings.excel_header_bg
            total_bg = self.db.settings.excel_total_bg
            
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
                for col_num, col_name in enumerate(df.columns):
                    if col_name == "Name":
                        worksheet.set_column(col_num, col_num, 26)
                    elif col_name == "Employment Status":
                        worksheet.set_column(col_num, col_num, 18)
                    elif col_name == "PF No":
                        worksheet.set_column(col_num, col_num, 12)
                    else:
                        worksheet.set_column(col_num, col_num, 14, num_fmt)
                
                # Apply Totals Row Format (only when the df actually ends in one)
                if has_total_row and not df.empty:
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

    def _export_to_pdf(self, df, output_path, start_date, end_date, title="Quarterly Interest Report",
                       has_total_row=True):
        """Export DataFrame to PDF via HTML and QWebEngineView."""
        if not self.printer_view_getter:
            return False, "PDF printing not available (UI dependency missing)."

        try:
            # 1. Generate HTML Table
            # Pandas can do basic HTML table
            html_table = df.to_html(index=False, classes='report-table', float_format=lambda x: "{:,.0f}".format(x) if isinstance(x, (int, float)) else str(x))
            
            # 2. Wrap in proper HTML with the shared letterhead + house style
            period_str = ""
            if start_date is not None and end_date is not None:
                period_str = f"Period: {start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')}"
            # Only emphasise the last row when it's actually a TOTAL row.
            last_row_css = ("table.report-table tr:last-child td "
                            "{ font-weight: bold; background-color: #f8fafc; "
                            "border-top: 2px solid #334155; }"
                            if has_total_row else "")

            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; padding: 20px; color: #1F2937; }}
                    {branding.LETTERHEAD_CSS}
                    table.report-table {{
                        width: 100%; border-collapse: collapse; font-size: 10px;
                    }}
                    table.report-table th {{
                        background-color: #f1f5f9; color: #0f172a; border: 1px solid #cbd5e1;
                        border-bottom: 2px solid #2563EB; padding: 5px; text-align: left;
                    }}
                    table.report-table td {{
                        border: 1px solid #e2e8f0; padding: 4px;
                    }}
                    {last_row_css}
                </style>
            </head>
            <body>
                {branding.letterhead_html(self.db, title, period_str)}
                {html_table}
                <div style="margin-top:20px; font-size: 9px; color: #94a3b8;">Generated on {datetime.now().strftime('%Y-%m-%d %H:%M')}</div>
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
            from PyQt6.QtCore import QTimer

            # Wait for the (asynchronous) setHtml load, with a timeout so a
            # failed load can never hang the UI. The timer is stopped before
            # the print wait below so it cannot cut that wait short.
            try:
                web_view.loadFinished.disconnect()
            except Exception:
                pass
            web_view.loadFinished.connect(loop.quit)
            load_timer = QTimer()
            load_timer.setSingleShot(True)
            load_timer.timeout.connect(loop.quit)
            load_timer.start(5000)
            loop.exec()
            load_timer.stop()

            # Print
            page_layout = QPageLayout(
                QPageSize(QPageSize.PageSizeId.A4),
                QPageLayout.Orientation.Landscape,
                QMarginsF(10, 10, 10, 10)
            )

            pdf_result = {"success": False}

            def print_finished(path, success):
                pdf_result["success"] = bool(success)
                loop.quit()

            try:
                web_view.page().pdfPrintingFinished.disconnect()
            except Exception:
                pass

            web_view.page().pdfPrintingFinished.connect(print_finished)
            web_view.page().printToPdf(output_path, page_layout)

            # PDF rendering of a large table can take a while; time out rather
            # than hang forever if Qt never fires pdfPrintingFinished.
            print_timer = QTimer()
            print_timer.setSingleShot(True)
            print_timer.timeout.connect(loop.quit)
            print_timer.start(60000)
            loop.exec()
            print_timer.stop()

            if pdf_result["success"]:
                return True, "Report generated successfully."
            return False, "Export Failed: PDF file could not be written (check the path is writable and not open elsewhere)."
                
        except Exception as e:
            return False, f"Report Generation Failed: {e}"

    
