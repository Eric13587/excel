"""Statement generator for LoanMaster.

This module handles PDF and Excel statement generation, extracted from
the Dashboard view for better separation of concerns.
"""
import os
import re
import math
from datetime import datetime

# Optional dependencies
try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:
    _PANDAS_AVAILABLE = False

from src.config import DEFAULT_INTEREST_RATE


class StatementGenerator:
    """Generates PDF and Excel statements for individuals.
    
    This class encapsulates all statement generation logic previously
    embedded in the Dashboard view.
    """
    
    def __init__(self, db_manager, printer_view_getter=None):
        """Initialize StatementGenerator.
        
        Args:
            db_manager: DatabaseManager instance for data access.
            printer_view_getter: Optional callable that returns a QWebEngineView
                for PDF generation. If None, HTML fallback is used.
        """
        self.db = db_manager
        self._get_printer_view = printer_view_getter
    
    @staticmethod
    def clean_notes(note):
        """Clean notes by removing auto-generated markers."""
        if not note:
            return ""
        cleaned = re.sub(r'\s*\(Auto\)', '', str(note))
        cleaned = re.sub(r'\s*\(Catch-up\)', '', cleaned)
        return cleaned.strip()
    
    def _get_individual_info(self, ind_id):
        """Get individual information.
        
        Returns:
            Tuple of (name, phone, email) or (None, None, None) if not found.
        """
        individuals = self.db.get_individuals()
        for ind in individuals:
            if ind[0] == ind_id:
                return ind[1], ind[2], ind[3] if len(ind) > 3 else ""
        return None, None, None
    
    def _get_account_status(self, ind_id):
        """Determine account active status.
        
        Returns:
            Tuple of (is_active, status_str, status_color).
        """
        active_loans = self.db.get_active_loans(ind_id)
        savings_balance = self.db.get_savings_balance(ind_id)
        is_active = (len(active_loans) > 0) or (savings_balance != 0)
        
        status_str = "Active" if is_active else "Inactive"
        status_color = "#28a745" if is_active else "#dc3545"
        
        return is_active, status_str, status_color
    
    def _generate_pdf_html(self, ind_id, name, phone, from_date, to_date,
                           status_str, status_color, from_display, to_display):
        """Generate HTML content for PDF statement.
        
        Returns:
            Tuple of (html_content, total_balance, total_gross_balance, savings_balance).
        """
        df = self.db.get_ledger(ind_id)
        savings_df = self.db.get_savings_transactions(ind_id, from_date, to_date)
        
        if df.empty and savings_df.empty:
            return None, 0, 0, 0
        
        savings_balance = self.db.get_savings_balance(ind_id)
        total_balance = 0.0
        total_gross_balance = 0.0
        
        # Generate HTML with landscape layout
        html = """<!DOCTYPE html>
<html><head>
<style>
    @page { size: landscape; margin: 15mm; }
    body { font-family: Arial, sans-serif; margin: 0; padding: 10px; font-size: 9px; }
    .header { text-align: center; border-bottom: 3px solid #2b5797; padding-bottom: 10px; margin-bottom: 15px; }
    .header h1 { color: #2b5797; margin: 0; font-size: 22px; }
    .header h2 { color: #333; margin: 5px 0 0 0; font-size: 16px; font-weight: normal; }
    .header .period { color: #666; font-size: 11px; margin-top: 5px; }
    .client-info { background: #f5f5f5; padding: 8px; margin-bottom: 15px; border-radius: 5px; display: flex; justify-content: space-between; }
    .client-info .left { }
    .client-info .right { text-align: right; }
    .client-info p { margin: 2px 0; }
    .main-container { display: flex; gap: 20px; }
    .loans-column { flex: 1; }
    .savings-column { flex: 0.6; }
    .section-title { background: #2b5797; color: white; padding: 6px 10px; font-size: 11px; font-weight: bold; margin-bottom: 0; }
    .savings-title { background: #28a745; }
    table { width: 100%; border-collapse: collapse; font-size: 8px; margin-bottom: 10px; }
    th { background: #e0e0e0; padding: 4px; text-align: left; border: 1px solid #ccc; font-size: 7px; }
    td { padding: 3px 4px; border: 1px solid #ddd; }
    .loan-box { margin-bottom: 12px; }
    .summary-row { display: flex; justify-content: space-between; margin-top: 15px; padding-top: 10px; border-top: 2px solid #2b5797; }
    .summary-item { font-weight: bold; font-size: 11px; }
    .summary-loans { color: #d9534f; }
    .summary-savings { color: #28a745; }
    .footer { margin-top: 15px; text-align: center; font-size: 8px; color: #999; }
</style>
</head><body>"""
        
        html += f"""<div class="header">
    <h1>ACCOUNT STATEMENT</h1>
    <h2>{name}</h2>
    <div class="period">For the period: {from_display} to {to_display}</div>
</div>
<div class="client-info">
    <div class="left">
        <p><strong>Client Name:</strong> {name}</p>
        <p><strong>Contact:</strong> {phone}</p>
    </div>
    <div class="right">
        <p><strong>Statement Date:</strong> {datetime.now().strftime('%B %d, %Y')}</p>
        <p><strong>Account Status:</strong> <span style="color:{status_color};font-weight:bold;">{status_str}</span></p>
    </div>
</div>
<div class="main-container">
    <div class="loans-column">
        <div class="section-title">LOANS</div>"""
        
        if not df.empty:
            df['loan_id'] = df['loan_id'].fillna('-')
            loan_groups = df.groupby('loan_id')
            
            for loan_ref in sorted(loan_groups.groups.keys()):
                group = loan_groups.get_group(loan_ref).sort_values(by=['id'])
                html += f"""<div class="loan-box">
                <table><thead><tr><th colspan="8" style="background:#2b5797;color:white;">Loan: {loan_ref}</th></tr>
                <tr><th>Date</th><th>Type</th><th>Debit</th><th>Interest</th><th>Credit</th><th>Balance</th><th>Gross Bal</th><th>Remarks</th></tr></thead><tbody>"""
                
                current_gross = 0.0
                
                for _, row in group.iterrows():
                    interest = math.ceil(float(row.get('interest_amount', 0)))
                    notes = self.clean_notes(row['notes'])
                    
                    event = row['event_type']
                    added = float(row['added'])
                    deducted = float(row['deducted'])
                    
                    show_gross = False
                    if event == "Loan Issued" or event == "Loan Top-Up":
                        current_gross += added * (1 + DEFAULT_INTEREST_RATE)
                        show_gross = True
                    if event == "Repayment" or event == "Loan Buyoff":
                        current_gross -= deducted
                        show_gross = True
                    
                    if row['date'] >= from_date and row['date'] <= to_date:
                        gross_display = f"{current_gross:,.0f}" if show_gross else ""
                        balance_display = math.ceil(float(row['balance']))
                        
                        html += f"<tr><td>{row['date']}</td><td>{row['event_type']}</td><td>{math.ceil(added):,}</td><td>{interest:,}</td><td>{math.ceil(deducted):,}</td><td>{balance_display:,}</td><td>{gross_display}</td><td>{notes}</td></tr>"
                
                html += "</tbody></table></div>"
                if not group.empty:
                    total_balance += float(group.iloc[-1]['balance'])
                    total_gross_balance += current_gross
        else:
            html += "<p style='padding:10px;color:#666;'>No loan transactions in this period</p>"
        
        html += """</div>
    <div class="savings-column">
        <div class="section-title savings-title">SAVINGS / SHARES</div>"""
        
        if not savings_df.empty:
            savings_df['date'] = savings_df['date'].astype(str)
            savings_df = savings_df.sort_values(by='date')
            
            html += """<table><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Balance</th><th>Notes</th></tr></thead><tbody>"""
            
            for _, row in savings_df.iterrows():
                notes = self.clean_notes(row['notes'])
                amount = row['amount']
                color = "black"
                
                if row['transaction_type'] == "Withdrawal":
                    amount = -amount
                    color = "#dc3545"
                
                html += f"<tr><td>{row['date']}</td><td>{row['transaction_type']}</td><td style='color:{color}'>{amount:,.0f}</td><td>{row['balance']:,.0f}</td><td>{notes}</td></tr>"
            
            html += "</tbody></table>"
        else:
            html += "<p style='padding:10px;color:#666;'>No savings transactions in this period</p>"
        
        html += f"""</div>
</div>
<div class="summary-row">
    <div class="summary-item summary-loans">
        <div>Total Net Outstanding: {total_balance:,.0f}</div>
        <div style="font-size:10px;color:#666;">(Principal + Accrued Interest)</div>
        <div style="margin-top:5px;">Total Gross Outstanding: {total_gross_balance:,.0f}</div>
        <div style="font-size:10px;color:#666;">(Principal + Total Expected Interest)</div>
    </div>
    <div class="summary-item summary-savings">Savings Balance: {savings_balance:,.0f}</div>
</div>
<div class="footer">Statement generated on {datetime.now().strftime('%Y-%m-%d at %H:%M')} | Period: {from_date} to {to_date}</div>
</body></html>"""
        
        return html, total_balance, total_gross_balance, savings_balance
    
    def generate_pdf_statement(self, ind_id, name, folder, from_date=None, to_date=None):
        """Generate and save a statement as PDF file with landscape layout.
        
        Args:
            ind_id: Individual ID.
            name: Individual name for filename.
            folder: Output folder path.
            from_date: Start date (YYYY-MM-DD). Defaults to "2000-01-01".
            to_date: End date (YYYY-MM-DD). Defaults to today.
            
        Returns:
            True if successful, False otherwise.
        """
        if not from_date:
            from_date = "2000-01-01"
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
        
        _, phone, _ = self._get_individual_info(ind_id)
        phone = phone or ""
        
        _, status_str, status_color = self._get_account_status(ind_id)
        
        from_display = datetime.strptime(from_date, "%Y-%m-%d").strftime("%B %d, %Y")
        to_display = datetime.strptime(to_date, "%Y-%m-%d").strftime("%B %d, %Y")
        
        html, _, _, _ = self._generate_pdf_html(
            ind_id, name, phone, from_date, to_date,
            status_str, status_color, from_display, to_display
        )
        
        if html is None:
            return False
        
        safe_name = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        filename = f"{safe_name}_statement_{datetime.now().strftime('%Y%m%d')}.pdf"
        filepath = os.path.join(folder, filename)
        
        try:
            from PyQt6.QtCore import QMarginsF, QEventLoop
            from PyQt6.QtGui import QPageLayout, QPageSize
            
            web_view = self._get_printer_view() if self._get_printer_view else None
            if web_view is None:
                raise ImportError("QWebEngineView not available")
            
            web_view.setHtml(html)
            
            loop = QEventLoop()
            try:
                web_view.loadFinished.disconnect()
            except:
                pass
            
            web_view.loadFinished.connect(loop.quit)
            loop.exec()
            
            page_layout = QPageLayout(
                QPageSize(QPageSize.PageSizeId.A4),
                QPageLayout.Orientation.Landscape,
                QMarginsF(10, 10, 10, 10)
            )
            
            pdf_done = [False]
            
            def on_pdf_done(filepath_out, success):
                pdf_done[0] = True
                loop.quit()
            
            try:
                web_view.page().pdfPrintingFinished.disconnect()
            except:
                pass
            
            web_view.page().pdfPrintingFinished.connect(on_pdf_done)
            web_view.page().printToPdf(filepath, page_layout)
            loop.exec()
            
        except Exception as e:
            print(f"PDF generation failed: {e}, falling back to HTML")
            filepath = filepath.replace('.pdf', '.html')
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)
        
        return True
    
    def generate_excel_statement(self, ind_id, name, folder, from_date, to_date):
        """Generate and save a statement as Excel file.
        
        Args:
            ind_id: Individual ID.
            name: Individual name for filename.
            folder: Output folder path.
            from_date: Start date (YYYY-MM-DD).
            to_date: End date (YYYY-MM-DD).
            
        Returns:
            True if successful, False otherwise.
        """
        if not _PANDAS_AVAILABLE:
            print("pandas not available for Excel generation")
            return False
        
        filename = f"Statement_{name}_{from_date}_to_{to_date}.xlsx"
        filename = re.sub(r'[\\/*?:"<>|]', "", filename)
        path = os.path.join(folder, filename)
        
        _, status_str, status_color = self._get_account_status(ind_id)
        
        try:
            df_full = self.db.get_ledger(ind_id)
            savings_df = self.db.get_savings_transactions(ind_id, from_date, to_date)
            
            if df_full.empty and savings_df.empty:
                return False
            
            with pd.ExcelWriter(path, engine='xlsxwriter') as writer:
                workbook = writer.book
                worksheet = workbook.add_worksheet("Statement")
                
                # Formats
                header_fmt = workbook.add_format({
                    'bold': True, 'font_size': 14, 'align': 'center',
                    'bg_color': '#2b5797', 'font_color': 'white'
                })
                status_fmt = workbook.add_format({
                    'bold': True, 'font_size': 12, 'align': 'center',
                    'font_color': status_color
                })
                sub_header_fmt = workbook.add_format({
                    'bold': True, 'font_size': 12, 'bg_color': '#e0e0e0', 'border': 1
                })
                col_header_fmt = workbook.add_format({
                    'bold': True, 'bg_color': '#f0f0f0', 'border': 1
                })
                savings_header_fmt = workbook.add_format({
                    'bold': True, 'font_size': 12, 'bg_color': '#28a745',
                    'font_color': 'white', 'border': 1
                })
                cell_fmt = workbook.add_format({'border': 1})
                currency_fmt = workbook.add_format({'border': 1, 'num_format': '#,##0'})
                period_fmt = workbook.add_format({'italic': True, 'font_size': 10, 'align': 'center'})
                
                # Document header
                worksheet.merge_range('A1:N1', f"ACCOUNT STATEMENT - {name}", header_fmt)
                worksheet.merge_range('A2:H2', f"For the period: {from_date} to {to_date}", period_fmt)
                worksheet.merge_range('I2:K2', status_str, status_fmt)
                
                # Column widths
                worksheet.set_column('A:A', 12)
                worksheet.set_column('B:B', 18)
                worksheet.set_column('C:E', 12)
                worksheet.set_column('F:G', 12)
                worksheet.set_column('H:H', 25)
                worksheet.set_column('I:I', 2)
                worksheet.set_column('J:J', 12)
                worksheet.set_column('K:K', 15)
                worksheet.set_column('L:L', 12)
                worksheet.set_column('M:M', 15)
                worksheet.set_column('N:N', 25)
                
                row_idx = 3
                
                if not df_full.empty:
                    df_full['loan_id'] = df_full['loan_id'].fillna('-')
                    loan_groups = df_full.groupby('loan_id')
                    
                    for loan_ref in sorted(loan_groups.groups.keys()):
                        group = loan_groups.get_group(loan_ref).sort_values(by=['date', 'id'])
                        group_visible = group[(group['date'] >= from_date) & (group['date'] <= to_date)]
                        
                        if group_visible.empty:
                            continue
                        
                        worksheet.merge_range(row_idx, 0, row_idx, 7, f"Loan Reference: {loan_ref}", sub_header_fmt)
                        row_idx += 1
                        
                        headers = ['Date', 'Type', 'Debit', 'Interest', 'Credit', 'Balance', 'Gross', 'Notes']
                        for col, header in enumerate(headers):
                            worksheet.write(row_idx, col, header, col_header_fmt)
                        row_idx += 1
                        
                        current_gross = 0.0
                        for _, row in group.iterrows():
                            event = row['event_type']
                            added = float(row['added'])
                            deducted = float(row['deducted'])
                            
                            if event in ["Loan Issued", "Loan Top-Up"]:
                                current_gross += added * (1 + DEFAULT_INTEREST_RATE)
                            if event in ["Repayment", "Loan Buyoff"]:
                                current_gross -= deducted
                            
                            if from_date <= row['date'] <= to_date:
                                worksheet.write(row_idx, 0, str(row['date']), cell_fmt)
                                worksheet.write(row_idx, 1, event, cell_fmt)
                                worksheet.write(row_idx, 2, math.ceil(added), currency_fmt)
                                worksheet.write(row_idx, 3, math.ceil(float(row.get('interest_amount', 0))), currency_fmt)
                                worksheet.write(row_idx, 4, math.ceil(deducted), currency_fmt)
                                worksheet.write(row_idx, 5, math.ceil(float(row['balance'])), currency_fmt)
                                worksheet.write(row_idx, 6, math.ceil(current_gross), currency_fmt)
                                worksheet.write(row_idx, 7, self.clean_notes(row['notes']), cell_fmt)
                                row_idx += 1
                        
                        row_idx += 1
                
                # Savings section
                if not savings_df.empty:
                    worksheet.merge_range(row_idx, 9, row_idx, 13, "SAVINGS / SHARES", savings_header_fmt)
                    row_idx += 1
                    
                    s_headers = ['Date', 'Type', 'Amount', 'Balance', 'Notes']
                    for col, header in enumerate(s_headers, start=9):
                        worksheet.write(row_idx, col, header, col_header_fmt)
                    row_idx += 1
                    
                    for _, row in savings_df.iterrows():
                        worksheet.write(row_idx, 9, str(row['date']), cell_fmt)
                        worksheet.write(row_idx, 10, row['transaction_type'], cell_fmt)
                        worksheet.write(row_idx, 11, float(row['amount']), currency_fmt)
                        worksheet.write(row_idx, 12, float(row['balance']), currency_fmt)
                        worksheet.write(row_idx, 13, self.clean_notes(row['notes']), cell_fmt)
                        row_idx += 1
            
            return True
            
        except Exception as e:
            print(f"Excel generation failed: {e}")
            return False
