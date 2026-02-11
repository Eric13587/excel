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
from src.data_structures import (
    StatementData, StatementPresentation, StatementLoanSection, 
    StatementRow, StatementSavingsRow, StatementConfig
)


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

    def _validate_inputs(self, ind_id, from_date, to_date):
        """Validate input parameters.
        
        Args:
            ind_id: Individual ID.
            from_date: Start date string (YYYY-MM-DD).
            to_date: End date string (YYYY-MM-DD).
            
        Raises:
            ValueError: If inputs are invalid.
        """
        # Validate ID existence early
        if not self.db.get_individual(ind_id):
            raise ValueError(f"Individual with ID {ind_id} not found.")

        # Validate date format and logic
        try:
            start = datetime.strptime(from_date, "%Y-%m-%d")
            end = datetime.strptime(to_date, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Invalid date format. Expected YYYY-MM-DD.")

        if start > end:
            raise ValueError("Start date cannot be after end date.")

    @staticmethod
    def _sanitize_filename(name):
        """Sanitize filename to prevent OS issues.
        
        Args:
            name: Input name string.
            
        Returns:
            Sanitized safe filename string.
        """
        if not name:
            name = "Unknown"
            
        # Remove unsafe chars
        safe = "".join(c for c in name if c.isalnum() or c in (' ', '-', '_'))
        safe = safe.strip()
        
        if not safe:
            safe = "Statement"
            
        # Limit length (255 is mostly max, but play safe with 100)
        return safe[:100]
    
    def _get_account_status(self, active_loans, savings_balance):
        """Determine account active status.
        
        Returns:
            Tuple of (is_active, status_str, status_color).
        """
        is_active = (len(active_loans) > 0) or (savings_balance != 0)
        
        status_str = "Active" if is_active else "Inactive"
        status_color = "#28a745" if is_active else "#dc3545"
        
        return is_active, status_str, status_color

    def _prepare_presentation(self, data: StatementData, from_date, to_date, config: StatementConfig = None) -> StatementPresentation:
        """Prepare presentation data model from raw data."""
        if config is None:
            config = StatementConfig()
            
        df = data.ledger_df
        savings_df = data.savings_df
        
        name = data.individual.get('name', 'Unknown')
        phone = data.individual.get('phone', '') or ""
        
        _, status_str, status_color = self._get_account_status(data.active_loans, data.savings_balance)
        from_display = datetime.strptime(from_date, "%Y-%m-%d").strftime(config.date_format)
        to_display = datetime.strptime(to_date, "%Y-%m-%d").strftime(config.date_format)
        period_display = f"{from_display} to {to_display}"
        
        loan_sections = []
        total_balance = 0.0
        total_gross_balance = 0.0
        
        if config.show_loans and not df.empty:
            df['loan_id'] = df['loan_id'].fillna('-')
            loan_groups = df.groupby('loan_id')
            
            for loan_ref in sorted(loan_groups.groups.keys()):
                group = loan_groups.get_group(loan_ref).sort_values(by=['date', 'id']) # Sort globally first
                
                rows = []
                current_gross = 0.0
                
                # Full Replay Simulation
                for _, row in group.iterrows():
                    event = row['event_type']
                    added = float(row['added'])
                    deducted = float(row['deducted'])
                    date_str = str(row['date'])
                    
                    # Simulation Logic (Matches LedgerView)
                    if event == "Loan Issued" or event == "Loan Top-Up":
                        # Gross increases by Principal + 15% Interest (Standard Rule)
                        # TODO: Should we read rate from loan config? defaulting to 0.15 matches UI.
                        current_gross += added * 1.15
                    elif event == "Repayment" or event == "Loan Buyoff":
                        current_gross -= deducted
                    
                    # Date Filtering for Report Display
                    # If date is before range, we just simulate and continue.
                    if date_str < from_date:
                        continue
                    # If date is AFTER range, we stop. The current_gross is now the closing balance for this period.
                    if date_str > to_date:
                        break
                        
                    # Prepare Row Data
                    interest = math.ceil(float(row.get('interest_amount', 0)))
                    balance = math.ceil(float(row['balance']))
                    notes = self.clean_notes(row['notes'])
                    
                    show_gross = False
                    if event in ["Loan Issued", "Loan Top-Up"]:
                        show_gross = True
                    if event in ["Repayment", "Loan Buyoff"]:
                        show_gross = True
                        
                    rows.append(StatementRow(
                        date=row['date'],
                        event_type=event,
                        debit=math.ceil(added),
                        interest=interest,
                        credit=math.ceil(deducted),
                        balance=balance,
                        gross_balance=math.ceil(current_gross),
                        show_gross=show_gross,
                        notes=notes
                    ))
                
                if rows:
                    loan_sections.append(StatementLoanSection(
                        loan_ref=loan_ref,
                        rows=rows
                    ))
                    
                    # Use the last visible row for balance snapshot, or the simulation state
                    # Ideally last row of the period (which matches `rows[-1]` or `balance` loop var if we broke)
                    # We need the balance at `to_date`. 
                    # If we broke, `balance` variable holds the balance of the last processed row (before break? no, last processed was > to_date?)
                    # Wait, if we 'break' on `date > to_date`, we haven't processed that future row.
                    # So `balance` holds value of the last *valid* row (<= to_date).
                    # `current_gross` is also correct.
                    
                    # EXCEPT: if 'rows' is empty (all dates < from_date), but loop finished (all dates < to_date).
                    # Then loan_sections is NOT appended (if rows check).
                    # But we might still want to count the totals?
                    # The current logic appends to loan_sections ONLY if rows exists.
                    # This means quiet loans don't appear in report sections. 
                    # But should they appear in Totals?
                    # Existing logic: `if rows: ... total_balance += ...`
                    # So existing logic excludes invisible loans from totals. We'll stick to that for consistency.
                    
                    total_balance += float(rows[-1].balance)
                    total_gross_balance += float(rows[-1].gross_balance)

        savings_rows = []
        if config.show_savings and not savings_df.empty:
            # Savings are usually simpler, just run through them
            # We want to display them sorted by date.
            savings_df = savings_df.sort_values(by='date')
            
            for _, row in savings_df.iterrows():
                # Filter
                if row['date'] >= from_date and row['date'] <= to_date:
                    notes = self.clean_notes(row['notes'])
                    amount = float(row['amount'])
                    is_withdrawal = (row['transaction_type'] == "Withdrawal")
                    if is_withdrawal:
                        amount = -amount # Display as negative or just red? PDF used red. Presentation model: raw amount?
                        # Let's verify existing logic: "amount = -amount" then display.
                        # We'll store signed amount in `amount` for simplicity? 
                        # Or store absolute and use flag?
                        # Existing PDF: `amount = -amount` if withdrawal.
                        # Let's store signed amount.
                        amount = -abs(amount)
                    
                    savings_rows.append(StatementSavingsRow(
                        date=str(row['date']),
                        type=row['transaction_type'],
                        amount=amount,
                        balance=float(row['balance']),
                        notes=notes,
                        is_withdrawal=is_withdrawal
                    ))

        return StatementPresentation(
            customer_name=name,
            customer_phone=phone,
            period_display=period_display,
            status_str=status_str,
            status_color=status_color,
            loan_sections=loan_sections,
            savings_rows=savings_rows,
            total_net_outstanding=total_balance,
            total_gross_outstanding=total_gross_balance,
            savings_balance=data.savings_balance
        )
    
    def _generate_pdf_html(self, presentation: StatementPresentation, config: StatementConfig = None):
        """Generate HTML content for PDF statement.
        
        Returns:
            HTML content string.
        """
        if config is None:
            config = StatementConfig()

        savings_only = not config.show_loans and config.show_savings
        loans_only = config.show_loans and not config.show_savings

        # Generate HTML with landscape layout
        html = """<!DOCTYPE html>
<html><head>
<style>
    @page { size: landscape; margin: 15mm; }
    body { font-family: Arial, sans-serif; margin: 0; padding: 10px; font-size: 9px; }
    .header { text-align: center; border-bottom: 3px solid #2b5797; padding-bottom: 10px; margin-bottom: 15px; display: flex; align_items: center; justify-content: center; position: relative;}
    .header h1 { color: #2b5797; margin: 0; font-size: 22px; }
    .header h2 { color: #333; margin: 5px 0 0 0; font-size: 16px; font-weight: normal; }
    .header .period { color: #666; font-size: 11px; margin-top: 5px; }
    .logo { position: absolute; left: 0; top: 0; height: 50px; }
    .client-info { background: #f5f5f5; padding: 8px; margin-bottom: 15px; border-radius: 5px; display: flex; justify-content: space-between; }
    .client-info .left { }
    .client-info .right { text-align: right; }
    .client-info p { margin: 2px 0; }
    .main-container { display: flex; gap: 20px; }
    .main-container.centered { justify-content: center; }
    .loans-column { flex: 1; }
    .savings-column { flex: 0.6; }
    .savings-column.standalone { flex: none; width: 70%; }
    .section-title { background: #2b5797; color: white; padding: 6px 10px; font-size: 11px; font-weight: bold; margin-bottom: 0; }
    .savings-title { background: #28a745; }
    table { width: 100%; border-collapse: collapse; font-size: 8px; margin-bottom: 10px; }
    th { background: #e0e0e0; padding: 4px; text-align: left; border: 1px solid #ccc; font-size: 7px; }
    td { padding: 3px 4px; border: 1px solid #ddd; }
    .loan-box { margin-bottom: 12px; }
    .summary-row { margin-top: 15px; padding: 10px; background: #f9f9f9; border-radius: 5px; }
    .footer { margin-top: 15px; text-align: center; font-size: 8px; color: #999; }
</style>
</head><body>"""
        
        logo_html = f'<img src="{config.company_logo_path}" class="logo">' if config.company_logo_path else ''
        
        html += f"""<div class="header">
    {logo_html}
    <div>
        <h1>{config.custom_title}</h1>
        <h2>{presentation.customer_name}</h2>
        <div class="period">For the period: {presentation.period_display}</div>
    </div>
</div>
<div class="client-info">
    <div class="left">
        <p><strong>Client Name:</strong> {presentation.customer_name}</p>
        <p><strong>Contact:</strong> {presentation.customer_phone}</p>
    </div>
    <div class="right">
        <p><strong>Statement Date:</strong> {datetime.now().strftime(config.date_format)}</p>
        <p><strong>Account Status:</strong> <span style="color:{presentation.status_color};font-weight:bold;">{presentation.status_str}</span></p>
    </div>
</div>"""

        # Main container — centered when savings-only
        container_class = "main-container centered" if savings_only else "main-container"
        html += f'<div class="{container_class}">'

        # === LOANS COLUMN ===
        if config.show_loans:
            html += """<div class="loans-column">
        <div class="section-title">LOANS</div>"""

            def render_header():
                 return "".join([f"<th>{col}</th>" for col in config.columns])

            def render_row(row):
                def get_val(col):
                    if col == "Date": return row.date
                    if col == "Type": return row.event_type
                    if col == "Debit": return f"{row.debit:,.0f}"
                    if col == "Interest": return f"{row.interest:,.0f}"
                    if col == "Credit": return f"{row.credit:,.0f}"
                    if col == "Balance": return f"{row.balance:,.0f}"
                    if col == "Gross": return f"{row.gross_balance:,.0f}" if row.show_gross else ""
                    if col == "Notes": return row.notes
                    return ""
                
                return "<tr>" + "".join([f"<td>{get_val(c)}</td>" for c in config.columns]) + "</tr>"

            if presentation.loan_sections:
                for section in presentation.loan_sections:
                    html += f"""<div class="loan-box">
                    <table><thead><tr><th colspan="{len(config.columns)}" style="background:#2b5797;color:white;">Loan: {section.loan_ref}</th></tr>
                    <tr>{render_header()}</tr></thead><tbody>"""
                    
                    for row in section.rows:
                        html += render_row(row)
                    
                    html += "</tbody></table></div>"
            else:
                html += "<p style='padding:10px;color:#666;'>No loan transactions in this period</p>"
            
            html += """</div>"""
        
        # === SAVINGS COLUMN ===
        if config.show_savings:
            col_class = "savings-column standalone" if savings_only else "savings-column"
            html += f"""<div class="{col_class}">
        <div class="section-title savings-title">SAVINGS / SHARES</div>"""
            if presentation.savings_rows:
                html += """<table><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Balance</th><th>Notes</th></tr></thead><tbody>"""
                
                for row in presentation.savings_rows:
                    color = "#dc3545" if row.is_withdrawal else "black"
                    html += f"<tr><td>{row.date}</td><td>{row.type}</td><td style='color:{color}'>{row.amount:,.0f}</td><td>{row.balance:,.0f}</td><td>{row.notes}</td></tr>"
                
                html += "</tbody></table>"
            else:
                html += "<p style='padding:10px;color:#666;'>No savings transactions in this period</p>"
            html += "</div>"
        
        html += "</div>"  # close main-container

        # === SUMMARY ROW — only show relevant sections ===
        html += '<div class="summary-row">'
        
        if config.show_loans:
            html += f"""<div class="summary-item summary-loans">
        <div>Total Net Outstanding: {presentation.total_net_outstanding:,.0f}</div>
        <div style="font-size:10px;color:#666;">(Principal + Accrued Interest)</div>
        <div style="margin-top:5px;">Total Gross Outstanding: {presentation.total_gross_outstanding:,.0f}</div>
        <div style="font-size:10px;color:#666;">(Principal + Total Expected Interest)</div>
    </div>"""
        
        if config.show_savings:
            html += f'<div class="summary-item summary-savings">Savings Balance: {presentation.savings_balance:,.0f}</div>'
        
        html += "</div>"  # close summary-row

        html += f"""<div class="footer">{config.custom_footer} | Statement generated on {datetime.now().strftime('%Y-%m-%d at %H:%M')} | Period: {presentation.period_display}</div>
</body></html>"""
        
        return html
    
    def generate_pdf_statement(self, ind_id, name, folder, from_date=None, to_date=None, config: StatementConfig = None):
        """Generate and save a statement as PDF file with landscape layout.
        
        Args:
            ind_id: Individual ID.
            name: Individual name for filename.
            folder: Output folder path.
            from_date: Start date (YYYY-MM-DD). Defaults to "2000-01-01".
            to_date: End date (YYYY-MM-DD). Defaults to today.
            config: Optional StatementConfig.
            
        Returns:
            True if successful, False otherwise.
        """
        if config is None:
            config = StatementConfig()

        if not from_date:
            from_date = "2000-01-01"
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")
            
        try:
            self._validate_inputs(ind_id, from_date, to_date)
        except ValueError as e:
            print(f"Validation Error: {e}")
            return False, None, "error"
        
        # Consolidate DB calls
        data = self.db.get_statement_data(ind_id, from_date, to_date)
        if not data.individual:
             return False, None, "error" # Individual not found
             
        # Prepare presentation
        try:
             presentation = self._prepare_presentation(data, from_date, to_date, config)
        except Exception as e:
            print(f"Presentation preparation failed: {e}")
            return False, None, "error"
        
        # Guard: if completely empty?
        if not presentation.loan_sections and not presentation.savings_rows:
            return False, None, "empty"
            
        html = self._generate_pdf_html(presentation, config)
        
        # Name from presentation (which got it from data)
        # Use sanitized filename
        safe_name = self._sanitize_filename(presentation.customer_name)
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
            if config and not config.allow_html_fallback:
                 print(f"PDF generation failed: {e}. Fallback disabled.")
                 return False, None, "failed"
                 
            print(f"PDF generation failed: {e}, falling back to HTML")
            filepath = filepath.replace('.pdf', '.html')
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(html)
            return True, filepath, "html"
        
        return True, filepath, "pdf"
    
    def generate_excel_statement(self, ind_id, name, folder, from_date, to_date, config: StatementConfig = None):
        """Generate and save a statement as Excel file.
        
        Args:
            ind_id: Individual ID.
            name: Individual name.
            folder: Output folder path.
            from_date: Start date (YYYY-MM-DD).
            to_date: End date (YYYY-MM-DD).
            config: Optional StatementConfig.
            
        Returns:
            True if successful, False otherwise.
        """
        if config is None:
            config = StatementConfig()

        if not _PANDAS_AVAILABLE:
            print("pandas not available for Excel generation")
            return False
            
        try:
            self._validate_inputs(ind_id, from_date, to_date)
        except ValueError as e:
            print(f"Validation Error: {e}")
            return False
            
        # Use sanitized name for filename
        safe_name = self._sanitize_filename(name)
        filename = f"Statement_{safe_name}_{from_date}_to_{to_date}.xlsx"
        filename = re.sub(r'[\\/*?:"<>|]', "", filename)
        path = os.path.join(folder, filename)
        
        # Consolidate DB calls
        data = self.db.get_statement_data(ind_id, from_date, to_date)
        if not data.individual:
             return False
        
        # Prepare presentation
        try:
             presentation = self._prepare_presentation(data, from_date, to_date, config)
        except Exception as e:
            print(f"Presentation preparation failed: {e}")
            return False

        if not presentation.loan_sections and not presentation.savings_rows:
             return False

        try:
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
                    'font_color': presentation.status_color
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
                worksheet.merge_range('A1:H1', f"{config.custom_title} - {presentation.customer_name}", header_fmt)
                worksheet.merge_range('A2:F2', f"For the period: {presentation.period_display}", period_fmt)
                worksheet.merge_range('G2:H2', presentation.status_str, status_fmt)
                
                # Column widths (approximate defaults)
                worksheet.set_column(0, len(config.columns)-1, 15)
                
                # Helper to map column name to presentation attribute
                def get_row_val(r, col_name):
                    if col_name == "Date": return r.date
                    if col_name == "Type": return r.event_type
                    if col_name == "Debit": return r.debit
                    if col_name == "Interest": return r.interest
                    if col_name == "Credit": return r.credit
                    if col_name == "Balance": return r.balance
                    if col_name == "Gross": return r.gross_balance if r.show_gross else ""
                    if col_name == "Notes": return r.notes
                    return ""

                row_idx = 3
                
                if config.show_loans and presentation.loan_sections:
                    for section in presentation.loan_sections:
                        # Merge title across dynamic columns
                        worksheet.merge_range(row_idx, 0, row_idx, len(config.columns)-1, f"Loan Reference: {section.loan_ref}", sub_header_fmt)
                        row_idx += 1
                        
                        # Dynamic Headers
                        for col, header in enumerate(config.columns):
                            worksheet.write(row_idx, col, header, col_header_fmt)
                        row_idx += 1
                        
                        for row in section.rows:
                            for col, header in enumerate(config.columns):
                                val = get_row_val(row, header)
                                if isinstance(val, (int, float)) and val != "":
                                    worksheet.write(row_idx, col, val, currency_fmt)
                                else:
                                    worksheet.write(row_idx, col, val, cell_fmt)
                            row_idx += 1
                        
                        row_idx += 1
                
                # Savings section
                if config.show_savings and presentation.savings_rows:
                    worksheet.merge_range(row_idx, 0, row_idx, 4, "SAVINGS / SHARES", savings_header_fmt)
                    row_idx += 1
                    
                    s_headers = ['Date', 'Type', 'Amount', 'Balance', 'Notes']
                    for col, header in enumerate(s_headers):
                        worksheet.write(row_idx, col, header, col_header_fmt)
                    row_idx += 1
                    
                    for row in presentation.savings_rows:
                        worksheet.write(row_idx, 0, row.date, cell_fmt)
                        worksheet.write(row_idx, 1, row.type, cell_fmt)
                        worksheet.write(row_idx, 2, row.amount, currency_fmt)
                        worksheet.write(row_idx, 3, row.balance, currency_fmt)
                        worksheet.write(row_idx, 4, row.notes, cell_fmt)
                        row_idx += 1
            
            return True
            
        except Exception as e:
            print(f"Excel generation failed: {e}")
            return False
