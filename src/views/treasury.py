from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
                             QTableWidget, QTableWidgetItem, QHeaderView,
                             QMessageBox, QLabel, QGroupBox, QFormLayout,
                             QComboBox, QLineEdit, QDateEdit, QTabWidget, QWidget)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor

from src.services.gl_service import GLService
from src.services.provisioning import ProvisioningService
from src.exceptions import UnbalancedJournalError, UnknownAccountError


class TreasuryDialog(QDialog):
    """Double-entry Treasury & General Ledger workspace.

    Replaces the previous single-entry "tag an amount with a type" form with a
    proper general ledger: balanced journal entries, a trial balance that must
    tie, per-account drill-down, and a journal register with reversing-entry
    corrections. All figures come from :class:`GLService` so the screen, the
    statements and the balance sheet agree.
    """

    def __init__(self, db_manager, theme_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self.theme_manager = theme_manager
        self.gl = GLService(db_manager)
        self.prov = ProvisioningService(db_manager, self.gl)
        # Project member activity + migrate legacy treasury rows so everything
        # shown is current.
        self.gl.sync()
        self.accounts = self.gl.get_accounts()

        self.setWindowTitle("Treasury & General Ledger")
        self.resize(1050, 680)
        self.init_ui()
        self.apply_theme()
        self.refresh_all()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def init_ui(self):
        self.layout = QVBoxLayout(self)

        self.summary_group = QGroupBox("Cash & Bank Summary Snapshot")
        summary_layout = QHBoxLayout()
        summary_layout.setContentsMargins(15, 12, 15, 12)
        self.cash_balance_label = QLabel("Calculated Bank Cash: …")
        self.cash_balance_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        summary_layout.addWidget(self.cash_balance_label)
        summary_layout.addStretch()
        self.balanced_label = QLabel("")
        self.balanced_label.setStyleSheet("font-size: 13px; font-weight: bold;")
        summary_layout.addWidget(self.balanced_label)
        self.summary_group.setLayout(summary_layout)
        self.layout.addWidget(self.summary_group)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_journal_entry_tab(), "New Journal Entry")
        self.tabs.addTab(self._build_trial_balance_tab(), "Trial Balance")
        self.tabs.addTab(self._build_account_ledger_tab(), "Account Ledger")
        self.tabs.addTab(self._build_journal_list_tab(), "Journal Register")
        self.tabs.addTab(self._build_provisioning_tab(), "Loan Provisioning")
        self.layout.addWidget(self.tabs)

        btn_layout = QHBoxLayout()
        self.btn_report = QPushButton("Generate Balance Sheet & P&L")
        self.btn_report.setStyleSheet("background-color: #10b981; color: white;")
        self.btn_report.clicked.connect(self.generate_reports)
        btn_layout.addWidget(self.btn_report)
        btn_layout.addStretch()
        self.btn_close = QPushButton("Close")
        self.btn_close.clicked.connect(self.accept)
        btn_layout.addWidget(self.btn_close)
        self.layout.addLayout(btn_layout)

    def _account_combo(self):
        combo = QComboBox()
        for a in self.accounts:
            combo.addItem(f"{a['code']} — {a['name']}", a['code'])
        return combo

    # ----- Tab 1: New Journal Entry ----- #
    def _build_journal_entry_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        form = QFormLayout()
        self.je_date = QDateEdit()
        self.je_date.setCalendarPopup(True)
        self.je_date.setDate(QDate.currentDate())
        self.je_memo = QLineEdit()
        self.je_memo.setPlaceholderText("e.g. January office rent")
        form.addRow("Date:", self.je_date)
        form.addRow("Memo:", self.je_memo)
        v.addLayout(form)

        self.je_table = QTableWidget(0, 3)
        self.je_table.setHorizontalHeaderLabels(["Account", "Debit", "Credit"])
        self.je_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.je_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.je_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.je_table.itemChanged.connect(self._recompute_totals)
        v.addWidget(self.je_table)

        line_btns = QHBoxLayout()
        add_btn = QPushButton("+ Add Line")
        add_btn.clicked.connect(lambda: self._add_je_line())
        rem_btn = QPushButton("− Remove Selected Line")
        rem_btn.clicked.connect(self._remove_je_line)
        line_btns.addWidget(add_btn)
        line_btns.addWidget(rem_btn)
        line_btns.addStretch()
        self.je_totals = QLabel("Debits 0.00   Credits 0.00   Difference 0.00")
        self.je_totals.setStyleSheet("font-weight: bold;")
        line_btns.addWidget(self.je_totals)
        v.addLayout(line_btns)

        post_row = QHBoxLayout()
        post_row.addStretch()
        self.btn_post = QPushButton("Post Entry")
        self.btn_post.setStyleSheet("background-color: #2563EB; color: white; font-weight: bold;")
        self.btn_post.clicked.connect(self.post_journal_entry)
        self.btn_post.setEnabled(False)
        post_row.addWidget(self.btn_post)
        v.addLayout(post_row)

        # Start with two blank lines (the minimum for a balanced entry).
        self._add_je_line()
        self._add_je_line()
        return w

    def _add_je_line(self):
        row = self.je_table.rowCount()
        self.je_table.insertRow(row)
        self.je_table.setCellWidget(row, 0, self._account_combo())
        for col in (1, 2):
            item = QTableWidgetItem("")
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.je_table.setItem(row, col, item)

    def _remove_je_line(self):
        row = self.je_table.currentRow()
        if row >= 0 and self.je_table.rowCount() > 1:
            self.je_table.removeRow(row)
            self._recompute_totals()

    @staticmethod
    def _cell_amount(item):
        if item is None or not item.text().strip():
            return 0.0
        try:
            return round(float(item.text().replace(",", "")), 2)
        except ValueError:
            return 0.0

    def _recompute_totals(self):
        total_d = total_c = 0.0
        for r in range(self.je_table.rowCount()):
            total_d += self._cell_amount(self.je_table.item(r, 1))
            total_c += self._cell_amount(self.je_table.item(r, 2))
        diff = round(total_d - total_c, 2)
        self.je_totals.setText(
            f"Debits {total_d:,.2f}   Credits {total_c:,.2f}   Difference {diff:,.2f}")
        color = "#166534" if abs(diff) < 0.005 and total_d > 0 else "#991b1b"
        self.je_totals.setStyleSheet(f"font-weight: bold; color: {color};")
        # Only a balanced, non-empty entry can be posted.
        self.btn_post.setEnabled(abs(diff) < 0.005 and total_d > 0)

    def post_journal_entry(self):
        lines = []
        for r in range(self.je_table.rowCount()):
            combo = self.je_table.cellWidget(r, 0)
            code = combo.currentData() if combo else None
            debit = self._cell_amount(self.je_table.item(r, 1))
            credit = self._cell_amount(self.je_table.item(r, 2))
            if debit == 0 and credit == 0:
                continue  # skip blank lines
            if debit and credit:
                QMessageBox.warning(self, "Invalid Line",
                                    "A line cannot have both a debit and a credit.")
                return
            lines.append({'account': code, 'debit': debit, 'credit': credit})

        if len(lines) < 2:
            QMessageBox.warning(self, "Invalid", "A journal entry needs at least two lines.")
            return

        try:
            self.gl.post_journal(
                self.je_date.date().toString("yyyy-MM-dd"),
                lines,
                memo=self.je_memo.text().strip(),
                source="manual",
            )
        except (UnbalancedJournalError, UnknownAccountError, ValueError) as e:
            QMessageBox.critical(self, "Posting Failed", str(e))
            return

        QMessageBox.information(self, "Posted", "Journal entry posted.")
        # Reset the form to two blank lines.
        self.je_table.setRowCount(0)
        self._add_je_line()
        self._add_je_line()
        self.je_memo.clear()
        self.refresh_all()

    # ----- Tab 2: Trial Balance ----- #
    def _build_trial_balance_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("As of:"))
        self.tb_date = QDateEdit()
        self.tb_date.setCalendarPopup(True)
        self.tb_date.setDate(QDate.currentDate())
        self.tb_date.dateChanged.connect(self.refresh_trial_balance)
        ctrl.addWidget(self.tb_date)
        ctrl.addStretch()
        v.addLayout(ctrl)

        self.tb_table = QTableWidget()
        self.tb_table.setColumnCount(5)
        self.tb_table.setHorizontalHeaderLabels(["Code", "Account", "Type", "Debit", "Credit"])
        self.tb_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tb_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self.tb_table)
        return w

    def refresh_trial_balance(self):
        as_of = self.tb_date.date().toString("yyyy-MM-dd")
        rows, balanced = self.gl.get_trial_balance(as_of)
        self.tb_table.setRowCount(0)
        total_d = total_c = 0.0
        for rec in rows:
            r = self.tb_table.rowCount()
            self.tb_table.insertRow(r)
            self.tb_table.setItem(r, 0, QTableWidgetItem(rec['code']))
            self.tb_table.setItem(r, 1, QTableWidgetItem(rec['name']))
            self.tb_table.setItem(r, 2, QTableWidgetItem(rec['type']))
            self._money_cell(r, 3, rec['debit'])
            self._money_cell(r, 4, rec['credit'])
            total_d += rec['debit']
            total_c += rec['credit']
        # Totals row
        r = self.tb_table.rowCount()
        self.tb_table.insertRow(r)
        tot = QTableWidgetItem("TOTAL")
        tot.setForeground(QColor("#1e3a8a"))
        self.tb_table.setItem(r, 2, tot)
        self._money_cell(r, 3, total_d, bold=True)
        self._money_cell(r, 4, total_c, bold=True)

        if balanced:
            self.balanced_label.setText("Books balanced ✓")
            self.balanced_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #166534;")
        else:
            self.balanced_label.setText("OUT OF BALANCE ✗")
            self.balanced_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #991b1b;")

    def _money_cell(self, row, col, value, bold=False):
        item = QTableWidgetItem(f"{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if bold:
            f = item.font(); f.setBold(True); item.setFont(f)
        self.tb_table.setItem(row, col, item)

    # ----- Tab 3: Account Ledger ----- #
    def _build_account_ledger_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Account:"))
        self.al_account = self._account_combo()
        self.al_account.currentIndexChanged.connect(self.refresh_account_ledger)
        ctrl.addWidget(self.al_account, 2)
        ctrl.addWidget(QLabel("As of:"))
        self.al_date = QDateEdit()
        self.al_date.setCalendarPopup(True)
        self.al_date.setDate(QDate.currentDate())
        self.al_date.dateChanged.connect(self.refresh_account_ledger)
        ctrl.addWidget(self.al_date)
        ctrl.addStretch()
        v.addLayout(ctrl)

        self.al_table = QTableWidget()
        self.al_table.setColumnCount(6)
        self.al_table.setHorizontalHeaderLabels(["Date", "Memo", "Source", "Debit", "Credit", "Balance"])
        self.al_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.al_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self.al_table)
        return w

    def refresh_account_ledger(self):
        code = self.al_account.currentData()
        if not code:
            return
        as_of = self.al_date.date().toString("yyyy-MM-dd")
        rows = self.gl.get_account_ledger(code, as_of)
        self.al_table.setRowCount(0)
        for rec in rows:
            r = self.al_table.rowCount()
            self.al_table.insertRow(r)
            self.al_table.setItem(r, 0, QTableWidgetItem(rec['date']))
            self.al_table.setItem(r, 1, QTableWidgetItem(rec['memo'] or ""))
            self.al_table.setItem(r, 2, QTableWidgetItem(rec['source'] or ""))
            for col, key in ((3, 'debit'), (4, 'credit'), (5, 'balance')):
                item = QTableWidgetItem(f"{rec[key]:,.2f}" if rec[key] else ("" if key != 'balance' else f"{rec[key]:,.2f}"))
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.al_table.setItem(r, col, item)

    # ----- Tab 4: Journal Register ----- #
    def _build_journal_list_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        self.jr_table = QTableWidget()
        self.jr_table.setColumnCount(6)
        self.jr_table.setHorizontalHeaderLabels(["ID", "Date", "Memo", "Source", "Amount", "Status"])
        self.jr_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.jr_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.jr_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self.jr_table)

        actions = QHBoxLayout()
        actions.addStretch()
        self.btn_reverse = QPushButton("Reverse Selected Entry")
        self.btn_reverse.setStyleSheet("background-color: #F59E0B; color: black;")
        self.btn_reverse.clicked.connect(self.reverse_selected)
        actions.addWidget(self.btn_reverse)
        v.addLayout(actions)
        return w

    def refresh_journal_list(self):
        entries = self.gl.get_journal_entries()
        self.jr_table.setRowCount(0)
        for e in entries:
            r = self.jr_table.rowCount()
            self.jr_table.insertRow(r)
            self.jr_table.setItem(r, 0, QTableWidgetItem(str(e['id'])))
            self.jr_table.setItem(r, 1, QTableWidgetItem(e['entry_date']))
            self.jr_table.setItem(r, 2, QTableWidgetItem(e['memo'] or ""))
            self.jr_table.setItem(r, 3, QTableWidgetItem(e['source'] or ""))
            amt = QTableWidgetItem(f"{e['amount']:,.2f}")
            amt.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.jr_table.setItem(r, 4, amt)
            self.jr_table.setItem(r, 5, QTableWidgetItem(e['status']))

    def reverse_selected(self):
        sel = self.jr_table.selectedItems()
        if not sel:
            return
        row = sel[0].row()
        entry_id = int(self.jr_table.item(row, 0).text())
        source = self.jr_table.item(row, 3).text()
        status = self.jr_table.item(row, 5).text()

        if self.gl.is_auto_source(source):
            QMessageBox.information(
                self, "Cannot Reverse",
                "This entry is auto-posted from member loan/savings activity. "
                "Correct it by editing the underlying loan or savings record, not here.")
            return
        if status != "posted":
            QMessageBox.information(self, "Cannot Reverse",
                                   f"Entry #{entry_id} is already '{status}'.")
            return

        if QMessageBox.question(
                self, "Reverse Entry",
                f"Post a reversing entry for journal #{entry_id}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            self.gl.reverse_entry(entry_id)
            QMessageBox.information(self, "Reversed", f"Entry #{entry_id} reversed.")
            self.refresh_all()

    # ----- Tab 5: Loan Provisioning (SASRA) ----- #
    def _build_provisioning_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("As of:"))
        self.pr_date = QDateEdit()
        self.pr_date.setCalendarPopup(True)
        self.pr_date.setDate(QDate.currentDate())
        self.pr_date.dateChanged.connect(self.refresh_provisioning)
        ctrl.addWidget(self.pr_date)
        ctrl.addStretch()
        self.pr_par = QLabel("")
        self.pr_par.setStyleSheet("font-weight: bold;")
        ctrl.addWidget(self.pr_par)
        v.addLayout(ctrl)

        self.pr_table = QTableWidget()
        self.pr_table.setColumnCount(7)
        self.pr_table.setHorizontalHeaderLabels(
            ["Classification", "Days in Arrears", "Rate", "Loans",
             "Gross Outstanding", "Net of Deposits", "Provision"])
        self.pr_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pr_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v.addWidget(self.pr_table)

        bottom = QHBoxLayout()
        self.pr_allowance = QLabel("")
        bottom.addWidget(self.pr_allowance)
        bottom.addStretch()
        self.btn_book = QPushButton("Book Provision to GL")
        self.btn_book.setStyleSheet("background-color: #2563EB; color: white; font-weight: bold;")
        self.btn_book.clicked.connect(self.book_provision_action)
        bottom.addWidget(self.btn_book)
        v.addLayout(bottom)
        return w

    @staticmethod
    def _band_days_label(bucket):
        from src.config import SASRA_PROVISION_BANDS
        for label, lo, hi, _ in SASRA_PROVISION_BANDS:
            if label == bucket:
                return f"{lo}+" if hi is None else f"{lo}–{hi}"
        return ""

    def refresh_provisioning(self):
        as_of = self.pr_date.date().toString("yyyy-MM-dd")
        summary = self.prov.get_provisioning_summary(as_of)
        self.pr_table.setRowCount(0)
        for b in summary['bands']:
            r = self.pr_table.rowCount()
            self.pr_table.insertRow(r)
            self.pr_table.setItem(r, 0, QTableWidgetItem(b['bucket']))
            self.pr_table.setItem(r, 1, QTableWidgetItem(self._band_days_label(b['bucket'])))
            self.pr_table.setItem(r, 2, QTableWidgetItem(f"{b['rate']*100:.0f}%"))
            cnt = QTableWidgetItem(str(b['count']))
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.pr_table.setItem(r, 3, cnt)
            self._pr_money(r, 4, b['gross'])
            self._pr_money(r, 5, b['net'])
            self._pr_money(r, 6, b['provision'])
        # Totals row
        r = self.pr_table.rowCount()
        self.pr_table.insertRow(r)
        tot = QTableWidgetItem("TOTAL")
        tot.setForeground(QColor("#1e3a8a"))
        self.pr_table.setItem(r, 0, tot)
        self._pr_money(r, 4, summary['total_gross'], bold=True)
        self._pr_money(r, 5, summary['total_net'], bold=True)
        self._pr_money(r, 6, summary['total_provision'], bold=True)

        self.pr_par.setText(f"Portfolio at Risk (>30d): {summary['par_ratio']*100:.1f}%")
        current = self.gl.get_account_balance("1190", as_of)
        required = summary['total_provision']
        self.pr_allowance.setText(
            f"Allowance booked: {current:,.2f}   |   Required: {required:,.2f}   "
            f"|   To book: {required - current:,.2f}")

    def _pr_money(self, row, col, value, bold=False):
        item = QTableWidgetItem(f"{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if bold:
            f = item.font(); f.setBold(True); item.setFont(f)
        self.pr_table.setItem(row, col, item)

    def book_provision_action(self):
        as_of = self.pr_date.date().toString("yyyy-MM-dd")
        summary = self.prov.get_provisioning_summary(as_of)
        current = self.gl.get_account_balance("1190", as_of)
        delta = round(summary['total_provision'] - current, 2)
        if abs(delta) < 0.005:
            QMessageBox.information(self, "Up to Date",
                                   "The allowance already matches the required provision.")
            return
        verb = "increase" if delta > 0 else "release"
        if QMessageBox.question(
                self, "Book Provision",
                f"Post a journal to {verb} the loan-loss allowance by {abs(delta):,.2f} "
                f"(to required level {summary['total_provision']:,.2f}) as of {as_of}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            res = self.prov.book_provision(as_of)
            QMessageBox.information(
                self, "Provision Booked",
                f"Allowance set to {res['required']:,.2f} (change {res['change']:,.2f}).")
            self.refresh_all()

    # ------------------------------------------------------------------ #
    # Shared refresh / theme
    # ------------------------------------------------------------------ #
    def refresh_all(self):
        self.update_bank_cash_visual()
        self.refresh_trial_balance()
        self.refresh_account_ledger()
        self.refresh_journal_list()
        self.refresh_provisioning()

    def update_bank_cash_visual(self):
        # Single source of truth: the Cash at Bank balance from the ledger.
        from src.config import GL_CASH
        cash = self.gl.get_account_balance(GL_CASH)
        self.cash_balance_label.setText(f"Calculated Bank Cash: {cash:,.2f}")

    def apply_theme(self):
        t = self.theme_manager
        self.setStyleSheet(f"""
            QDialog {{ background-color: {t.get_color('bg_primary')}; color: {t.get_color('text_primary')}; }}
            QGroupBox {{ border: 1px solid {t.get_color('border')}; margin-top: 10px; border-radius: 5px; }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {t.get_color('text_secondary')}; }}
            QPushButton {{ background-color: {t.get_color('accent')}; color: white; padding: 8px 15px; border-radius: 5px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {t.get_color('accent_hover')}; }}
            QPushButton:disabled {{ background-color: {t.get_color('border')}; color: {t.get_color('text_secondary')}; }}
            QTabWidget::pane {{ border: 1px solid {t.get_color('border')}; }}
            QTabBar::tab {{ background: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; padding: 8px 14px; }}
            QTabBar::tab:selected {{ background: {t.get_color('accent')}; color: white; }}
            QTableWidget {{ background-color: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; gridline-color: {t.get_color('border')}; }}
            QHeaderView::section {{ background-color: {t.get_color('bg_header')}; color: {t.get_color('text_secondary')}; font-weight: bold; padding: 4px; border: none; border-right: 1px solid {t.get_color('border')}; }}
            QLineEdit, QComboBox, QDateEdit {{ background-color: {t.get_color('input_bg')}; border: 1px solid {t.get_color('border')}; padding: 6px; border-radius: 4px; color: {t.get_color('text_primary')}; }}
        """)

    # ------------------------------------------------------------------ #
    # Statements (GL-derived; balances by construction)
    # ------------------------------------------------------------------ #
    def generate_reports(self):
        from PyQt6.QtWidgets import QFileDialog
        from src.reports import ReportGenerator
        from datetime import datetime
        import platform
        import subprocess
        import os

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Institutional Reports",
            f"SACCO_Financials_{datetime.now().strftime('%Y%m%d')}.pdf", "PDF Files (*.pdf)")
        if not path:
            return

        main_dash = self.parent() if hasattr(self, 'parent') and hasattr(self.parent(), 'get_printer_view') else None
        getter = getattr(main_dash, 'get_printer_view', None) if main_dash else None
        if not getter:
            QMessageBox.critical(self, "Error", "Cannot initialize PDF printer engine.")
            return

        generator = ReportGenerator(self.db, printer_view_getter=getter)
        success, msg = generator.generate_financial_statements(
            path, target_date_str=datetime.now().strftime("%Y-%m-%d"))

        if success:
            QMessageBox.information(self, "Success", msg + f"\nSaved to: {path}")
            if platform.system() == 'Windows':
                os.startfile(path)
            elif platform.system() == 'Linux':
                subprocess.Popen(['xdg-open', path])
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', path])
        else:
            QMessageBox.critical(self, "Error", msg)
