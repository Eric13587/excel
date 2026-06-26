"""LedgerView for managing individual loan ledgers."""
import math
import pandas as pd
from datetime import datetime

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QLabel,
                             QPushButton, QLineEdit, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QScrollArea, QGroupBox,
                             QMessageBox, QFileDialog, QMenu, QDialog,
                             QFormLayout, QDateEdit, QCheckBox, QSpinBox, QDialogButtonBox,
                             QHeaderView, QSplitter, QProgressDialog, QApplication,
                             QTabWidget, QDoubleSpinBox)
from PyQt6.QtGui import QAction, QColor, QShortcut, QKeySequence
from PyQt6.QtCore import Qt, QDate, QTimer


from ..theme import ThemeManager
from ..engine import LoanEngine
from ..exceptions import ChristmasLockedError


class LedgerView(QWidget):
    """View for managing an individual's loan ledger."""
    
    def __init__(self, main_window, db_manager):
        super().__init__()
        self.main_window = main_window
        self.db = db_manager
        self.theme_manager = ThemeManager(db_manager)
        self.engine = LoanEngine(db_manager)
        self.current_individual_id = None
        
        self.init_ui()
        self.apply_theme()
        self.setup_shortcuts()

    def setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        shortcut_f1 = QShortcut(QKeySequence("F1"), self)
        shortcut_f1.activated.connect(lambda: self.amount_input.setFocus())
        
        shortcut_esc = QShortcut(QKeySequence("Escape"), self)
        shortcut_esc.activated.connect(self.main_window.show_dashboard)
        
        # Undo/Redo shortcuts
        shortcut_undo = QShortcut(QKeySequence("Ctrl+Z"), self)
        shortcut_undo.activated.connect(self.global_undo)
        
        shortcut_redo = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        shortcut_redo.activated.connect(self.global_redo)
    
    def global_undo(self):
        """Global undo - restores the last deleted transaction.
        
        The undo stack contains deleted transactions. Pressing Ctrl+Z restores
        the most recently deleted transaction (undoes the deletion).
        """
        if not self.engine.can_undo():
            QMessageBox.information(self, "Undo", 
                "Nothing to undo.\n\nUse the 'Undo' button on a loan to delete a transaction first.")
            return
        
        desc = self.engine.get_undo_description() or "action"
        confirm = QMessageBox.question(self, "Confirm Restore", 
                                       f"Restore: {desc}?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            result = self.engine.undo()
            if result:
                self.refresh_table()
                self.refresh_loans_list()
                QMessageBox.information(self, "Restored", f"Restored: {result.description}")
            else:
                QMessageBox.warning(self, "Error", "Failed to restore.")
    
    def global_redo(self):
        """Global redo - re-deletes a restored transaction.
        
        After using Ctrl+Z to restore a deleted transaction, Ctrl+Shift+Z
        will delete it again (redo the deletion).
        """
        if not self.engine.can_redo():
            QMessageBox.information(self, "Redo", "Nothing to redo.")
            return
        
        desc = self.engine.get_redo_description() or "action"
        confirm = QMessageBox.question(self, "Confirm Delete Again", 
                                       f"Delete again: {desc}?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            result = self.engine.redo()
            if result:
                self.refresh_table()
                self.refresh_loans_list()
                QMessageBox.information(self, "Deleted", f"Deleted: {result.description}")
            else:
                QMessageBox.warning(self, "Error", "Failed to delete.")

    def init_ui(self):
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0) # Maximize space
        
        # Sidebar Scroll Area
        
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        sidebar_widget = QWidget()
        sidebar = QVBoxLayout(sidebar_widget)
        sidebar.setContentsMargins(10, 10, 10, 10) 
        
        back_btn = QPushButton("<- Back to Dashboard")
        back_btn.clicked.connect(self.main_window.show_dashboard)
        sidebar.addWidget(back_btn)
        
        self.title_label = QLabel("<b>Ledger</b>")
        sidebar.addWidget(self.title_label)
        
        sidebar.addWidget(QLabel("<b>Add New Loan</b>"))
        self.amount_input = QLineEdit(placeholderText="Principal Amount")
        self.duration_input = QLineEdit(placeholderText="Duration (Months)")
        
        # Principal input with calculator button
        principal_layout = QHBoxLayout()
        sidebar.addWidget(QLabel("Principal:"))
        principal_layout.addWidget(self.amount_input)
        calc_btn = QPushButton("?")
        calc_btn.setMaximumWidth(30)
        calc_btn.setToolTip("Calculate principal from deduction")
        calc_btn.clicked.connect(self.calculate_principal_dialog)
        principal_layout.addWidget(calc_btn)
        sidebar.addLayout(principal_layout)
        
        sidebar.addWidget(QLabel("Duration (Months):"))
        sidebar.addWidget(self.duration_input)
        sidebar.addWidget(QLabel("Interest Rate (%):"))
        self.interest_input = QLineEdit(placeholderText="Default: 15")
        sidebar.addWidget(self.interest_input)
        sidebar.addWidget(QLabel("Date:"))
        self.date_input = QDateEdit()
        self.date_input.setCalendarPopup(True)
        self.date_input.setDate(self.main_window.last_operation_date)
        sidebar.addWidget(self.date_input)
        
        loan_btn = QPushButton("Issue Loan")
        loan_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white; font-weight: bold;")
        loan_btn.clicked.connect(self.process_loan)
        sidebar.addWidget(loan_btn)

        # Loan controls live in the shared sidebar; greyed out when not on the
        # Loans tab (see _on_main_tab_changed).
        self._loan_controls = [self.amount_input, self.duration_input, self.interest_input,
                               self.date_input, loan_btn, calc_btn]

        
        sidebar.addWidget(QLabel("<b>Active Loans</b>"))
        self.loans_list = QListWidget()
        # Set fixed height for list widget to avoid it taking all space in scroll area
        self.loans_list.setMinimumHeight(100)
        self.loans_list.setMaximumHeight(150)
        sidebar.addWidget(self.loans_list)
        
        sidebar.addSpacing(20)
        sidebar.addWidget(QLabel("<b>Actions</b>"))
        



        
 

        

        

        # Savings/Shares controls now live in the Savings tab header
        # (see _build_savings_tab), keeping the sidebar loan-only.

        sidebar.addStretch()
        
        sidebar_scroll.setWidget(sidebar_widget)
        # self.layout.addWidget(sidebar_scroll, 1) # OLD

        
        # Main Content Area — one tab per fund.
        # Loans tab keeps the existing scroll_area / scroll_layout untouched.
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_content = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_area.setWidget(self.scroll_content)

        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.scroll_area, "Loans")
        self.main_tabs.addTab(self._build_savings_tab(), "Savings / Shares")
        self.main_tabs.addTab(self._build_christmas_tab(), "Christmas")
        self.main_tabs.addTab(self._build_benevolent_tab(), "Benevolent")
        # Grey the sidebar loan controls when not on the Loans tab.
        self.main_tabs.currentChanged.connect(self._on_main_tab_changed)

        # === SPLITTER IMPLEMENTATION ===
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(sidebar_scroll)
        splitter.addWidget(self.main_tabs)
        
        # Set Initial Sizes (approx 1:3 ratio)
        splitter.setSizes([300, 900])
        splitter.setCollapsible(0, False) # Don't collapse sidebar completely
        
        self.layout.addWidget(splitter)
        
        self.tables = {}
        self.savings_table = None

    def get_ledger_df(self, individual_id, start_date=None, end_date=None):
        return self.db.get_ledger(individual_id, start_date, end_date)
    
    def apply_theme(self):
        """Update styles based on current theme."""
        t = self.theme_manager
        
        # General Widget Style
        self.setStyleSheet(f"background-color: {t.get_color('bg_primary')}; color: {t.get_color('text_primary')};")
        
        # Specific Styles for inputs handled globally by parent sheet often, but let's be specific
        input_style = f"""
            QLineEdit, QDateEdit, QSpinBox {{
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                border: 1px solid {t.get_color('border')};
                border-radius: 4px;
                padding: 4px;
            }}
            QListWidget {{
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                border: 1px solid {t.get_color('border')};
            }}
            QScrollArea {{ border: none; }}
        """
        # We can't setStyleSheet recursively easily on self, better to set on specific children if needed, 
        # or use a global sheet on 'self' that targets children by type.
        self.setStyleSheet(f"""
            QWidget {{ background-color: {t.get_color('bg_primary')}; color: {t.get_color('text_primary')}; }}
            QLineEdit, QDateEdit, QSpinBox {{
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                border: 1px solid {t.get_color('border')};
                border-radius: 4px;
                padding: 4px;
            }}
            QListWidget {{
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                border: 1px solid {t.get_color('border')};
            }}
            QLabel {{ color: {t.get_color('text_primary')}; }}
            QTabWidget::pane {{ border: 1px solid {t.get_color('border')}; }}
            QTabBar::tab {{ background: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; padding: 8px 16px; border: 1px solid {t.get_color('border')}; }}
            QTabBar::tab:selected {{ background: {t.get_color('accent')}; color: white; font-weight: bold; }}
            QTableWidget {{ background-color: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; gridline-color: {t.get_color('border')}; }}
            QHeaderView::section {{ background-color: {t.get_color('bg_header')}; color: {t.get_color('text_secondary')}; padding: 4px; border: none; border-right: 1px solid {t.get_color('border')}; }}
            QGroupBox {{
                border: 1px solid {t.get_color('border')};
                border-radius: 6px; 
                margin-top: 10px; 
                font-weight: bold;
            }}
            QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 3px; }}
        """)

    def refresh_ledger(self):
        """Refresh all ledger views (table, active loans, savings)."""
        if self.current_individual_id:
            self.refresh_table()
            self.refresh_loans_list()
            self.refresh_savings_balance()

    def load_individual(self, ind_id, name):
        self.current_individual_id = ind_id
        self.title_label.setText(f"<b>Ledger for {name}</b>")
        
        # Auto-populate monthly savings amount for this individual
        suggested = self.engine.get_suggested_savings_increment(ind_id)
        if suggested and suggested > 0:
            self.savings_increment_input.setText(str(int(suggested)))
        
        self.refresh_ledger()


    def refresh_loans_list(self):
        self.loans_list.clear()
        loans = self.engine.db.get_active_loans(self.current_individual_id)
        today = datetime.now().strftime("%Y-%m-%d")
        for loan in loans:
            item_text = f"{loan['ref']} | Bal: {loan['balance']:.2f} | Due: {loan['next_due_date']}"
            list_item = QListWidgetItem(item_text)
            if loan['next_due_date'] < today:
                list_item.setForeground(QColor(self.theme_manager.get_color('danger')))
            self.loans_list.addItem(list_item)

    def refresh_table(self):
        if self.current_individual_id is None:
            return

        for i in reversed(range(self.scroll_layout.count())):
            w = self.scroll_layout.itemAt(i).widget()
            if w is not None:
                w.setParent(None)
        self.tables = {}

        df = self.engine.get_ledger_df(self.current_individual_id)
        
        if not df.empty:
            df['loan_id'] = df['loan_id'].fillna('-')
            loan_groups = df.groupby('loan_id')
            sorted_loan_ids = sorted(loan_groups.groups.keys())
        else:
            sorted_loan_ids = []
            
        total_balance = 0.0

        for loan_ref in sorted_loan_ids:
            group = loan_groups.get_group(loan_ref).sort_values(by=['date', 'id'])
            
            # Find latest repayment ID for this group to enforce edit constraints
            repayments = group[group['event_type'] == "Repayment"]
            latest_repayment_id = -1
            if not repayments.empty:
                latest_repayment_id = repayments.iloc[-1]['id'] # Since sorted ascending
            
            group_box = QGroupBox()
            group_layout = QVBoxLayout()
            header_layout = QHBoxLayout()
            
            is_overdue = False
            if loan_ref != "-":
                loan_info = self.engine.db.get_loan_by_ref(self.current_individual_id, loan_ref)
                if loan_info and loan_info['next_due_date'] < datetime.now().strftime("%Y-%m-%d"):
                    is_overdue = True
            
            title_text = f"<b>Loan Reference: {loan_ref}</b>"
            if loan_info and loan_info.get('is_suspended', 0):
                suspend_until = loan_info.get('suspend_until', '')
                suffix = f" until {suspend_until}" if suspend_until else ""
                title_text = f"<b style='color: #F59E0B;'>Loan Reference: {loan_ref} (\u23f8 SUSPENDED{suffix})</b>"
            elif is_overdue:
                title_text = f"<b style='color: {self.theme_manager.get_color('danger')};'>Loan Reference: {loan_ref} (OVERDUE)</b>"
            title_label = QLabel(title_text)
            header_layout.addWidget(title_label)
            header_layout.addStretch()
            
            # Initialize table first so it can be passed to lambdas
            table = QTableWidget()
            table.setColumnCount(10)
            table.setHorizontalHeaderLabels(["Date", "Event", "ID", "Prin. Δ", "Int. Δ", "Payment", "Prin. Bal", "Int. Bal", "Total Bal", "Notes"])
            table.setRowCount(len(group))
            table.setColumnHidden(7, True) # Hide Interest Balance column as requested
            
            if loan_ref != "-":
                # Check suspension status
                is_suspended = loan_info.get('is_suspended', 0) if loan_info else 0
                suspend_until = loan_info.get('suspend_until', '') if loan_info else ''
                
                # Header Buttons: Info -> Edit -> Catch Up ...
                info_btn = QPushButton("Info")
                info_btn.setMaximumWidth(40)
                info_btn.clicked.connect(lambda checked, ref=loan_ref: self.show_loan_info(ref))
                header_layout.addWidget(info_btn)
                
                edit_entry_btn = QPushButton("Edit Event")
                edit_entry_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('bg_secondary')}; color: {self.theme_manager.get_color('text_primary')}; border: 1px solid {self.theme_manager.get_color('border')};")
                edit_entry_btn.clicked.connect(lambda checked, t=table: self.edit_loan_entry_btn(t))
                header_layout.addWidget(edit_entry_btn)
                
                # Suspend / Resume button
                if is_suspended:
                    resume_btn = QPushButton("\u25b6 Resume")
                    resume_btn.setStyleSheet("background-color: #10B981; color: white; font-weight: bold;")
                    resume_btn.setToolTip(f"Suspended until {suspend_until}" if suspend_until else "Suspended indefinitely")
                    resume_btn.clicked.connect(lambda checked, lid=loan_info['id']: self.resume_loan_btn(lid))
                    header_layout.addWidget(resume_btn)
                else:
                    suspend_btn = QPushButton("\u23f8 Suspend")
                    suspend_btn.setStyleSheet("background-color: #F59E0B; color: black; font-weight: bold;")
                    suspend_btn.clicked.connect(lambda checked, lid=loan_info['id'], ref=loan_ref: self.suspend_loan_dialog(lid, ref))
                    header_layout.addWidget(suspend_btn)

                # Order: Catch Up (1) -> Top-Up (2) -> Delete (3) -> Deduct (4) -> Auto (5) -> Undo (6)

                catch_up_btn = QPushButton("Catch Up")
                catch_up_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('info')}; color: white;")
                catch_up_btn.clicked.connect(lambda checked, ref=loan_ref: self.loans_catch_up_to_current(ref))
                if is_suspended:
                    catch_up_btn.setEnabled(False)
                    catch_up_btn.setToolTip("Loan is suspended")
                header_layout.addWidget(catch_up_btn)

                top_up_btn = QPushButton("Top-Up Loan")
                top_up_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white;")
                top_up_btn.clicked.connect(lambda checked, ref=loan_ref: self.top_up_loan_dialog(ref))
                header_layout.addWidget(top_up_btn)
                
                buyoff_btn = QPushButton("Buyoff")
                buyoff_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('purple')}; color: white;")
                buyoff_btn.clicked.connect(lambda checked, ref=loan_ref: self.buyoff_loan_btn(ref))
                header_layout.addWidget(buyoff_btn)
                
                delete_loan_btn = QPushButton("Delete Loan")
                delete_loan_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
                delete_loan_btn.clicked.connect(lambda checked, ref=loan_ref: self.delete_loan_btn(ref))
                header_layout.addWidget(delete_loan_btn)

                # Deduction "Adder"
                dedur_label = QLabel("x")
                dedur_label.setStyleSheet("font-weight: bold; margin-left: 10px;")
                header_layout.addWidget(dedur_label)
                
                deduct_multiplier = QSpinBox()
                deduct_multiplier.setRange(1, 12)
                deduct_multiplier.setValue(1)
                deduct_multiplier.setFixedWidth(50)
                header_layout.addWidget(deduct_multiplier)
                
                deduct_btn = QPushButton("Deduct")
                deduct_btn.clicked.connect(lambda checked, ref=loan_ref, spin=deduct_multiplier: self.deduct_multiplier_btn(ref, spin))
                if is_suspended:
                    deduct_btn.setEnabled(False)
                    deduct_btn.setToolTip("Loan is suspended")
                header_layout.addWidget(deduct_btn)

                undo_btn = QPushButton("Delete Last")
                undo_btn.setToolTip("Delete last transaction (Ctrl+Z to restore)")
                undo_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('warning')}; color: black;")
                undo_btn.clicked.connect(lambda checked, ref=loan_ref: self.undo_last_for_loan_btn(ref))
                header_layout.addWidget(undo_btn)
            
            group_layout.addLayout(header_layout)
            
            # table initialization moved up
            
            for i, (index, row) in enumerate(group.iterrows()):
                table.setItem(i, 0, QTableWidgetItem(str(row['date'])))
                table.setItem(i, 1, QTableWidgetItem(str(row['event_type'])))
                
                id_item = QTableWidgetItem(str(row['loan_id']))
                id_item.setData(Qt.ItemDataRole.UserRole, int(row['id']))
                table.setItem(i, 2, id_item)
                
                # Logic for Deltas
                event = row['event_type']
                added = float(row['added'])
                deducted = float(row['deducted'])
                p_portion = float(row.get('principal_portion', 0))
                i_portion = float(row.get('interest_portion', 0))
                
                prin_delta = 0.0
                int_delta = 0.0
                payment = 0.0
                
                # Gross Balance Simulation (Visual Only)
                # Assumes 15% interest total on Issue/Top-Up. 
                # Gross starts at 0 for the group loop and accumulates? 
                # No, we need to accumulate row by row within the sorted group.
                # Since we are inside the 'group' loop (loan_ref), we can track a running gross.
                # However, the table iterates 'group.iterrows()' which is sorted.
                # We need to calculate this BEFORE the row loop or inside it statefully.
                
                if i == 0:
                    self.running_gross_map = {} # Helper if needed, but simple var works
                    self.current_gross = 0.0
                    
                if event == "Loan Issued":
                    prin_delta = added
                    # Gross = Principal + 15% Interest
                    self.current_gross += added * 1.15 
                elif event == "Loan Top-Up":
                    prin_delta = added
                    self.current_gross += added * 1.15
                elif event == "Interest Earned":
                    int_delta = added  # Accrual
                    # Internal accounting, doesn't change Gross Obligation
                elif event == "Repayment" or event == "Loan Buyoff":
                    prin_delta = -p_portion
                    int_delta = -i_portion
                    payment = deducted
                    self.current_gross -= deducted
                else:
                    # Fallback
                    if added > 0: prin_delta = added
                    if deducted > 0: payment = deducted
                    # Unknown events usually don't affect standard Gross logic unless restructuring
                
                # Format to show signs
                p_text = f"{prin_delta:+.0f}" if prin_delta != 0 else "-"
                i_text = f"{int_delta:+.0f}" if int_delta != 0 else "-"
                pay_text = f"{payment:,.0f}" if payment != 0 else "-"
                
                table.setItem(i, 3, QTableWidgetItem(p_text))
                table.setItem(i, 4, QTableWidgetItem(i_text))
                table.setItem(i, 5, QTableWidgetItem(pay_text))
                
                # Balances
                p_bal = float(row.get('principal_balance', 0))
                i_bal = float(row.get('interest_balance', 0))
                
                table.setItem(i, 6, QTableWidgetItem(f"{p_bal:,.0f}"))
                table.setItem(i, 7, QTableWidgetItem(f"{i_bal:,.0f}"))
                
                # Total Balance (Gross Obligation) v2
                # Use the simulated value
                table.setItem(i, 8, QTableWidgetItem(f"{self.current_gross:,.0f}"))
                
                table.setItem(i, 9, QTableWidgetItem(str(row['notes'])))
                
                no_edit = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                edit_flag = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable
                
                table.item(i, 1).setFlags(no_edit)
                table.item(i, 2).setFlags(no_edit)
                
                # Make all columns no_edit by default
                for c in range(3, 9):
                    table.item(i, c).setFlags(no_edit)
                
                # Payment Editable Logic
                if row['event_type'] == "Repayment":
                     # Payment is col 5
                     # Only allow if it is the latest repayment
                     current_id = int(row['id'])
                     if current_id == latest_repayment_id:
                        table.item(i, 5).setFlags(edit_flag)
                     else:
                        table.item(i, 5).setFlags(no_edit)
                        # Optional: Add tooltip or style to indicate locked?
                        table.item(i, 5).setToolTip("Only the latest repayment can be edited.")

                # Color logic
                if row['event_type'] == 'Loan Issued':
                    # Use theme accent instead of hardcoded blue
                    bg_color = QColor(self.theme_manager.get_color("bg_header").split('stop:1 ')[-1].replace(')', '')) # Hacky? No, use accent
                    # Actually theme has 'accent' which is blue.
                    bg_color = QColor(self.theme_manager.get_color("accent"))
                    # But header is dark blue... let's use a specific color or just stick to accent.
                    # Hardcoded was #2b5797 (Dark Blue), Accent is #3B82F6 (Brighter).
                    # Let's keep it distinct or map it.
                    # Ideally we want it readable.
                    white = QColor("white")
                    for c in range(10):
                        table.item(i, c).setBackground(bg_color)
                        table.item(i, c).setForeground(white)
                elif row['event_type'] == 'Interest Earned':
                    # Highlight Accrual
                    bg_color = QColor(self.theme_manager.get_color("warning_bg"))
                    fg_color = QColor(self.theme_manager.get_color("text_primary"))
                    for c in range(10):
                         table.item(i, c).setBackground(bg_color)
                         table.item(i, c).setForeground(fg_color)

            table.itemChanged.connect(self.on_item_changed)
            table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(lambda pos, t=table: self.open_context_menu(pos, t))
            
            table.resizeRowsToContents()
            table.setFixedHeight(table.verticalHeader().length() + table.horizontalHeader().height() + 10)
            
            group_layout.addWidget(table)
            group_box.setLayout(group_layout)
            self.scroll_layout.addWidget(group_box)
            
            self.tables[loan_ref] = table
            
            if not group.empty:
                total_balance += float(group.iloc[-1]['balance'])


        # Refresh the fund tabs.
        self.refresh_savings()
        self.refresh_christmas()
        self.refresh_benevolent()

        name = self.engine.db.get_individual_name(self.current_individual_id)
        self.title_label.setText(f"<b>Ledger for {name} | Total Debt: {total_balance:.2f}</b>")

    # ==================================================================== #
    # Shared helpers for the fund tabs
    # ==================================================================== #
    def _amount_date_dialog(self, title, amount_label="Amount:"):
        """Small modal: amount + date. Returns (amount, 'YYYY-MM-DD') or None."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        form = QFormLayout(dlg)
        amt = QDoubleSpinBox()
        amt.setRange(0.01, 1_000_000_000.0)
        amt.setGroupSeparatorShown(True)
        amt.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        date = QDateEdit()
        date.setCalendarPopup(True)
        date.setDate(self.main_window.last_operation_date)
        form.addRow(amount_label, amt)
        form.addRow("Date:", date)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            return amt.value(), date.date().toString("yyyy-MM-dd")
        return None

    def _populate_fund_table(self, table, df, columns):
        """columns: list of (header, df_key). 'amount'/'balance' keys are money-formatted."""
        table.setRowCount(0)
        if df is None or df.empty:
            return
        for _, row in df.iterrows():
            r = table.rowCount()
            table.insertRow(r)
            for c, (_header, key) in enumerate(columns):
                raw = row.get(key, "")
                if key in ("amount", "balance"):
                    item = QTableWidgetItem(f"{float(raw or 0):,.0f}")
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item = QTableWidgetItem(str(raw) if raw is not None else "")
                if c == 0:  # stash the row id for edit/delete
                    item.setData(Qt.ItemDataRole.UserRole, int(row['id']))
                table.setItem(r, c, item)
        table.resizeRowsToContents()

    def _fund_selected_id(self, table):
        row = table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Selection", "Please select a row first.")
            return None
        item = table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _fund_edit_entry(self, table, fund_table):
        """Edit the selected fund row (date/amount/notes), then recalc + refresh."""
        trans_id = self._fund_selected_id(table)
        if trans_id is None:
            return
        tx = self.db.fund_get_transaction(fund_table, trans_id)
        if not tx:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Entry")
        form = QFormLayout(dlg)
        date = QDateEdit()
        date.setCalendarPopup(True)
        date.setDate(QDate.fromString(str(tx['date'])[:10], "yyyy-MM-dd"))
        amt = QDoubleSpinBox()
        amt.setRange(0.0, 1_000_000_000.0)
        amt.setGroupSeparatorShown(True)
        amt.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        amt.setValue(float(tx['amount'] or 0))
        notes = QLineEdit(tx.get('notes') or "")
        form.addRow("Date:", date)
        form.addRow("Amount:", amt)
        form.addRow("Notes:", notes)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self.db.fund_update_transaction(fund_table, trans_id,
                                        date.date().toString("yyyy-MM-dd"), amt.value(), notes.text())
        self.db.fund_recalculate(fund_table, self.current_individual_id)
        self.refresh_table()

    def _fund_delete_entry(self, table, fund_table):
        """Delete the selected fund row, then recalc + refresh."""
        trans_id = self._fund_selected_id(table)
        if trans_id is None:
            return
        if QMessageBox.question(self, "Confirm", "Delete this entry?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                != QMessageBox.StandardButton.Yes:
            return
        self.db.fund_delete_transaction(fund_table, trans_id)
        self.db.fund_recalculate(fund_table, self.current_individual_id)
        self.refresh_table()

    def _fund_context_menu(self, position, table, fund_table):
        menu = QMenu()
        edit = QAction("Edit Entry", self)
        edit.triggered.connect(lambda: self._fund_edit_entry(table, fund_table))
        delete = QAction("Delete Entry", self)
        delete.triggered.connect(lambda: self._fund_delete_entry(table, fund_table))
        menu.addAction(edit)
        menu.addAction(delete)
        menu.exec(table.viewport().mapToGlobal(position))

    def _fund_delete_all(self, fund_table):
        """Delete every entry for the current member in a fund."""
        if self.current_individual_id is None:
            return
        name = "Christmas" if fund_table == "christmas_savings" else "Benevolent"
        if QMessageBox.question(
                self, "Delete All",
                f"Delete ALL {name} entries for this member? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                != QMessageBox.StandardButton.Yes:
            return
        if fund_table == "christmas_savings":
            self.engine.christmas_service.delete_all(self.current_individual_id)
        else:
            self.engine.benevolent_service.delete_all(self.current_individual_id)
        self.refresh_table()

    def _on_main_tab_changed(self, index):
        """Grey the sidebar loan controls when not on the Loans tab (index 0)."""
        on_loans = (index == 0)
        for ctrl in getattr(self, '_loan_controls', []):
            ctrl.setEnabled(on_loans)

    # ==================================================================== #
    # Savings / Shares tab (controls moved here from the sidebar)
    # ==================================================================== #
    def _build_savings_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        header = QHBoxLayout()
        self.savings_label = QLabel("Savings / Shares — Balance: 0")
        self.savings_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {self.theme_manager.get_color('success')};")
        header.addWidget(self.savings_label)
        header.addStretch()
        header.addWidget(QLabel("Monthly:"))
        self.savings_increment_input = QLineEdit("2500")
        self.savings_increment_input.setMaximumWidth(80)
        header.addWidget(self.savings_increment_input)

        deposit_btn = QPushButton("Deposit")
        deposit_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white;")
        deposit_btn.clicked.connect(self.savings_deposit_dialog)
        withdraw_btn = QPushButton("Withdraw")
        withdraw_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        withdraw_btn.clicked.connect(self.savings_withdraw_dialog)
        catchup_btn = QPushButton("Catch Up")
        catchup_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('info')}; color: white;")
        catchup_btn.clicked.connect(self.savings_catch_up_to_current)
        quick_btn = QPushButton("+ Quick")
        quick_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white;")
        quick_btn.clicked.connect(self.savings_quick_increment)
        auto_btn = QPushButton("Auto...")
        auto_btn.clicked.connect(self.savings_auto_increment_dialog)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(lambda: self.savings_table and self.edit_savings_entry_btn(self.savings_table))
        info_btn = QPushButton("Info")
        info_btn.clicked.connect(lambda: self.savings_table and self.show_savings_info(self.savings_table))
        deletelast_btn = QPushButton("Delete Last")
        deletelast_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('warning')}; color: black;")
        deletelast_btn.clicked.connect(self.undo_last_savings)
        deleteall_btn = QPushButton("Delete All")
        deleteall_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        deleteall_btn.clicked.connect(self.clear_all_savings)
        for b in (deposit_btn, withdraw_btn, catchup_btn, quick_btn, auto_btn,
                  edit_btn, info_btn, deletelast_btn, deleteall_btn):
            header.addWidget(b)
        v.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.savings_scroll_layout = QVBoxLayout(content)
        scroll.setWidget(content)
        v.addWidget(scroll)
        return w

    def refresh_savings(self):
        if self.current_individual_id is None:
            return
        for i in reversed(range(self.savings_scroll_layout.count())):
            wdg = self.savings_scroll_layout.itemAt(i).widget()
            if wdg is not None:
                wdg.setParent(None)

        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        balance = self.db.get_savings_balance(self.current_individual_id)
        self.savings_label.setText(f"Savings / Shares — Balance: {balance:,.0f}")
        self.savings_table = None
        if savings_df.empty:
            return

        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["ID", "Date", "Type", "Amount", "Balance", "Notes"])
        table.setColumnHidden(0, True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setRowCount(len(savings_df))
        no_edit = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        for i, (_, row) in enumerate(savings_df.iterrows()):
            id_item = QTableWidgetItem(str(int(row['id'])))
            id_item.setData(Qt.ItemDataRole.UserRole, int(row['id']))
            id_item.setFlags(no_edit)
            table.setItem(i, 0, id_item)
            table.setItem(i, 1, QTableWidgetItem(str(row['date'])))  # editable
            type_item = QTableWidgetItem(str(row['transaction_type']))
            type_item.setFlags(no_edit)
            table.setItem(i, 2, type_item)
            table.setItem(i, 3, QTableWidgetItem(f"{float(row['amount']):,.0f}"))  # editable
            bal_item = QTableWidgetItem(f"{float(row['balance']):,.0f}")
            bal_item.setFlags(no_edit)
            table.setItem(i, 4, bal_item)
            table.setItem(i, 5, QTableWidgetItem(str(row['notes']) if row['notes'] else ""))  # editable
            is_deposit = row['transaction_type'] == 'Deposit'
            bg = QColor(self.theme_manager.get_color("success_bg" if is_deposit else "danger_bg"))
            fg = QColor(self.theme_manager.get_color("text_primary"))
            for c in range(6):
                table.item(i, c).setBackground(bg)
                table.item(i, c).setForeground(fg)

        table.itemChanged.connect(self.on_savings_item_changed)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table: self.open_savings_context_menu(pos, t))
        table.resizeRowsToContents()
        self.savings_scroll_layout.addWidget(table)
        self.savings_table = table

    # ==================================================================== #
    # Christmas fund tab
    # ==================================================================== #
    def _build_christmas_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        header = QHBoxLayout()
        self.christmas_label = QLabel("Christmas Fund — Balance: 0")
        self.christmas_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {self.theme_manager.get_color('success')};")
        header.addWidget(self.christmas_label)
        header.addStretch()
        header.addWidget(QLabel("Monthly:"))
        self.christmas_monthly_input = QLineEdit("2500")
        self.christmas_monthly_input.setMaximumWidth(90)
        header.addWidget(self.christmas_monthly_input)

        deposit_btn = QPushButton("Deposit")
        deposit_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white;")
        deposit_btn.clicked.connect(self.christmas_deposit_dialog)
        withdraw_btn = QPushButton("Withdraw")
        withdraw_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        withdraw_btn.clicked.connect(self.christmas_withdraw_dialog)
        catchup_btn = QPushButton("Catch Up")
        catchup_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('info')}; color: white;")
        catchup_btn.clicked.connect(self.christmas_catch_up)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(lambda: self._fund_edit_entry(self.christmas_table, "christmas_savings"))
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('warning')}; color: black;")
        delete_btn.clicked.connect(lambda: self._fund_delete_entry(self.christmas_table, "christmas_savings"))
        deleteall_btn = QPushButton("Delete All")
        deleteall_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        deleteall_btn.clicked.connect(lambda: self._fund_delete_all("christmas_savings"))
        for b in (deposit_btn, withdraw_btn, catchup_btn, edit_btn, delete_btn, deleteall_btn):
            header.addWidget(b)
        v.addLayout(header)

        self.christmas_hint = QLabel("")
        self.christmas_hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        v.addWidget(self.christmas_hint)

        self.christmas_table = QTableWidget()
        self.christmas_table.setColumnCount(5)
        self.christmas_table.setHorizontalHeaderLabels(["Date", "Type", "Amount", "Balance", "Notes"])
        self.christmas_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.christmas_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.christmas_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.christmas_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.christmas_table.customContextMenuRequested.connect(
            lambda pos: self._fund_context_menu(pos, self.christmas_table, "christmas_savings"))
        v.addWidget(self.christmas_table)
        return w

    def refresh_christmas(self):
        if self.current_individual_id is None:
            return
        import calendar
        svc = self.engine.christmas_service
        bal = svc.get_balance(self.current_individual_id)
        self.christmas_label.setText(f"Christmas Fund — Balance: {bal:,.0f}")
        self.christmas_hint.setText(
            f"Withdrawals are locked until {calendar.month_name[svc.get_unlock_month()]}.")
        self._populate_fund_table(
            self.christmas_table, svc.get_transactions(self.current_individual_id),
            [("Date", "date"), ("Type", "transaction_type"), ("Amount", "amount"),
             ("Balance", "balance"), ("Notes", "notes")])

    def christmas_deposit_dialog(self):
        if self.current_individual_id is None:
            return
        res = self._amount_date_dialog("Christmas Deposit", "Deposit:")
        if not res:
            return
        amount, date_str = res
        self.engine.christmas_service.add_deposit(self.current_individual_id, amount, date_str)
        self.refresh_table()

    def christmas_withdraw_dialog(self):
        if self.current_individual_id is None:
            return
        res = self._amount_date_dialog("Christmas Withdrawal", "Withdraw:")
        if not res:
            return
        amount, date_str = res
        svc = self.engine.christmas_service
        try:
            svc.add_withdrawal(self.current_individual_id, amount, date_str)
        except ChristmasLockedError as e:
            override = QMessageBox.question(
                self, "Withdrawals Locked",
                f"{e.message}.\n\nOverride and withdraw anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if override != QMessageBox.StandardButton.Yes:
                return
            svc.add_withdrawal(self.current_individual_id, amount, date_str, allow_override=True)
        self.refresh_table()

    def christmas_catch_up(self):
        if self.current_individual_id is None:
            return
        try:
            monthly = float(self.christmas_monthly_input.text())
        except ValueError:
            monthly = 0
        n = self.engine.christmas_service.catch_up(self.current_individual_id, monthly)
        self.refresh_table()
        if n == 0:
            QMessageBox.information(self, "Catch Up",
                                   "Nothing to catch up — add an initial deposit first.")

    # ==================================================================== #
    # Benevolent fund tab
    # ==================================================================== #
    def _build_benevolent_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)

        header = QHBoxLayout()
        self.benevolent_label = QLabel("Benevolent Fund — Total: 0")
        self.benevolent_label.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {self.theme_manager.get_color('accent')};")
        header.addWidget(self.benevolent_label)
        header.addStretch()

        enrol_btn = QPushButton("Enrol / Edit")
        enrol_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('accent')}; color: white;")
        enrol_btn.clicked.connect(self.benevolent_enroll_dialog)
        deduct_btn = QPushButton("Deduct")
        deduct_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('success')}; color: white;")
        deduct_btn.clicked.connect(self.benevolent_deduct)
        catchup_btn = QPushButton("Catch Up")
        catchup_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('info')}; color: white;")
        catchup_btn.clicked.connect(self.benevolent_catch_up)
        payout_btn = QPushButton("Payout")
        payout_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        payout_btn.clicked.connect(self.benevolent_payout)
        edit_btn = QPushButton("Edit")
        edit_btn.clicked.connect(lambda: self._fund_edit_entry(self.benevolent_table, "benevolent_ledger"))
        delete_btn = QPushButton("Delete")
        delete_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('warning')}; color: black;")
        delete_btn.clicked.connect(lambda: self._fund_delete_entry(self.benevolent_table, "benevolent_ledger"))
        deleteall_btn = QPushButton("Delete All")
        deleteall_btn.setStyleSheet(f"background-color: {self.theme_manager.get_color('danger')}; color: white;")
        deleteall_btn.clicked.connect(lambda: self._fund_delete_all("benevolent_ledger"))
        for b in (enrol_btn, deduct_btn, catchup_btn, payout_btn, edit_btn, delete_btn, deleteall_btn):
            header.addWidget(b)
        v.addLayout(header)

        self.benevolent_info = QLabel("Not enrolled.")
        self.benevolent_info.setStyleSheet("color: #94a3b8; font-size: 11px;")
        v.addWidget(self.benevolent_info)

        self.benevolent_table = QTableWidget()
        self.benevolent_table.setColumnCount(4)
        self.benevolent_table.setHorizontalHeaderLabels(["Date", "Amount", "Total", "Notes"])
        self.benevolent_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.benevolent_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.benevolent_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.benevolent_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.benevolent_table.customContextMenuRequested.connect(
            lambda pos: self._fund_context_menu(pos, self.benevolent_table, "benevolent_ledger"))
        v.addWidget(self.benevolent_table)
        return w

    def refresh_benevolent(self):
        if self.current_individual_id is None:
            return
        svc = self.engine.benevolent_service
        total = svc.get_total(self.current_individual_id)
        self.benevolent_label.setText(f"Benevolent Fund — Total Contributed: {total:,.0f}")
        acc = svc.get_account(self.current_individual_id)
        if acc and acc.get('active') and (acc.get('monthly_amount') or 0) > 0:
            self.benevolent_info.setText(
                f"Monthly contribution: {acc['monthly_amount']:,.0f}   |   Next due: {acc['next_due_date']}")
        else:
            self.benevolent_info.setText("Not enrolled — use 'Enrol / Edit' to set a monthly contribution.")
        self._populate_fund_table(
            self.benevolent_table, svc.get_transactions(self.current_individual_id),
            [("Date", "date"), ("Amount", "amount"), ("Total", "balance"), ("Notes", "notes")])

    def benevolent_enroll_dialog(self):
        if self.current_individual_id is None:
            return
        acc = self.engine.benevolent_service.get_account(self.current_individual_id)
        dlg = QDialog(self)
        dlg.setWindowTitle("Benevolent Enrolment")
        form = QFormLayout(dlg)
        amt = QDoubleSpinBox()
        amt.setRange(0.0, 1_000_000_000.0)
        amt.setGroupSeparatorShown(True)
        amt.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
        if acc:
            amt.setValue(float(acc.get('monthly_amount') or 0))
        start = QDateEdit()
        start.setCalendarPopup(True)
        if acc and acc.get('start_date'):
            start.setDate(QDate.fromString(acc['start_date'], "yyyy-MM-dd"))
        else:
            start.setDate(self.main_window.last_operation_date)
        form.addRow("Monthly amount:", amt)
        form.addRow("First contribution:", start)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if amt.value() <= 0:
            QMessageBox.warning(self, "Invalid", "Monthly amount must be greater than 0.")
            return
        self.engine.benevolent_service.enroll(
            self.current_individual_id, amt.value(), start.date().toString("yyyy-MM-dd"))
        self.refresh_table()

    def benevolent_deduct(self):
        if self.current_individual_id is None:
            return
        if not self.engine.benevolent_service.is_enrolled(self.current_individual_id):
            QMessageBox.information(self, "Not Enrolled",
                                   "Enrol this member in the Benevolent fund first.")
            return
        self.engine.benevolent_service.deduct_single(self.current_individual_id)
        self.refresh_table()

    def benevolent_catch_up(self):
        if self.current_individual_id is None:
            return
        if not self.engine.benevolent_service.is_enrolled(self.current_individual_id):
            QMessageBox.information(self, "Not Enrolled",
                                   "Enrol this member in the Benevolent fund first.")
            return
        n = self.engine.benevolent_service.catch_up(self.current_individual_id)
        self.refresh_table()
        if n == 0:
            QMessageBox.information(self, "Catch Up", "Already up to date.")

    def benevolent_payout(self):
        """Record a welfare payout (claim) from the fund against this member."""
        if self.current_individual_id is None:
            return
        res = self._amount_date_dialog("Benevolent Payout (Welfare Claim)", "Payout amount:")
        if not res:
            return
        amount, date = res
        self.engine.benevolent_service.add_payout(self.current_individual_id, amount, date)
        self.refresh_table()

    def buyoff_loan_btn(self, loan_ref):
        """Show dialog to buyoff/settle the loan fully."""
        loan = self.engine.db.get_loan_by_ref(self.current_individual_id, loan_ref)
        if not loan: return
        
        principal = loan['balance']
        accrued = loan.get('interest_balance', 0.0)
        future = loan.get('unearned_interest', 0.0)
        total_payoff = principal + accrued + future
        
        msg = (f"Are you sure you want to BUYOFF this loan?\n\n"
               f"Principal: {principal:,.0f}\n"
               f"Accrued Interest: {accrued:,.0f}\n"
               f"Future Interest: {future:,.0f}\n"
               f"---------------------------\n"
               f"TOTAL PAYOFF: {total_payoff:,.0f}\n\n"
               f"This will settle the loan inclusive of all future interest.")
               
        confirm = QMessageBox.question(self, "Confirm Buyoff", msg, 
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                                       
        if confirm == QMessageBox.StandardButton.Yes:
            # Use default date logic (Next Due Date) from LoanService
            if self.engine.buyoff_loan(self.current_individual_id, loan_ref, None):
                QMessageBox.information(self, "Success", "Loan Paid Off Successfully!")
                self.refresh_table()
                self.refresh_loans_list()
            else:
                QMessageBox.warning(self, "Error", "Could not process buyoff.")

    def suspend_loan_dialog(self, loan_id, loan_ref):
        """Suspend a loan for N months — ongoing, or backdated as a past event."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Suspend Loan {loan_ref}")
        dialog.setMinimumWidth(360)

        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"Suspend deductions for loan <b>{loan_ref}</b>:"))

        form = QFormLayout()

        start_input = QDateEdit()
        start_input.setCalendarPopup(True)
        # Default to where the loan actually is (its next due date), so
        # "suspend for N months" means the loan's next N due months — even when
        # back-filling a loan whose next due is in the past.
        loan_info = self.engine.db.get_loan_by_ref(self.current_individual_id, loan_ref)
        ndd = (loan_info or {}).get('next_due_date')
        start_qdate = QDate.fromString(ndd, "yyyy-MM-dd") if ndd else QDate()
        start_input.setDate(start_qdate if start_qdate.isValid() else QDate.currentDate())
        form.addRow("Suspended from:", start_input)

        months_spin = QSpinBox()
        months_spin.setRange(1, 60)
        months_spin.setValue(3)
        form.addRow("Suspend for (months):", months_spin)

        layout.addLayout(form)

        hint = QLabel("Tip: choose a past start date to record a suspension that already "
                      "happened — it will show as a no-deductions period on statements.")
        hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            months = months_spin.value()
            start_str = start_input.date().toString("yyyy-MM-dd")
            end_str = start_input.date().addMonths(months).toString("yyyy-MM-dd")
            today_str = QDate.currentDate().toString("yyyy-MM-dd")

            # If the window already has deductions, recording a suspension on its
            # own would only annotate — the existing deductions would contradict
            # it. Offer to rebuild the schedule so the suspension truly applies.
            existing = self.engine.db.count_deductions_in_period(
                self.current_individual_id, loan_ref, start_str, end_str)
            apply_rebuild = False
            if existing:
                ind_id = self.current_individual_id
                if self.engine.loan_service.has_loan_restructure_events(ind_id, loan_ref):
                    proceed = QMessageBox.question(self, "Deductions Found in Period",
                        f"{existing} deduction(s) exist for {loan_ref} between {start_str} and "
                        f"{end_str}, and this loan has top-ups / buy-offs so it can't be "
                        f"auto-rebuilt.\n\nRecord the suspension as a note only (it may "
                        f"contradict those deductions)?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if proceed != QMessageBox.StandardButton.Yes:
                        return
                else:
                    choice = QMessageBox.question(self, "Apply Suspension to Schedule",
                        f"There {'is' if existing == 1 else 'are'} {existing} deduction(s) recorded "
                        f"for {loan_ref} between {start_str} and {end_str}.\n\n"
                        f"Rebuild the loan's schedule to remove those deductions and apply the "
                        f"suspension (the loan term extends accordingly)?\n\n"
                        f"Yes = rebuild and apply    No = just record a note on statements",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                        | QMessageBox.StandardButton.Cancel)
                    if choice == QMessageBox.StandardButton.Cancel:
                        return
                    apply_rebuild = (choice == QMessageBox.StandardButton.Yes)

            kind = self.engine.db.record_suspension(loan_id, start_str, end_str, today=today_str)

            if apply_rebuild:
                try:
                    self.engine.loan_service.rebuild_loan_schedule(self.current_individual_id, loan_ref)
                    QMessageBox.information(self, "Suspension Applied",
                        f"Rebuilt {loan_ref}: deductions for the suspended months were removed "
                        f"and the loan term extended accordingly.")
                except ValueError as e:
                    QMessageBox.warning(self, "Rebuild Skipped", str(e))
            elif kind == 'historical':
                QMessageBox.information(self, "Past Suspension Recorded",
                    f"Recorded a past suspension for {loan_ref} from {start_str} to {end_str}.\n"
                    f"It will appear as a no-deductions period on statements covering it.")
            else:
                QMessageBox.information(self, "Suspended",
                    f"Loan {loan_ref} suspended from {start_str} until {end_str}.\n"
                    f"Deductions will be skipped for {months} month(s).")
            self.refresh_table()
            self.refresh_loans_list()

    def resume_loan_btn(self, loan_id):
        """Resume a suspended loan."""
        confirm = QMessageBox.question(self, "Resume Loan",
            "Are you sure you want to resume deductions for this loan?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if confirm == QMessageBox.StandardButton.Yes:
            self.engine.db.resume_loan(loan_id)
            QMessageBox.information(self, "Resumed", "Loan deductions have been resumed.")
            self.refresh_table()
            self.refresh_loans_list()



    def deduct_single_loan_btn(self, loan_ref):
        if self.engine.deduct_single_loan(self.current_individual_id, loan_ref):
            self.refresh_table()
            self.refresh_loans_list()
            # Success popup removed for better workflow
        else:
            QMessageBox.warning(self, "Error", "Could not deduct (is loan active?)")

    def deduct_multiplier_btn(self, loan_ref, spin_box):
        """Deduct multiple times based on spinbox value."""
        count = spin_box.value()
        if count < 1: return
        
        # Add progress dialog for multiple deductions
        progress = QProgressDialog(f"Processing {count} deductions...", "Cancel", 0, count, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        
        try:
            success_count = 0
            for i in range(count):
                if progress.wasCanceled():
                    break
                    
                progress.setLabelText(f"Deduction {i + 1} of {count}...")
                if self.engine.deduct_single_loan(self.current_individual_id, loan_ref):
                    success_count += 1
                else:
                    break # Stop if loan finished or error
                
                progress.setValue(i + 1)
                QApplication.processEvents()
            
            progress.close()
            
            if success_count > 0:
                self.refresh_table()
                self.refresh_loans_list()
                QMessageBox.information(self, "Success", f"Successfully processed {success_count} deduction(s).")
            else:
                QMessageBox.warning(self, "No Effect", "No deductions were processed (Loan might be Paid or fully caught up).")
                
        except Exception as e:
            progress.close()
            # Check for LoanInactiveError in string representation
            if "LoanInactiveError" in str(type(e)) or "not active" in str(e).lower():
                QMessageBox.warning(self, "Loan is Paid", f"Cannot process deduction: Loan is already Paid.")
            else:
                 QMessageBox.critical(self, "Error", f"Failed to deduct: {str(e)}")


    def on_item_changed(self, item):
        table = item.tableWidget()
        row = item.row()
        col = item.column()
        id_item = table.item(row, 2)
        if not id_item:
            return
        trans_id = id_item.data(Qt.ItemDataRole.UserRole)
        event_type = table.item(row, 1).text()
        
        # Block signals to prevent recursion
        table.blockSignals(True)
        
        try:
            # Helper to parse safe float from table cells (handling commas, -, + signs)
            def safe_float(txt):
                if not txt or txt == "-": return 0.0
                clean = txt.replace(',', '').replace('+', '')
                return float(clean)
            
            date = table.item(row, 0).text()
            
            # Identify columns
            # 3: Prin Delta, 4: Int Delta, 5: Payment
            p_text = table.item(row, 3).text()
            i_text = table.item(row, 4).text()
            pay_text = table.item(row, 5).text()
            
            notes = table.item(row, 9).text() # Notes is at 9
            
            if col == 0:
                 # Date Edit Logic (keep existing but safer?)
                 # ... existing date logic ...
                 # For brevity, I will copy exact logic but fix validation
                total_rows = table.rowCount()
                if row < total_rows - 1:
                    confirm = QMessageBox.question(self, "Cascade Dates",
                        f"Update all {total_rows - row - 1} entries below with monthly increments?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    
                    if confirm == QMessageBox.StandardButton.Yes:
                        from dateutil.relativedelta import relativedelta
                        from datetime import datetime as dt
                        
                        # Determine Shift Delta
                        # Fetch original date from DB to calculate shift
                        current_tx = self.engine.db.get_transaction(trans_id)
                        if not current_tx: return
                        
                        old_date_str = current_tx['date']
                        # Handle potential full timestamp if DB changed? Usually YYYY-MM-DD
                        try:
                            old_date_obj = dt.strptime(old_date_str, "%Y-%m-%d")
                        except ValueError:
                            # Fallback if DB has time
                            old_date_obj = dt.strptime(old_date_str.split()[0], "%Y-%m-%d")
                            
                        new_date_obj = dt.strptime(date, "%Y-%m-%d")
                        
                        # Use relativedelta for smart monthly shifting
                        shift_delta = relativedelta(new_date_obj, old_date_obj)
                        # Note: relativedelta(dt1, dt2) gives the difference.
                        # Adding this difference to another date applies the same relative shift (e.g. +1 month).
                        
                        # Batch Update
                        cursor = self.engine.db.conn.cursor()
                        
                        for i in range(row, total_rows):
                            r_id = table.item(i, 2).data(Qt.ItemDataRole.UserRole)
                            
                            # Get existing date from DB (safest)
                            r_tx = self.engine.db.get_transaction(r_id) # Optimization: could query all in 1 go, but this is fine
                            r_old_str = r_tx['date']
                            try:
                                r_old = dt.strptime(r_old_str, "%Y-%m-%d")
                            except ValueError:
                                r_old = dt.strptime(r_old_str.split()[0], "%Y-%m-%d")
                                
                            n_date_obj = r_old + shift_delta
                            n_date_str = n_date_obj.strftime("%Y-%m-%d")
                            
                            cursor.execute("UPDATE ledger SET date = ? WHERE id = ?", (n_date_str, r_id))
                        
                        self.engine.db.conn.commit()
                        self.engine.recalculate_balances(self.current_individual_id)
                        table.blockSignals(False)
                        from PyQt6.QtCore import QTimer
                        QTimer.singleShot(0, self.refresh_table)
                        return

            # Detect Edit Type
            new_amount = safe_float(pay_text)
            
            if event_type == "Repayment" and col == 5:
                 # Guard: Disallow zero/negative repayments
                 if new_amount <= 0:
                     msg = QMessageBox(self)
                     msg.setIcon(QMessageBox.Icon.Information)
                     msg.setWindowTitle("Zero Deduction Not Allowed")
                     msg.setTextFormat(Qt.TextFormat.RichText)
                     msg.setText(
                         "Setting a repayment to zero is not supported.<br><br>"
                         "If no deduction happened in a particular month, the correct "
                         "approach is to edit the <b>date</b> of this entry to the month "
                         "when the deduction actually occurred next.<br><br>"
                         "To do this:<br>"
                         "&nbsp;&nbsp;1. Double-click the <b>Date</b> cell of this entry<br>"
                         "&nbsp;&nbsp;2. Change it to the next month where a deduction took place<br>"
                         "&nbsp;&nbsp;3. Press Enter — subsequent dates will auto-cascade<br><br>"
                         "This preserves the correct loan balance and interest calculations."
                     )
                     msg.exec()
                     # Revert the cell to its previous value
                     tx = self.engine.get_transaction(trans_id)
                     if tx:
                         table.setItem(row, 5, QTableWidgetItem(f"{float(tx['deducted']):,.0f}"))
                     table.blockSignals(False)
                     return
                 
                 # Special logic for Payment Amount
                 self.engine.update_repayment_amount(self.current_individual_id, trans_id, new_amount, notes)
                 
            else:
                 # Generic Edit fallback (Date, Notes, or weird edits)
                 # Reconstruct added/deducted
                 added = 0.0
                 deducted = 0.0
                 
                 if event_type == "Loan Issued" or event_type == "Loan Top-Up":
                     added = safe_float(p_text)
                 elif event_type == "Interest Earned":
                     added = safe_float(i_text) # Interest is added logic
                 elif event_type == "Repayment":
                     deducted = safe_float(pay_text)
                 
                 # If user edited Payment on non-repayment row?
                 if col == 5 and event_type != "Repayment":
                      # Maybe they want to change deduction?
                      deducted = safe_float(pay_text)
                 
                 # Mark as edited ONLY if Amount/Payment columns changed (3, 4, 5)
                 # 3: Prin Delta (Added for TopUp), 4: Int Delta (Added for IntEarn), 5: Payment
                 should_mark_edited = (col in [3, 4, 5])
                 
                 self.engine.edit_transaction(self.current_individual_id, trans_id, date, added, deducted, notes, mark_edited=should_mark_edited)
            
            table.blockSignals(False)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self.refresh_table)
            
        except ValueError:
            table.blockSignals(False)
            QMessageBox.critical(self, "Error", "Invalid number.")
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self.refresh_table)

    def process_loan(self):
        try:
            p = float(self.amount_input.text())
            d = int(self.duration_input.text())
            dt = self.date_input.date().toString("yyyy-MM-dd")
            self.main_window.last_operation_date = self.date_input.date()
            
            interest_text = self.interest_input.text().strip()
            interest_rate = float(interest_text) / 100.0 if interest_text else 0.15
            
            deduction = self.engine.add_loan_event(self.current_individual_id, p, d, dt, interest_rate)
            self.refresh_table()
            self.refresh_loans_list()
            QMessageBox.information(self, "Success", f"New Monthly Deduction: {deduction:.2f}")
        except ValueError:
            QMessageBox.critical(self, "Error", "Please enter valid numbers.")



    def deduct_single_loan_btn(self, loan_ref):
        if self.engine.deduct_single_loan(self.current_individual_id, loan_ref):
            self.refresh_table()
            self.refresh_loans_list()
            # Success popup removed for better workflow
        else:
            QMessageBox.warning(self, "Warning", "Could not process deduction.")

    def delete_loan_btn(self, loan_ref):
        confirm = QMessageBox.question(self, "Confirm Delete", 
                                       f"Delete loan {loan_ref} and ALL its history?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            self.engine.delete_loan(self.current_individual_id, loan_ref)
            self.refresh_table()
            self.refresh_loans_list()



    def loans_catch_up_to_current(self, loan_ref=None):
        """Run auto deductions for all active loans from next_due_date to current month (exclusive)."""
        active_loans = self.engine.db.get_active_loans(self.current_individual_id)
        if loan_ref:
            active_loans = [l for l in active_loans if l['ref'] == loan_ref]

        if not active_loans:
            if loan_ref:
                QMessageBox.warning(self, "Warning", "This loan is not active or not found.")
            else:
                QMessageBox.warning(self, "Warning", "No active loans to catch up.")
            return
        
        from dateutil.relativedelta import relativedelta
        
        # Target: last day of previous month
        current_month_start = datetime.now().replace(day=1)
        to_date = (current_month_start - relativedelta(days=1)).strftime("%Y-%m-%d")
        
        total_deductions = 0
        loans_processed = 0
        
        # Add progress dialog for visual feedback
        progress = QProgressDialog("Processing loan catch-up...", "Cancel", 0, len(active_loans), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        
        for i, loan in enumerate(active_loans):
            if progress.wasCanceled():
                break
                
            progress.setLabelText(f"Processing {loan['ref']}...")
            from_date = loan['next_due_date']
            
            # Use auto_deduct_range which handles the date range properly
            count = self.engine.auto_deduct_range(
                self.current_individual_id, loan['ref'], from_date, to_date
            )
            if count > 0:
                total_deductions += count
            loans_processed += 1
            
            progress.setValue(i + 1)
            QApplication.processEvents()
        
        progress.close()
        
        if progress.wasCanceled():
            self.refresh_table()
            self.refresh_loans_list()
            QMessageBox.information(self, "Cancelled", f"Operation cancelled. Processed {total_deductions} deductions.")
        elif total_deductions == 0:
            QMessageBox.information(self, "Info", "All loans are already up to date!")
        else:
            self.refresh_table()
            self.refresh_loans_list()
            QMessageBox.information(self, "Success", f"Processed {total_deductions} deductions across {loans_processed} loan(s).")



    def undo_last_for_loan_btn(self, loan_ref):
        """Undo last transaction for a loan with confirmation dialog."""
        # Get last transaction details for informative confirmation
        ledger_df = self.engine.get_ledger_df(self.current_individual_id)
        if ledger_df.empty:
            QMessageBox.warning(self, "Warning", "No transactions to undo.")
            return
        
        loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref].sort_values(by=['date', 'id'])
        if loan_txs.empty:
            QMessageBox.warning(self, "Warning", "No transactions found for this loan.")
            return
        
        last_tx = loan_txs.iloc[-1]
        
        # Build informative message
        event_type = last_tx['event_type']
        date = last_tx['date']
        amount = last_tx.get('deducted', 0) or last_tx.get('added', 0)
        
        msg = (f"Undo the last transaction for <b>{loan_ref}</b>?\n\n"
               f"<b>Type:</b> {event_type}\n"
               f"<b>Date:</b> {date}\n"
               f"<b>Amount:</b> {amount:,.0f}\n\n"
               f"This action will remove this transaction from the ledger.")
        
        confirm = QMessageBox.question(self, "Confirm Undo", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if confirm != QMessageBox.StandardButton.Yes:
            return
        
        try:
            # Use undo_transaction_with_state to capture loan state for redo capability
            trans_id = int(last_tx['id'])
            if self.engine.undo_transaction_with_state(self.current_individual_id, trans_id):
                self.refresh_table()
                self.refresh_loans_list()
                # QMessageBox.information(self, "Deleted", 
                #     f"Transaction deleted. Press Ctrl+Z to restore if needed.")
                pass
            else:
                QMessageBox.warning(self, "Warning", "Failed to undo transaction.")
        except ValueError as e:
            QMessageBox.warning(self, "Cannot Undo", str(e))

    def auto_deduct_dialog(self, loan_ref):
        loan = self.engine.db.get_loan_by_ref(self.current_individual_id, loan_ref)
        if not loan or loan['status'] != 'Active':
            QMessageBox.warning(self, "Warning", "Loan is not active.")
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Auto Deduct - {loan_ref}")
        layout = QFormLayout(dialog)
        
        layout.addRow(QLabel(f"<b>Balance:</b> {loan['balance']:,.0f}"))
        layout.addRow(QLabel(f"<b>Installment:</b> {loan['installment']:,.0f}"))
        
        from PyQt6.QtWidgets import QRadioButton, QButtonGroup

        mode_group = QButtonGroup(dialog)
        rb_months = QRadioButton("By Number of Months")
        rb_range = QRadioButton("By Date Range")
        mode_group.addButton(rb_months)
        mode_group.addButton(rb_range)
        rb_months.setChecked(True)
        
        layout.addRow(rb_months)
        layout.addRow(rb_range)
        
        # Stacked widgets or just hiding/showing rows
        months_widget = QWidget()
        months_layout = QFormLayout(months_widget)
        months_input = QLineEdit("1")
        months_layout.addRow("Number of Months:", months_input)
        layout.addRow(months_widget)
        
        range_widget = QWidget()
        range_layout = QFormLayout(range_widget)
        
        from_date_input = QDateEdit()
        from_date_input.setCalendarPopup(True)
        from_date_input.setDate(QDate.fromString(loan['next_due_date'], "yyyy-MM-dd"))
        range_layout.addRow("Start Date:", from_date_input)
        
        to_date_input = QDateEdit()
        to_date_input.setCalendarPopup(True)
        to_date_input.setDate(self.main_window.last_operation_date)
        range_layout.addRow("End Date:", to_date_input)
        
        layout.addRow(range_widget)
        range_widget.hide()
        
        def toggle_mode():
            if rb_months.isChecked():
                months_widget.show()
                range_widget.hide()
            else:
                months_widget.hide()
                range_widget.show()
        
        rb_months.toggled.connect(toggle_mode)
        rb_range.toggled.connect(toggle_mode)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Run Auto Deduct")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                from dateutil.relativedelta import relativedelta
                
                if rb_months.isChecked():
                    months = int(months_input.text())
                    if months <= 0:
                        raise ValueError
                    
                    # Start from next due date
                    start_date = datetime.strptime(loan['next_due_date'], "%Y-%m-%d").date()
                    # End date is start + (months-1)
                    end_date = start_date + relativedelta(months=months-1)
                    
                    from_date_str = start_date.strftime("%Y-%m-%d")
                    to_date_str = end_date.strftime("%Y-%m-%d")
                    
                else:
                    from_date_str = from_date_input.date().toString("yyyy-MM-dd")
                    to_date_str = to_date_input.date().toString("yyyy-MM-dd")
                    self.main_window.last_operation_date = to_date_input.date()

                count = self.engine.auto_deduct_range(
                    self.current_individual_id, loan_ref, 
                    from_date_str, to_date_str
                )
                
                if count > 0:
                    QMessageBox.information(self, "Success", f"Processed {count} deduction(s).")
                    self.refresh_table()
                    self.refresh_loans_list()
                else:
                    QMessageBox.information(self, "Info", "No deductions were made (check loan balance).")
            except ValueError:
                QMessageBox.critical(self, "Error", "Invalid input.")

    def top_up_loan_dialog(self, loan_ref):
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Top-Up Loan {loan_ref}")
        layout = QFormLayout(dialog)
        
        amount_input = QLineEdit()
        duration_input = QLineEdit()
        layout.addRow("Top-Up Amount:", amount_input)
        layout.addRow("New Duration (Months):", duration_input)
        
        date_input = QDateEdit()
        date_input.setCalendarPopup(True)
        
        # Smart Default Date: Last Transaction + 1 Month
        try:
            from dateutil.relativedelta import relativedelta
            from datetime import datetime as dt
            from PyQt6.QtCore import QDate
            
            ledger_df = self.engine.get_ledger_df(self.current_individual_id)
            if not ledger_df.empty:
                # Filter for THIS loan to get context-aware date (Fix for Issue #525)
                # If we have transactions for this loan, use its last date.
                # If not (new loan?), fallback to global last or last op.
                loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref]
                
                if not loan_txs.empty:
                    # Ensure sorted (though DB now handles it, being safe locally is good)
                    loan_txs = loan_txs.sort_values(by=['date', 'id'])
                    last_date_str = loan_txs.iloc[-1]['date']
                else:
                    # Fallback to global last if loan has no history yet?
                    # Or maybe global last is safer than nothing?
                    # Let's use global last if loan is empty.
                    last_date_str = ledger_df.iloc[-1]['date']

                try:
                    last_date_obj = dt.strptime(last_date_str, "%Y-%m-%d")
                except ValueError:
                    last_date_obj = dt.strptime(last_date_str.split()[0], "%Y-%m-%d")
                
                default_date_obj = last_date_obj + relativedelta(months=1)
                default_qdate = QDate(default_date_obj.year, default_date_obj.month, default_date_obj.day)
                date_input.setDate(default_qdate)
            else:
                date_input.setDate(self.main_window.last_operation_date)
        except Exception as e:
            # Fallback
            date_input.setDate(self.main_window.last_operation_date)

        layout.addRow("Top-Up Date:", date_input)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Top-Up")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                amount = float(amount_input.text())
                duration = int(duration_input.text())
                date_str = date_input.date().toString("yyyy-MM-dd")
                self.main_window.last_operation_date = date_input.date()

                # Validation: Prevent Top-Up back in time
                ledger_df = self.get_ledger_df(self.current_individual_id)
                if not ledger_df.empty:
                    loan_txs = ledger_df[ledger_df['loan_id'] == loan_ref]
                    if not loan_txs.empty:
                        loan_txs = loan_txs.sort_values(by=['date', 'id'])
                        last_tx_date = loan_txs.iloc[-1]['date']
                        
                        # Parse dates to compare months
                        try:
                            last_c_date = datetime.strptime(last_tx_date, "%Y-%m-%d")
                        except ValueError:
                            # Handle potential timestamp format in DB
                            last_c_date = datetime.strptime(last_tx_date.split()[0], "%Y-%m-%d")
                            
                        new_c_date = date_input.date().toPyDate()
                        # Convert both to (year, month) tuples
                        last_month_key = (last_c_date.year, last_c_date.month)
                        new_month_key = (new_c_date.year, new_c_date.month)
                        
                        # Logic: New Date Must be > Last Date AND Not in Same Month
                        # Simply: new_month_key must be > last_month_key
                        
                        if new_month_key <= last_month_key:
                             QMessageBox.information(self, "Restricted Action", 
                                f"Cannot add a top-up in the same month (or earlier) as the last transaction.\n\n"
                                f"Last Transaction: {last_tx_date}\n"
                                f"Top-ups must occur in a subsequent month (forward in time).")
                             return
                
                if amount <= 0 or duration <= 0:
                    raise ValueError
                if self.engine.top_up_loan(self.current_individual_id, loan_ref, amount, duration, date_str):
                    QMessageBox.information(self, "Success", "Loan topped up!")
                    self.refresh_table()
                    self.refresh_loans_list()
            except ValueError:
                QMessageBox.critical(self, "Error", "Invalid input.")






    def open_context_menu(self, position, table):
        menu = QMenu()
        
        # Determine which table triggered this
        is_savings = (table == self.savings_table)
        
        if is_savings:
            edit_action = QAction("Edit Entry", self)
            edit_action.triggered.connect(lambda: self.edit_savings_entry(table))
        else:
            edit_action = QAction("Edit Entry", self)
            edit_action.triggered.connect(lambda: self.edit_loan_entry(table))
        menu.addAction(edit_action)
        
        if is_savings:
            info_action = QAction("Info", self)
            info_action.triggered.connect(lambda: self.show_savings_info(table))
            menu.addAction(info_action)
            
        delete_action = QAction("Delete Entry", self)
        delete_action.triggered.connect(lambda: self.delete_entry(table))
        menu.addAction(delete_action)
        
        menu.exec(table.viewport().mapToGlobal(position))

    def edit_loan_entry(self, table):
        row = table.currentRow()
        if row < 0: return
        
        # ID is in col 2 (hidden or visible?)
        trans_id = table.item(row, 2).data(Qt.ItemDataRole.UserRole)
        tx = self.db.get_transaction(trans_id)
        if not tx: return
        
        if tx['event_type'] == "Loan Top-Up":
            self.edit_top_up_dialog(tx)
        elif tx['event_type'] == "Repayment":
            # Constraint check
            is_latest = self.engine.is_latest_repayment(self.current_individual_id, tx['loan_id'], trans_id)
            if not is_latest:
                from PyQt6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "Edit Restricted", "You can only edit the most recent repayment transaction for this loan.\n\nTo correct older entries, please delete or undo newer transactions first.")
                return
            self.edit_generic_transaction(tx)
        else:
            # Generic Edit (Amount, Date, Notes)
            self.edit_generic_transaction(tx)
            
    def edit_top_up_dialog(self, tx):
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Loan Top-Up")
        layout = QFormLayout(dialog)
        
        amount_input = QLineEdit(str(tx['added']))
        layout.addRow("Amount:", amount_input)
        
        # Extract duration from notes?
        import re
        duration = "12" 
        match = re.search(r"Duration: (\d+)m", tx['notes'])
        if match:
             duration = match.group(1)
        duration_input = QLineEdit(duration)
        layout.addRow("Duration (Months):", duration_input)
        
        date_input = QDateEdit()
        date_input.setCalendarPopup(True)
        date_input.setDate(QDate.fromString(tx['date'], "yyyy-MM-dd"))
        layout.addRow("Date:", date_input)
        
        notes_input = QLineEdit(tx['notes'])
        layout.addRow("Notes:", notes_input)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                new_amt = float(amount_input.text())
                new_dur = int(duration_input.text())
                new_date = date_input.date().toString("yyyy-MM-dd")
                new_notes = notes_input.text()
                
                # Update notes with new duration if format matches standard
                if "Duration:" in new_notes:
                    new_notes = re.sub(r"Duration: \d+m", f"Duration: {new_dur}m", new_notes)
                else:
                    new_notes += f" (Duration: {new_dur}m)"
                
                self.engine.update_loan_transaction(tx['id'], new_date, new_amt, new_notes, new_dur)
                self.refresh_table()
                self.refresh_loans_list()
                QMessageBox.information(self, "Success", "Top-Up updated and loan recalculated.")
            except ValueError:
                QMessageBox.critical(self, "Error", "Invalid input.")

    def edit_generic_transaction(self, tx):
        QMessageBox.information(self, "Info", "Editing this transaction type is not yet fully supported (only Top-Up editing triggers cascade).")


        
    def edit_savings_entry(self, table):
        row = table.currentRow()
        if row < 0: return
        # Savings ID in col 0? Check table setup.
        # In on_savings_item_changed: id_item = table.item(row, 0)
        id_item = table.item(row, 0)
        if not id_item: return
        trans_id = id_item.data(Qt.ItemDataRole.UserRole)
        
        # Fetch current data
        # We can get from text or DB. DB is safer.
        # Savings doesn't have `get_transaction` for savings table specifically? 
        # `db.get_savings_transactions` gets all.
        # `db.get_transaction` is for ledger table (loans).
        # We need `get_savings_transaction` in DB or just filter.
        # Or parse from table.
        current_date = table.item(row, 1).text()
        current_amt = float(table.item(row, 3).text().replace(',', ''))
        current_notes = table.item(row, 5).text()
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Savings Entry")
        layout = QFormLayout(dialog)
        
        amount_input = QLineEdit(str(current_amt))
        layout.addRow("Amount:", amount_input)
        
        date_input = QDateEdit()
        date_input.setCalendarPopup(True)
        date_input.setDate(QDate.fromString(current_date, "yyyy-MM-dd"))
        layout.addRow("Date:", date_input)
        
        notes_input = QLineEdit(current_notes)
        layout.addRow("Notes:", notes_input)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Save")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
             try:
                 new_amt = float(amount_input.text())
                 new_date = date_input.date().toString("yyyy-MM-dd")
                 new_note = notes_input.text()
                 
                 # We need update_savings_transaction in DB
                 self.db.update_savings_transaction(trans_id, new_date, new_amt, new_note)
                 self.db.recalculate_savings_balances(self.current_individual_id)
                 self.refresh_table()
                 self.refresh_savings_balance()
             except ValueError:
                 QMessageBox.critical(self, "Error", "Invalid Amount")

    def show_savings_info(self, table):
        row = table.currentRow()
        if row < 0: return
        
        date = table.item(row, 1).text()
        amt = table.item(row, 3).text()
        notes = table.item(row, 5).text()
        bal = table.item(row, 4).text() # Balance column is 4
        
        info = f"<b>Date:</b> {date}<br>"
        info += f"<b>Amount:</b> {amt}<br>"
        info += f"<b>Notes:</b> {notes}<br>"
        info += f"<b>Running Balance:</b> {bal}<br>"
        
        QMessageBox.information(self, "Savings Entry Info", info)

    def delete_entry(self, table):
        row = table.currentRow()
        if row < 0:
            return
            
        if table == self.savings_table:
             id_item = table.item(row, 0)
             trans_id = id_item.data(Qt.ItemDataRole.UserRole)
             confirm = QMessageBox.question(self, "Confirm", "Delete this savings entry?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
             if confirm == QMessageBox.StandardButton.Yes:
                 self.db.delete_savings_transaction(trans_id)
                 self.db.recalculate_savings_balances(self.current_individual_id)
                 self.refresh_savings_balance()
                 self.refresh_table()
        else:
            trans_id = table.item(row, 2).data(Qt.ItemDataRole.UserRole)
            
            confirm = QMessageBox.question(self, "Confirm", "Delete this transaction?",
                                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirm == QMessageBox.StandardButton.Yes:
                self.engine.delete_transaction(self.current_individual_id, trans_id)
                self.refresh_table()
                self.refresh_loans_list()






    # ========== SAVINGS METHODS ==========
    
    def refresh_savings_balance(self):
        """Update the savings balance display."""
        balance = self.db.get_savings_balance(self.current_individual_id)
        self.savings_label.setText(f"Savings / Shares — Balance: {balance:,.0f}")
    
    def savings_deposit_dialog(self):
        """Dialog to add a deposit."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Deposit")
        layout = QFormLayout(dialog)
        
        amount_input = QLineEdit()
        amount_input.setPlaceholderText("Amount")
        layout.addRow("Amount:", amount_input)
        
        date_input = QDateEdit()
        date_input.setCalendarPopup(True)
        date_input.setDate(self.main_window.last_operation_date)
        layout.addRow("Date:", date_input)
        
        notes_input = QLineEdit()
        notes_input.setPlaceholderText("Optional notes")
        layout.addRow("Notes:", notes_input)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Deposit")
        ok_btn.setStyleSheet("background-color: #28a745; color: white;")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                amount = float(amount_input.text())
                if amount <= 0:
                    raise ValueError
                date = date_input.date().toString("yyyy-MM-dd")
                self.main_window.last_operation_date = date_input.date()
                notes = notes_input.text().strip()
                
                new_balance = self.db.add_savings_transaction(
                    self.current_individual_id, date, "Deposit", amount, notes
                )
                self.refresh_savings_balance()
                self.refresh_table()  # Refresh the table display
                QMessageBox.information(self, "Success", f"Deposited {amount:,.0f}. New balance: {new_balance:,.0f}")
            except ValueError:
                QMessageBox.critical(self, "Error", "Please enter a valid amount.")
    
    def savings_withdraw_dialog(self):
        """Dialog to make a withdrawal."""
        current_balance = self.db.get_savings_balance(self.current_individual_id)
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Withdraw")
        layout = QFormLayout(dialog)
        
        layout.addRow(QLabel(f"<b>Current Balance:</b> {current_balance:,.0f}"))
        
        amount_input = QLineEdit()
        amount_input.setPlaceholderText("Amount")
        layout.addRow("Amount:", amount_input)
        
        date_input = QDateEdit()
        date_input.setCalendarPopup(True)
        
        # Determine default date (use date of last transaction if available)
        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        if not savings_df.empty:
            last_date_str = savings_df.iloc[-1]['date']
            default_date = QDate.fromString(last_date_str, "yyyy-MM-dd")
        else:
            default_date = QDate.currentDate()
            
        date_input.setDate(default_date)
        layout.addRow("Date:", date_input)
        
        notes_input = QLineEdit()
        notes_input.setPlaceholderText("Optional notes")
        layout.addRow("Notes:", notes_input)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Withdraw")
        ok_btn.setStyleSheet("background-color: #dc3545; color: white;")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                amount = float(amount_input.text())
                if amount <= 0:
                    raise ValueError
                if amount > current_balance:
                    QMessageBox.warning(self, "Warning", "Insufficient balance!")
                    return
                    
                date = date_input.date().toString("yyyy-MM-dd")
                self.main_window.last_operation_date = date_input.date()
                notes = notes_input.text().strip()
                if not notes:
                    notes = "Withdrawal"
                
                new_balance = self.db.add_savings_transaction(
                    self.current_individual_id, date, "Withdrawal", amount, notes
                )
                self.refresh_savings_balance()
                self.refresh_table()  # Refresh the table display
                QMessageBox.information(self, "Success", f"Withdrew {amount:,.0f}. New balance: {new_balance:,.0f}")
            except ValueError:
                QMessageBox.critical(self, "Error", "Please enter a valid amount.")
    
    def savings_quick_increment(self):
        """Quick increment savings by the configured monthly amount - uses next month after last entry."""
        try:
            amount = float(self.savings_increment_input.text())
            if amount <= 0:
                raise ValueError
            
            # Get last DEPOSIT date and add 1 month (withdrawals don't shift deposit schedule)
            savings_df = self.db.get_savings_transactions(self.current_individual_id)
            deposits_df = savings_df[savings_df['transaction_type'] == 'Deposit'] if not savings_df.empty else savings_df
            if deposits_df.empty:
                # No deposit entries, use current date
                date = datetime.now().strftime("%Y-%m-%d")
            else:
                from dateutil.relativedelta import relativedelta
                last_date_str = deposits_df.iloc[-1]['date']
                last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
                next_date = last_date + relativedelta(months=1)
                date = next_date.strftime("%Y-%m-%d")
            
            new_balance = self.db.add_savings_transaction(
                self.current_individual_id, date, "Deposit", amount, "Monthly Contribution"
            )
            self.refresh_savings_balance()
            self.refresh_table()
            # No confirmation dialog as requested
        except ValueError:
            QMessageBox.critical(self, "Error", "Please enter a valid increment amount.")

    
    def savings_auto_increment_dialog(self):
        """Auto-increment savings from a past date to present."""
        try:
            increment_amount = float(self.savings_increment_input.text())
            if increment_amount <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, "Error", "Please set a valid monthly increment amount first.")
            return
        
        from PyQt6.QtWidgets import QRadioButton, QButtonGroup

        dialog = QDialog(self)
        dialog.setWindowTitle("Auto Increment Savings")
        layout = QFormLayout(dialog)
        
        layout.addRow(QLabel(f"<b>Monthly Amount:</b> {increment_amount:,.0f}"))
        
        mode_group = QButtonGroup(dialog)
        rb_months = QRadioButton("By Number of Months")
        rb_range = QRadioButton("By Date Range")
        mode_group.addButton(rb_months)
        mode_group.addButton(rb_range)
        rb_months.setChecked(True)
        
        layout.addRow(rb_months)
        layout.addRow(rb_range)
        
        # Determine default start date (next month after last DEPOSIT, not withdrawal)
        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        default_start = QDate.currentDate()
        if not savings_df.empty:
            deposits_df = savings_df[savings_df['transaction_type'] == 'Deposit']
            if not deposits_df.empty:
                last_date_str = deposits_df.iloc[-1]['date']
            else:
                last_date_str = savings_df.iloc[-1]['date']
            last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
            from dateutil.relativedelta import relativedelta
            next_date = last_date + relativedelta(months=1)
            default_start = QDate(next_date.year, next_date.month, next_date.day)
        
        # Stacked widgets
        months_widget = QWidget()
        months_layout = QFormLayout(months_widget)
        months_input = QLineEdit("6")
        months_layout.addRow("Number of Months:", months_input)
        layout.addRow(months_widget)
        
        range_widget = QWidget()
        range_layout = QFormLayout(range_widget)
        
        from_date_input = QDateEdit()
        from_date_input.setCalendarPopup(True)
        from_date_input.setDate(default_start)
        range_layout.addRow("Start Date:", from_date_input)
        
        to_date_input = QDateEdit()
        to_date_input.setCalendarPopup(True)
        to_date_input.setDate(self.main_window.last_operation_date)
        range_layout.addRow("End Date:", to_date_input)
        
        layout.addRow(range_widget)
        range_widget.hide()
        
        def toggle_mode():
            if rb_months.isChecked():
                months_widget.show()
                range_widget.hide()
            else:
                months_widget.hide()
                range_widget.show()
        
        rb_months.toggled.connect(toggle_mode)
        rb_range.toggled.connect(toggle_mode)
        
        btn_box = QHBoxLayout()
        ok_btn = QPushButton("Run Auto Increment")
        ok_btn.setStyleSheet("background-color: #28a745; color: white;")
        cancel_btn = QPushButton("Cancel")
        btn_box.addWidget(ok_btn)
        btn_box.addWidget(cancel_btn)
        layout.addRow(btn_box)
        
        ok_btn.clicked.connect(dialog.accept)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                from dateutil.relativedelta import relativedelta
                
                count = 0
                
                if rb_months.isChecked():
                    months = int(months_input.text())
                    if months <= 0:
                        raise ValueError
                    
                    # Start from default calculated start date
                    current_date = default_start.toPyDate()
                    
                    for _ in range(months):
                        date_str = current_date.strftime("%Y-%m-%d")
                        self.db.add_savings_transaction(
                            self.current_individual_id, date_str, "Deposit", 
                            increment_amount, "Monthly Contribution (Auto)"
                        )
                        count += 1
                        current_date = current_date + relativedelta(months=1)
                        
                else:
                    from_date = from_date_input.date().toPyDate()
                    to_date = to_date_input.date().toPyDate()
                    self.main_window.last_operation_date = to_date_input.date()
                    
                    current_date = from_date
                    while current_date <= to_date:
                        date_str = current_date.strftime("%Y-%m-%d")
                        self.db.add_savings_transaction(
                            self.current_individual_id, date_str, "Deposit", 
                            increment_amount, "Monthly Contribution (Auto)"
                        )
                        count += 1
                        current_date = current_date + relativedelta(months=1)
                
                self.refresh_savings_balance()
                self.refresh_table()
                new_balance = self.db.get_savings_balance(self.current_individual_id)
                QMessageBox.information(self, "Success", f"Added {count} deposits of {increment_amount:,.0f} each.\nNew balance: {new_balance:,.0f}")
            except ValueError:
                QMessageBox.critical(self, "Error", "Invalid input.")
    
    # ========== SAVINGS CRUD HANDLERS ==========
    
    def edit_savings_entry_btn(self, table):
        """Trigger edit from button."""
        row = table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Selection", "Please select a row to edit.")
            return
        self.edit_savings_entry(table)

    def edit_loan_entry_btn(self, table):
        """Trigger loan edit from button."""
        row = table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Selection", "Please select an event to edit.")
            return
        self.edit_loan_entry(table)



    def on_savings_item_changed(self, item):
        """Handle editing of savings table items."""
        table = item.tableWidget()
        row = item.row()
        col = item.column()
        
        # Only allow editing date (1), amount (3), and notes (5)
        if col not in [1, 3, 5]:
            return
        
        id_item = table.item(row, 0)
        if not id_item:
            return
        trans_id = id_item.data(Qt.ItemDataRole.UserRole)
        
        # Block signals to prevent recursion
        table.blockSignals(True)
        
        try:
            new_date = table.item(row, 1).text()
            new_amount = float(table.item(row, 3).text().replace(',', ''))
            new_notes = table.item(row, 5).text()
            
            # If date column was edited, ask if user wants to cascade update
            if col == 1:
                total_rows = table.rowCount()
                if row < total_rows - 1:
                    confirm = QMessageBox.question(self, "Cascade Dates",
                        f"Update all {total_rows - row - 1} entries below with monthly increments?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    
                    if confirm == QMessageBox.StandardButton.Yes:
                        from dateutil.relativedelta import relativedelta
                        from datetime import datetime as dt
                        
                        base_date = dt.strptime(new_date, "%Y-%m-%d")
                        cursor = self.db.conn.cursor()
                        
                        # Update all rows from current to end
                        for i in range(row, total_rows):
                            row_trans_id = table.item(i, 0).data(Qt.ItemDataRole.UserRole)
                            calc_date = (base_date + relativedelta(months=(i - row))).strftime("%Y-%m-%d")
                            
                            row_amount = float(table.item(i, 3).text().replace(',', ''))
                            row_notes = table.item(i, 5).text()
                            
                            cursor.execute("""
                                UPDATE savings SET date=?, amount=?, notes=? WHERE id=?
                            """, (calc_date, row_amount, row_notes, row_trans_id))
                        
                        self.db.conn.commit()
                        self.db.recalculate_savings_balances(self.current_individual_id)
                        table.blockSignals(False)
                        QTimer.singleShot(0, self.refresh_table)
                        self.refresh_savings_balance()
                        return
            
            # Update the single transaction
            cursor = self.db.conn.cursor()
            cursor.execute("""
                UPDATE savings SET date=?, amount=?, notes=? WHERE id=?
            """, (new_date, new_amount, new_notes, trans_id))
            self.db.conn.commit()
            
            # Recalculate balances
            self.db.recalculate_savings_balances(self.current_individual_id)
            table.blockSignals(False)
            QTimer.singleShot(0, self.refresh_table)
            self.refresh_savings_balance()
        except ValueError:
            table.blockSignals(False)
            QMessageBox.critical(self, "Error", "Invalid value entered.")
            QTimer.singleShot(0, self.refresh_table)


    
    def open_savings_context_menu(self, position, table):
        """Context menu for savings table."""
        menu = QMenu()
        delete_action = QAction("Delete Entry", self)
        delete_action.triggered.connect(lambda: self.delete_savings_entry(table))
        menu.addAction(delete_action)
        menu.exec(table.viewport().mapToGlobal(position))
    
    def delete_savings_entry(self, table):
        """Delete a savings entry."""
        row = table.currentRow()
        if row < 0:
            return
        
        trans_id = table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        confirm = QMessageBox.question(self, "Confirm", "Delete this savings entry?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            self.db.delete_savings_transaction(trans_id)
            self.db.recalculate_savings_balances(self.current_individual_id)
            self.refresh_table()
            self.refresh_savings_balance()
    
    def undo_last_savings(self):
        """Undo the last savings transaction with confirmation dialog."""
        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        if savings_df.empty:
            QMessageBox.information(self, "Info", "No savings transactions to undo.")
            return
        
        last_trans = savings_df.iloc[-1]
        
        # Build informative confirmation message
        trans_type = last_trans['transaction_type']
        date = last_trans['date']
        amount = last_trans['amount']
        
        msg = (f"Undo the last savings transaction?\n\n"
               f"Type: {trans_type}\n"
               f"Date: {date}\n"
               f"Amount: {amount:,.0f}\n\n"
               f"This action will remove this entry from savings.")
        
        confirm = QMessageBox.question(self, "Confirm Undo", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        
        if confirm != QMessageBox.StandardButton.Yes:
            return
        
        self.db.delete_savings_transaction(int(last_trans['id']))
        self.db.recalculate_savings_balances(self.current_individual_id)
        self.refresh_table()
        self.refresh_savings_balance()
    
    def clear_all_savings(self):
        """Delete all savings transactions for this individual."""
        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        if savings_df.empty:
            QMessageBox.information(self, "Info", "No savings transactions to clear.")
            return
        
        confirm = QMessageBox.question(self, "Confirm Clear All", 
                                       f"Delete ALL {len(savings_df)} savings entries?\nThis cannot be undone!",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            cursor = self.db.conn.cursor()
            cursor.execute("DELETE FROM savings WHERE individual_id=?", (self.current_individual_id,))
            self.db.conn.commit()
            self.refresh_table()
            self.refresh_savings_balance()
            QMessageBox.information(self, "Success", "All savings entries deleted.")
    
    def savings_catch_up_to_current(self):
        """Auto-increment savings from last entry up to (but not including) current month."""
        try:
            amount = float(self.savings_increment_input.text())
            if amount <= 0:
                raise ValueError("Invalid amount")
        except ValueError:
            QMessageBox.critical(self, "Error", "Please set a valid monthly increment amount first.")
            return
        
        savings_df = self.db.get_savings_transactions(self.current_individual_id)
        if savings_df.empty:
            QMessageBox.warning(self, "Warning", "No savings entries exist. Use 'Auto Increment...' to start from a specific date.")
            return
        
        from dateutil.relativedelta import relativedelta
        
        # Use last DEPOSIT date, not last transaction (withdrawals don't shift deposit schedule)
        deposits_df = savings_df[savings_df['transaction_type'] == 'Deposit']
        if not deposits_df.empty:
            last_date_str = deposits_df.iloc[-1]['date']
        else:
            last_date_str = savings_df.iloc[-1]['date']
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d")
        
        # Ask for Target Date
        # Default to Current Month
        default_target = datetime.now()
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Catch Up Savings")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Catch up savings deposits until:"))
        
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDate(QDate(default_target.year, default_target.month, default_target.day))
        date_edit.setDisplayFormat("yyyy-MM-dd")
        layout.addWidget(date_edit)
        
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
            
        target_date_q = date_edit.date()
        target_date = datetime(target_date_q.year(), target_date_q.month(), target_date_q.day())
        
        # Target Limit: specific date provided + 1 month (to include the target month)
        # E.g. Target Feb 2025 -> Limit Mar 1 2025 (Exclusive)
        current_month_start = (target_date + relativedelta(months=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Calculate how many months to process
        current_date = last_date + relativedelta(months=1)
        total_months = 0
        temp_date = current_date
        while temp_date < current_month_start:
            total_months += 1
            temp_date = temp_date + relativedelta(months=1)
        
        if total_months == 0:
            QMessageBox.information(self, "Info", "Already up to date! Last entry is from current or previous month.")
            return
        
        # Add progress dialog
        progress = QProgressDialog("Processing savings catch-up...", "Cancel", 0, total_months, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        
        count = 0
        while current_date < current_month_start:
            if progress.wasCanceled():
                break
                
            date_str = current_date.strftime("%Y-%m-%d")
            progress.setLabelText(f"Adding deposit for {date_str}...")
            
            self.db.add_savings_transaction(
                self.current_individual_id, date_str, "Deposit",
                amount, "Monthly Contribution (Catch-up)"
            )
            count += 1
            current_date = current_date + relativedelta(months=1)
            
            progress.setValue(count)
            QApplication.processEvents()
        
        progress.close()
        
        self.refresh_savings_balance()
        self.refresh_table()
        new_balance = self.db.get_savings_balance(self.current_individual_id)
        
        if progress.wasCanceled():
            QMessageBox.information(self, "Cancelled", f"Operation cancelled. Added {count} deposits.\nNew balance: {new_balance:,.0f}")
        else:
            QMessageBox.information(self, "Success", f"Added {count} deposits of {amount:,.0f} each.\nNew balance: {new_balance:,.0f}")

    
    def calculate_principal_dialog(self):
        """Calculate principal from monthly deduction, months, and interest rate."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Calculate Principal")
        dialog.setMinimumWidth(300)
        layout = QVBoxLayout(dialog)
        
        # === Method 1: From Monthly Deduction ===
        layout.addWidget(QLabel("<b>Calculate Principal from Monthly Deduction</b>"))
        form1 = QFormLayout()
        
        deduction_input = QLineEdit()
        deduction_input.setPlaceholderText("e.g., 2300")
        form1.addRow("Monthly Deduction:", deduction_input)
        
        months_input = QLineEdit()
        months_input.setPlaceholderText("e.g., 11")
        form1.addRow("Number of Months:", months_input)
        
        interest_input1 = QLineEdit()
        interest_input1.setText("15")
        form1.addRow("Interest Rate (%):", interest_input1)
        
        result_label1 = QLabel("<b>Principal: ---</b>")
        result_label1.setStyleSheet("color: #2b5797; font-size: 14px;")
        form1.addRow(result_label1)
        
        # State to store calc
        self.calculated_principal = None
        self.calculated_months = None
        
        def calculate1():
            import math
            try:
                deduction = float(deduction_input.text())
                months = int(months_input.text())
                rate = float(interest_input1.text()) / 100.0
                total = deduction * months
                # Formula: Principal = Total / (1 + rate)
                principal = int((total / (1 + rate)) + 0.5)
                
                # Forward Verify loop to ensure math.ceil doesn't bump us up by 1 due to rounding
                # We want the highest principal that keeps us at or below the target deduction.
                for _ in range(5):
                    interest = principal * rate
                    repayment = principal + interest
                    calc_deduction = math.ceil(repayment / months)
                    
                    if calc_deduction > deduction:
                        principal -= 1
                    else:
                        break # We are safe (<= target)
                
                result_label1.setText(f"<b>Principal: {principal:,.0f}</b>")
                
                self.calculated_principal = principal
                self.calculated_months = months
                use_btn.setEnabled(True)
                
            except (ValueError, ZeroDivisionError):
                result_label1.setText("<b>Principal: Invalid input</b>")
                use_btn.setEnabled(False)
        
        
        def on_use_result():
            if self.calculated_principal is None:
                # User clicked Use Result without calculating first
                calculate1()
            
            # If calculation successful (or already done), proceed
            if self.calculated_principal is not None:
                dialog.accept()
        
        calc_btn1 = QPushButton("Calculate")
        calc_btn1.clicked.connect(calculate1)
        form1.addRow(calc_btn1)
        
        layout.addLayout(form1)
        
        # Buttons
        layout.addSpacing(10)
        btn_box = QHBoxLayout()
        use_btn = QPushButton("Use Result")
        use_btn.setStyleSheet("background-color: #28a745; color: white;")
        # use_btn.setEnabled(False) # Removed: Allow clicking to auto-calc
        
        cancel_btn = QPushButton("Cancel")
        
        btn_box.addWidget(use_btn)
        btn_box.addWidget(cancel_btn)
        layout.addLayout(btn_box)
        
        use_btn.clicked.connect(on_use_result)
        cancel_btn.clicked.connect(dialog.reject)
        
        if dialog.exec() == QDialog.DialogCode.Accepted and self.calculated_principal:
             self.amount_input.setText(f"{self.calculated_principal}")
             self.duration_input.setText(f"{self.calculated_months}")
             self.interest_input.setText(interest_input1.text())


    def show_loan_info(self, loan_ref):
        """Show summary of loan info."""
        loan = self.engine.db.get_loan_by_ref(self.current_individual_id, loan_ref)
        if not loan: return
        
        # Calculate derived stats if needed
        # Principal is stored in 'principal' column usually
        principal = loan.get('principal', 0)
        # Using HTML for bolding
        # Calculate Estimated Remaining Duration
        total_debt_base = loan['balance'] + loan.get('interest_balance', 0) + loan.get('unearned_interest', 0)
        inst = loan.get('installment', 1)
        est_duration = 0
        if inst > 0:
            import math
            est_duration = math.ceil(total_debt_base / inst)

        msg = (f"<h3>Loan {loan_ref}</h3>"
               f"<p><b>Initial Principal:</b> {principal:,.0f}</p>"
               f"<p><b>Current Balance:</b> {loan['balance']:,.0f}</p>"
               f"<p><b>Interest Balance:</b> {loan.get('interest_balance', 0):,.0f}</p>"
               f"<p><b>Monthly Installment:</b> {loan['installment']:,.0f}</p>"
               f"<p><b>Monthly Interest:</b> {loan.get('monthly_interest', 0):,.0f}</p>"
               f"<p><b>Unearned Interest:</b> {loan.get('unearned_interest', 0):,.0f}</p>"
               f"<p><b>Est. Remaining Duration:</b> {est_duration} months</p>"
               f"<p><b>Next Due Date:</b> {loan['next_due_date']}</p>"
               f"<p><b>Status:</b> {loan['status']}</p>")
               
        box = QMessageBox(self)
        box.setWindowTitle(f"Loan Info - {loan_ref}")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(msg)
        box.exec()


