"""Dashboard view for LoanMaster."""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QListWidget, QListWidgetItem,
                             QTableWidgetItem, QLineEdit, QMessageBox,
                             QFileDialog, QDialog, QFormLayout, QCheckBox,
                             QScrollArea, QDialogButtonBox, QMenu, QFrame, QGraphicsDropShadowEffect, QProgressDialog, QApplication)
from PyQt6.QtGui import QAction, QPixmap, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from datetime import datetime
import os
import sys


from src.dialogs import IndividualDialog, ImportDialog, StatementConfigDialog, DuplicateResolutionDialog, ImportHistoryDialog, ImportPreviewDialog, ExcelImportDialog
from src.reports import ReportGenerator
from ..theme import ThemeManager
from ..database import DatabaseManager


class IndividualCard(QFrame):
    """A custom widget to display individual details in the list."""
    
    def __init__(self, ind_id, name, phone, email, parent_dashboard, is_retired=False):
        super().__init__()
        self.ind_id = ind_id
        self.name = name
        self.phone = phone
        self.email = email
        self.is_retired = is_retired
        self.dashboard = parent_dashboard
        self._is_selected = False
        
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(68)
        
        self.init_ui()
        self.apply_theme() # Initial theme application
        
    def init_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        
        # Avatar (Circle with Initials)
        self.avatar = QLabel()
        self.avatar.setFixedSize(40, 40)
        initials = "".join([n[0] for n in self.name.split()[:2]]).upper()
        self.avatar.setText(initials)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.avatar_color = self._get_avatar_color()
        self.avatar.setStyleSheet(f"""
            background-color: {self.avatar_color}; 
            color: white; 
            font-weight: bold; 
            font-size: 15px; 
            border-radius: 20px;
        """)
        layout.addWidget(self.avatar)
        
        # Info Section
        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        info_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        
        self.name_label = QLabel(self.name)
        # Font weight handled in apply_theme, basic setup here
        
        details_text = self.phone if self.phone else "No Phone"
        if self.email:
            details_text += f" | {self.email}"
            
        self.details_label = QLabel(details_text)
        
        info_layout.addWidget(self.name_label)
        info_layout.addWidget(self.details_label)
        layout.addLayout(info_layout)
        
        layout.addStretch()
        
        # Retirement badge
        self.retired_badge = QLabel("RETIRED")
        self.retired_badge.setStyleSheet(
            "background-color: #DC2626; color: white; font-size: 10px; "
            "font-weight: bold; padding: 2px 8px; border-radius: 4px;"
        )
        self.retired_badge.setVisible(self.is_retired)
        layout.addWidget(self.retired_badge)

    def apply_theme(self):
        t = self.dashboard.theme_manager
        
        self.name_label.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {t.get_color('text_primary')};")
        self.details_label.setStyleSheet(f"font-size: 12px; color: {t.get_color('text_secondary')};")
        
        # Re-apply selection state with new colors
        self.update_style()

    def _get_avatar_color(self):
        # Muted Professional Colors
        colors = ["#4B5563", "#EF4444", "#F59E0B", "#10B981", "#3B82F6", "#6366F1", "#8B5CF6", "#EC4899"]
        return colors[hash(self.name) % len(colors)]

    def mousePressEvent(self, event):
        self.dashboard.select_individual(self)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.dashboard.open_ledger_by_id(self.ind_id, self.name)
        super().mouseDoubleClickEvent(event)

    def set_selected(self, selected):
        self._is_selected = selected
        self.update_style()

    def update_style(self):
        t = self.dashboard.theme_manager
        if self._is_selected:
            bg = t.get_color("card_selected_bg")
            border = t.get_color("card_selected_border")
            self.setStyleSheet(f"""
                IndividualCard {{
                    background-color: {bg};
                    border: 2px solid {border};
                    border-radius: 8px;
                }}
            """)
        else:
            bg = t.get_color("card_bg")
            border = t.get_color("card_border")
            h_border = t.get_color("card_hover_border")
            self.setStyleSheet(f"""
                IndividualCard {{
                    background-color: {bg};
                    border: 1px solid {border};
                    border-radius: 8px;
                }}
                IndividualCard:hover {{
                    border-color: {h_border};
                }}
            """)



class ReportWorker(QThread):
    """Background worker for report generation."""
    progress = pyqtSignal(int, int, str) # current, total, message
    finished = pyqtSignal(bool, str)     # success, message
    
    finished = pyqtSignal(bool, str)     # success, message
    
    def __init__(self, db_name, printer_view_getter, start_date, output_path, report_type='loan'):
        super().__init__()
        self.db_name = db_name
        self.printer_view_getter = printer_view_getter
        self.start_date = start_date
        self.output_path = output_path
        self.report_type = report_type
        
    def run(self):
        db_manager = None
        try:
            # Create thread-local DB connection
            db_manager = DatabaseManager(self.db_name)
            
            # Create generator with thread-local DB
            # Note: printer_view_getter usage in thread might be risky for PDF. 
            # If PDF export is requested, we might need to rethink this, 
            # but for Excel/CSV this fixes the SQLite error.
            generator = ReportGenerator(db_manager, printer_view_getter=self.printer_view_getter)
            
            def callback(current, total, msg):
                self.progress.emit(current, total, msg)
                
            if self.report_type == 'savings':
                success, msg = generator.generate_quarterly_savings_report(
                    self.start_date, 
                    self.output_path, 
                    progress_callback=callback
                )
            else:
                success, msg = generator.generate_quarterly_report(
                    self.start_date, 
                    self.output_path, 
                    progress_callback=callback
                )
            self.finished.emit(success, msg)
            
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            if db_manager and hasattr(db_manager, 'conn'):
                try:
                    db_manager.conn.close()
                except:
                    pass


class Dashboard(QWidget):
    """Main dashboard showing list of individuals.
    
    Uses UIStateManager for selection state and StatementGenerator for
    PDF/Excel generation.
    """
    
    def __init__(self, main_window, db_manager):
        super().__init__()
        self.main_window = main_window
        self.db = db_manager
        self.theme_manager = ThemeManager(self.db)
        self.printer_view = None
        
        # Use new architecture components
        from ..ui_state_manager import UIStateManager
        from ..loan_action_controller import LoanActionController
        from ..statement_generator import StatementGenerator
        from ..engine import LoanEngine
        
        self.engine = LoanEngine(self.db)
        
        self._ui_state = UIStateManager(on_selection_changed=self._on_selection_changed)
        self._statement_generator = StatementGenerator(
            db_manager, 
            printer_view_getter=self.get_printer_view
        )
        
        self.loan_controller = LoanActionController(
            self.db, 
            self._ui_state, 
            self, 
            on_refresh=self.refresh_list, 
            on_open_ledger=self.open_ledger_by_id
        )
        
        # Legacy compatibility (delegate to UIStateManager)
        self.selected_card = None
        self.card_widgets = []
        self.init_ui()

    def init_ui(self):
        # Apply theme initially (creates layout structure if not exists)
        # However, we need to create widgets first, then apply style.
        # Let's separate widget creation from styling.
        
        self.create_widgets()
        self.apply_theme()

    def create_widgets(self):
        # Main layout with no margins at the top level
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        
        # --- HEADER SECTION ---
        self.header_frame = QFrame()
        self.header_frame.setObjectName("headerFrame")
        self.header_frame.setFixedHeight(80)
        
        header_layout = QHBoxLayout(self.header_frame)
        header_layout.setContentsMargins(25, 10, 25, 10)
        
        # Logo and Title
        logo_title_layout = QHBoxLayout()
        logo_title_layout.setSpacing(15)
        
        logo_label = QLabel()
        
        # Robust path finding for resources
        if getattr(sys, 'frozen', False):
            # Running as compiled executable
            base_path = os.path.dirname(sys.executable)
        else:
            # Running as script
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            
        icon_path = os.path.join(base_path, "resources", "icon.png")
        
        if os.path.exists(icon_path):
            pixmap = QPixmap(icon_path)
            logo_label.setPixmap(pixmap.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        
        self.title_label = QLabel("LoanMaster")
        
        logo_title_layout.addWidget(logo_label)
        logo_title_layout.addWidget(self.title_label)
        header_layout.addLayout(logo_title_layout)
        
        header_layout.addStretch()
        
        # Quick Actions
        quick_actions_layout = QHBoxLayout()
        quick_actions_layout.setSpacing(12)
        
        self.add_btn = QPushButton("+ New Individual")
        self.add_btn.setFixedSize(150, 38)
        self.add_btn.clicked.connect(self.add_individual)
        
        self.import_btn = QPushButton("Import Data")
        self.import_btn.setFixedSize(130, 38)
        import_menu = QMenu(self)
        act_db = QAction("From Database (.db)…", self)
        act_db.triggered.connect(self.import_individuals)
        act_xlsx = QAction("From Excel (.xlsx)…", self)
        act_xlsx.triggered.connect(self.import_from_excel)
        import_menu.addAction(act_db)
        import_menu.addAction(act_xlsx)
        self.import_btn.setMenu(import_menu)

        quick_actions_layout.addWidget(self.add_btn)
        quick_actions_layout.addWidget(self.import_btn)
        header_layout.addLayout(quick_actions_layout)
        
        self.main_layout.addWidget(self.header_frame)
        
        # --- CONTENT SECTION ---
        self.content_container = QWidget()
        content_layout = QVBoxLayout(self.content_container)
        content_layout.setContentsMargins(40, 30, 40, 30)
        content_layout.setSpacing(20)
        
        # Search Section
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name or phone...")
        self.search_input.setMinimumHeight(48)
        self.search_input.textChanged.connect(self.filter_list)
        
        # Shadow for search
        shadow_effect = QGraphicsDropShadowEffect()
        shadow_effect.setBlurRadius(20)
        shadow_effect.setOffset(0, 4)
        shadow_effect.setColor(QColor(0, 0, 0, 15))
        self.search_input.setGraphicsEffect(shadow_effect)
        self.search_shadow = shadow_effect # Store to update color later
        
        search_layout.addWidget(self.search_input)
        content_layout.addLayout(search_layout)
        
        # Results Title
        results_header_layout = QHBoxLayout()
        self.results_label = QLabel("Active Individuals")
        results_header_layout.addWidget(self.results_label)
        results_header_layout.addStretch()
        content_layout.addLayout(results_header_layout)
        
        # Scroll Area for List
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        
        self.scroll_content = QWidget()
        self.scroll_content_layout = QVBoxLayout(self.scroll_content)
        self.scroll_content_layout.setSpacing(12)
        self.scroll_content_layout.setContentsMargins(0, 5, 5, 5) # Right margin for scrollbar
        self.scroll_content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        
        self.scroll_area.setWidget(self.scroll_content)
        content_layout.addWidget(self.scroll_area)
        
        # Bottom Actions Section
        self.actions_container = QFrame()
        self.actions_container.setFixedHeight(72)
        
        # Shadow for actions bar
        action_shadow = QGraphicsDropShadowEffect()
        action_shadow.setBlurRadius(30)
        action_shadow.setOffset(0, 10)
        action_shadow.setColor(QColor(0, 0, 0, 20))
        self.actions_container.setGraphicsEffect(action_shadow)
        self.actions_shadow = action_shadow
        
        actions_layout = QHBoxLayout(self.actions_container)
        actions_layout.setContentsMargins(20, 10, 20, 10)
        actions_layout.setSpacing(15)
        
        self.open_btn = QPushButton("Open Ledger")
        self.open_btn.clicked.connect(self.open_ledger_btn)
        self.open_btn.setEnabled(False)
        
        self.edit_btn = QPushButton("Edit Details")
        self.edit_btn.clicked.connect(self.edit_individual)
        self.edit_btn.setEnabled(False)
        
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.delete_individual)
        self.delete_btn.setEnabled(False)
        
        self.retire_btn = QPushButton("Retire")
        self.retire_btn.clicked.connect(self.retire_individual_btn)
        self.retire_btn.setEnabled(False)
        self.retire_btn.setStyleSheet("background-color: #DC2626; color: white; padding: 8px 15px; border-radius: 5px;")
        
        actions_layout.addWidget(self.open_btn)
        actions_layout.addWidget(self.edit_btn)
        actions_layout.addWidget(self.retire_btn)
        actions_layout.addStretch()
        actions_layout.addWidget(self.delete_btn)
        
        content_layout.addWidget(self.actions_container)
        
        # --- FOOTER / UTILITY SECTION ---
        utility_layout = QHBoxLayout()
        utility_layout.setSpacing(15)
        
        self.print_selected_btn = QPushButton("Print Statements")
        self.print_selected_btn.clicked.connect(self.batch_print_selected)
        self.print_selected_btn.setStyleSheet("background-color: #3b82f6; color: white; padding: 8px 15px; border-radius: 5px;")
        
        self.reports_btn = QPushButton("Quarterly Reports")
        self.reports_btn.setStyleSheet("background-color: #10B981; color: white; padding: 8px 15px; border-radius: 5px;")
        reports_menu = QMenu(self)
        
        loan_report_action = QAction("Loan Report", self)
        loan_report_action.triggered.connect(self.generate_quarterly_report)
        reports_menu.addAction(loan_report_action)

        savings_report_action = QAction("Savings / Shares Report", self)
        savings_report_action.triggered.connect(
            lambda: self._open_fund_report_dialog("savings", "Savings / Shares"))
        reports_menu.addAction(savings_report_action)

        christmas_report_action = QAction("Christmas Report", self)
        christmas_report_action.triggered.connect(
            lambda: self._open_fund_report_dialog("christmas", "Christmas"))
        reports_menu.addAction(christmas_report_action)

        benevolent_report_action = QAction("Benevolent Report", self)
        benevolent_report_action.triggered.connect(
            lambda: self._open_fund_report_dialog("benevolent", "Benevolent"))
        reports_menu.addAction(benevolent_report_action)

        self.reports_btn.setMenu(reports_menu)
        
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.clicked.connect(self.open_settings)
        self.settings_btn.setStyleSheet("background-color: #6B7280; color: white; padding: 8px 15px; border-radius: 5px;")
        
        self.mass_ops_btn = QPushButton("Mass Operations")
        self.mass_ops_btn.setStyleSheet("background-color: #8B5CF6; color: white; padding: 8px 15px; border-radius: 5px;")
        
        mass_menu = QMenu(self)
        deduct_action = QAction("Mass Deduct (Loans)", self)
        deduct_action.triggered.connect(self.open_mass_deduction_dialog)
        mass_menu.addAction(deduct_action)
        
        savings_action = QAction("Mass Savings (Increments)", self)
        savings_action.triggered.connect(self.open_mass_savings_dialog)
        mass_menu.addAction(savings_action)

        christmas_action = QAction("Mass Christmas (Contributions)", self)
        christmas_action.triggered.connect(self.open_mass_christmas_dialog)
        mass_menu.addAction(christmas_action)

        benevolent_action = QAction("Mass Benevolent (Deductions)", self)
        benevolent_action.triggered.connect(self.open_mass_benevolent_dialog)
        mass_menu.addAction(benevolent_action)

        mass_menu.addSeparator()

        repair_action = QAction("Repair Loans (legacy terms)…", self)
        repair_action.triggered.connect(self.open_repair_loans_dialog)
        mass_menu.addAction(repair_action)

        activity_action = QAction("Activity Log…", self)
        activity_action.triggered.connect(self.show_activity_log)
        mass_menu.addAction(activity_action)

        mass_menu.addSeparator()
        
        self.undo_mass_action = QAction("Undo Last Mass Operation", self)
        self.undo_mass_action.triggered.connect(self.undo_mass_operation)
        self.undo_mass_action.setEnabled(True) # Always enabled, check in handler?
        # Or better: Context menu 'aboutToShow' capable?
        # For simplicity: Keep enabled, show message if nothing to undo.
        mass_menu.addAction(self.undo_mass_action)
        
        self.mass_ops_btn.setMenu(mass_menu)
        
        self.treasury_btn = QPushButton("Treasury & GL")
        self.treasury_btn.clicked.connect(self.open_treasury)
        self.treasury_btn.setStyleSheet("background-color: #2563EB; color: white; padding: 8px 15px; border-radius: 5px;")
        
        utility_layout.addWidget(self.print_selected_btn)
        utility_layout.addWidget(self.reports_btn)
        utility_layout.addStretch()
        utility_layout.addWidget(self.treasury_btn)
        utility_layout.addWidget(self.settings_btn)
        utility_layout.addWidget(self.mass_ops_btn)
        
        content_layout.addLayout(utility_layout)
        
        self.main_layout.addWidget(self.content_container)
        
        self.refresh_list()

    def apply_theme(self):
        """Update all styles based on current theme."""
        t = self.theme_manager
        
        # Header
        self.header_frame.setStyleSheet(f"""
            #headerFrame {{
                background: {t.get_color("bg_header")};
                border-bottom: 1px solid rgba(255,255,255,0.1);
            }}
        """)
        
        self.title_label.setStyleSheet(f"font-size: 28px; font-weight: 600; color: {t.get_color('text_header')}; font-family: 'Segoe UI', sans-serif; letter-spacing: 0.5px;")
        
        # Buttons in header
        self.add_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(255, 255, 255, 0.1);
                color: {t.get_color('text_header')};
                border: 1px solid rgba(255, 255, 255, 0.2);
                border-radius: 6px;
                font-weight: 600;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: rgba(255, 255, 255, 0.2);
                border-color: rgba(255, 255, 255, 0.3);
            }}
        """)
        
        self.import_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {t.get_color('accent')};
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: 600;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background-color: {t.get_color('accent_hover')};
            }}
        """)

        # Main Content
        self.content_container.setStyleSheet(f"background-color: {t.get_color('bg_primary')};")
        
        # Search
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                border: 1px solid {t.get_color('border')};
                border-radius: 8px;
                padding: 0 20px;
                font-size: 15px;
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                selection-background-color: {t.get_color('accent')};
            }}
            QLineEdit:focus {{
                border: 2px solid {t.get_color('accent')};
            }}
        """)
        
        # Update shadows (parse rgba string to QColor)
        # Simplified: just recreate effect
        try:
             # Just set color from string if possible or hardcode alpha for simplicity
             # Theme returns string like "rgba(0,0,0,15)"
             # QColor can parse this
             c = QColor(t.get_color("shadow"))
             self.search_shadow.setColor(c)
             self.actions_shadow.setColor(c)
        except:
             pass

        # Results Label
        self.results_label.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {t.get_color('text_secondary')}; letter-spacing: 0.3px; text-transform: uppercase;")
        
        # Scroll Area
        self.scroll_content.setStyleSheet("background-color: transparent;")
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{ border: none; background: transparent; }}
            QScrollBar:vertical {{
                border: none;
                background: {t.get_color('scrollbar_bg')};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background: {t.get_color('scrollbar_handle')};
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {t.get_color('text_secondary')};
            }}
        """)

        # Action Bar
        self.actions_container.setStyleSheet(f"""
            QFrame {{
                background-color: {t.get_color('bg_secondary')}; 
                border: 1px solid {t.get_color('border')}; 
                border-radius: 12px;
            }}
        """)

        # Action Buttons
        self.open_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {t.get_color('accent')}; color: white; font-weight: 600; padding: 10px 24px; border-radius: 6px; border: none; }}
            QPushButton:disabled {{ background-color: {t.get_color('border')}; color: {t.get_color('text_secondary')}; }}
            QPushButton:hover:!disabled {{ background-color: {t.get_color('accent_hover')}; }}
        """)
        
        self.edit_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {t.get_color('bg_secondary')}; color: {t.get_color('text_primary')}; font-weight: 600; padding: 10px 24px; border-radius: 6px; border: 1px solid {t.get_color('border')}; }}
            QPushButton:disabled {{ color: {t.get_color('border')}; border-color: {t.get_color('border')}; }}
            QPushButton:hover:!disabled {{ background-color: {t.get_color('bg_primary')}; border-color: {t.get_color('text_secondary')}; }}
        """)
        
        self.delete_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {t.get_color('danger_bg')}; color: {t.get_color('danger')}; font-weight: 600; padding: 10px 24px; border-radius: 6px; border: 1px solid {t.get_color('danger_border')}; }}
            QPushButton:disabled {{ background-color: {t.get_color('bg_secondary')}; color: {t.get_color('border')}; border-color: {t.get_color('border')}; }}
            QPushButton:hover:!disabled {{ background-color: {t.get_color('danger_border')}; border-color: {t.get_color('danger')}; }}
        """)
        
        # Utility Buttons
        # These are simple, maybe we keep them or theme them. Let's theme them slightly.
        # print_selected_btn uses 'accent' usually
        self.print_selected_btn.setStyleSheet(f"background-color: {t.get_color('accent')}; color: white; padding: 8px 15px; border-radius: 5px;")
        self.reports_btn.setStyleSheet(f"background-color: {t.get_color('success')}; color: white; padding: 8px 15px; border-radius: 5px;")
        self.settings_btn.setStyleSheet(f"background-color: {t.get_color('text_secondary')}; color: white; padding: 8px 15px; border-radius: 5px;")
        self.mass_ops_btn.setStyleSheet(f"background-color: #8B5CF6; color: white; padding: 8px 15px; border-radius: 5px;") # Purple

        # Update all cards
        for card in self.card_widgets:
            card.apply_theme()


    def refresh_list(self, select_id=None):
        # Clear existing items
        self.card_widgets = []
        self.selected_card = None
        self.update_action_buttons()
        
        while self.scroll_content_layout.count():
            item = self.scroll_content_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
        individuals = self.db.get_individuals()
        individuals.sort(key=lambda x: x[1].lower())
        
        for idx, ind in enumerate(individuals, 1):
             # ind: [id, name, phone, email, default_deduction, created_at, import_id, is_retired, retired_date]
             ind_details = self.db.get_individual(ind[0])
             is_retired = bool(ind_details.get('is_retired', 0)) if ind_details else False
             card = IndividualCard(ind[0], ind[1], ind[2], ind[3], self, is_retired=is_retired)
             self.scroll_content_layout.addWidget(card)
             self.card_widgets.append(card)
        
        # Sync to UIStateManager
        self._ui_state.set_cards(self.card_widgets)
        
        # Auto-select if requested
        if select_id is not None:
            for card in self.card_widgets:
                if card.ind_id == select_id:
                    self.select_individual(card)
                    break

    def filter_list(self, text):
        """Filter cards by text using UIStateManager."""
        self._ui_state.apply_filter(text)
        
    def _on_selection_changed(self, card):
        """Callback when selection changes via UIStateManager."""
        self.selected_card = card  # Sync legacy attribute
        self.update_action_buttons()

    def select_individual(self, card):
        """Select a card using UIStateManager."""
        self._ui_state.select(card)
        # Legacy sync is handled via _on_selection_changed callback

    def update_action_buttons(self):
        enabled = (self.selected_card is not None)
        self.open_btn.setEnabled(enabled)
        self.edit_btn.setEnabled(enabled)
        self.delete_btn.setEnabled(enabled)
        self.retire_btn.setEnabled(enabled)
        
        # Update retire button text/colour; grey out when nothing is selected
        # (a :disabled rule, since the solid red/green otherwise looks enabled).
        disabled_css = ("QPushButton:disabled { background-color: "
                        f"{self.theme_manager.get_color('border')}; "
                        f"color: {self.theme_manager.get_color('text_secondary')}; }}")
        if self.selected_card and self.selected_card.is_retired:
            self.retire_btn.setText("Reinstate")
            base = "background-color: #10B981; color: white; padding: 8px 15px; border-radius: 5px;"
        else:
            self.retire_btn.setText("Retire")
            base = "background-color: #DC2626; color: white; padding: 8px 15px; border-radius: 5px;"
        self.retire_btn.setStyleSheet(f"QPushButton {{ {base} }} {disabled_css}")

    def add_individual(self):
        self.loan_controller.add_individual(IndividualDialog)

    def get_selected_id(self):
        return self._ui_state.get_selected_id()

    def open_ledger_btn(self):
        self.loan_controller.open_ledger()

    def open_ledger_by_id(self, ind_id, name):
         self.main_window.show_ledger(ind_id, name)
         
    # Compatibility shim if called from outside? (Assuming no external calls to open_ledger with item)


    def edit_individual(self):
        self.loan_controller.edit_individual(IndividualDialog)

    def delete_individual(self):
        self.loan_controller.delete_individual()
    
    def retire_individual_btn(self):
        """Retire or reinstate the selected individual."""
        if not self.selected_card:
            return
        
        ind_id = self.selected_card.ind_id
        name = self.selected_card.name
        is_retired = self.selected_card.is_retired
        
        if is_retired:
            # Reinstate
            confirm = QMessageBox.question(
                self, "Reinstate Individual",
                f"Are you sure you want to reinstate '{name}'?\n\n"
                f"They will appear in all reports again.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                self.engine.reinstate_individual(ind_id)
                self.refresh_list(select_id=ind_id)
                QMessageBox.information(self, "Reinstated", f"'{name}' has been reinstated.")
        else:
            # Retire
            savings_bal = self.db.get_savings_balance(ind_id)
            has_loans = self.db.has_outstanding_loans(ind_id)
            
            msg = f"Are you sure you want to retire '{name}'?\n\n"
            if savings_bal > 0:
                msg += f"\u2022 Savings balance of {savings_bal:,.0f} will be auto-withdrawn\n"
            if has_loans:
                msg += f"\u2022 Outstanding loans will remain active until cleared\n"
                msg += f"\u2022 They will still appear in Loan Reports\n"
            msg += f"\n\u2022 They will be excluded from Savings/Shares Reports\n"
            msg += f"\u2022 They will be excluded from Mass Savings operations"
            
            confirm = QMessageBox.question(
                self, "Retire Individual", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                result = self.engine.retire_individual(ind_id)
                withdrawn = result.get('savings_withdrawn', 0)
                
                msg = f"'{name}' has been retired."
                if withdrawn > 0:
                    msg += f"\nSavings withdrawal of {withdrawn:,.0f} was created."
                
                self.refresh_list(select_id=ind_id)
                QMessageBox.information(self, "Retired", msg)
    
    def batch_print_all(self):
        """Print all statements to a selected folder."""
        # Get date range first
        date_range = self.loan_controller.get_date_range_dialog("Batch Print - Select Date Range")
        if not date_range:
            return
        
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Save Statements")
        if not folder:
            return
        
        individuals = self.db.get_individuals()
        if not individuals:
            QMessageBox.information(self, "Info", "No individuals to print.")
            return
        
        count = 0
        total_steps = len(individuals)
        
        progress = QProgressDialog("Generating Statements...", "Cancel", 0, total_steps, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0) # Show immediately
        progress.show()
        
        for i, ind in enumerate(individuals):
            if progress.wasCanceled():
                break
                
            progress.setLabelText(f"Generating Statement for {ind[1]}...")
            success, path, fmt = self._statement_generator.generate_pdf_statement(ind[0], ind[1], folder, date_range[0], date_range[1])
            
            if success:
                count += 1
            progress.setValue(i + 1)
            QApplication.processEvents() # Ensure UI remains responsive
        
        progress.close()
        QMessageBox.information(self, "Success", f"Saved {count} statement(s) to:\n{folder}")
    
    def batch_print_selected(self):
        """Open dialog to select which statements to print."""
        individuals = self.db.get_individuals()
        if not individuals:
            QMessageBox.information(self, "Info", "No individuals to print.")
            return
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Select Statements to Print")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)
        
        layout.addWidget(QLabel("<b>Select individuals to print:</b>"))
        
        # Filter for print selection
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("Filter list...")
        layout.addWidget(filter_input)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        checkboxes = []
        for ind in individuals:
            cb = QCheckBox(f"{ind[1]} | {ind[2]}")
            cb.setChecked(False)
            cb.setProperty("ind_id", ind[0])
            cb.setProperty("name", ind[1])
            checkboxes.append(cb)
            scroll_layout.addWidget(cb)
            
        def filter_checkboxes(text):
            text = text.lower()
            for cb in checkboxes:
                cb.setHidden(text not in cb.text().lower())
                
        filter_input.textChanged.connect(filter_checkboxes)
        
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        # Select Only Visible checkboxes if filter is active? 
        # Or Just all? "Select All" usually implies All.
        # But if filtered, user might want "Select Visible".
        # Let's simple: Select All checkboxes regardless of visibility to match "Print All" behavior easily.
        select_all_btn.clicked.connect(lambda: [cb.setChecked(True) for cb in checkboxes])
        
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(lambda: [cb.setChecked(False) for cb in checkboxes])
        btn_layout.addWidget(select_all_btn)
        btn_layout.addWidget(clear_btn)
        layout.addLayout(btn_layout)
        
        # Date Range removed from here, moving to config dialog
        # layout.addWidget(QLabel("<b>Statement Period:</b>"))
        pass
        
        # Helper to handle execution
        self.print_mode = "pdf" # default
        self.print_config_data = None # Store config here
        
        def set_mode_and_configure(mode):
            self.print_mode = mode
            
            # Compute earliest date across selected individuals
            selected_ids = [cb.property("ind_id") for cb in checkboxes if cb.isChecked()]
            earliest_date = None
            if selected_ids:
                earliest_date = self.db.get_earliest_record_date_for_ids(selected_ids)
            
            # Open Config Dialog with smart default start date
            conf_dialog = StatementConfigDialog(dialog, default_start_date=earliest_date)
            if conf_dialog.exec() == QDialog.DialogCode.Accepted:
                # Capture data
                f_date, t_date, config = conf_dialog.get_config()
                self.print_config_data = {
                    "from": f_date.toString("yyyy-MM-dd"),
                    "to": t_date.toString("yyyy-MM-dd"),
                    "config": config
                }
                dialog.accept()
            
        print_btn = QPushButton("Configure & Print")
        # Create Menu
        menu = QMenu(print_btn)
        
        pdf_action = QAction("Print to PDF", dialog)
        pdf_action.triggered.connect(lambda: set_mode_and_configure("pdf"))
        menu.addAction(pdf_action)
        
        excel_action = QAction("Print to Excel", dialog)
        excel_action.triggered.connect(lambda: set_mode_and_configure("excel"))
        menu.addAction(excel_action)
        
        print_btn.setMenu(menu)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        action_layout = QHBoxLayout()
        action_layout.addWidget(print_btn)
        action_layout.addWidget(cancel_btn)
        layout.addLayout(action_layout)
        
        if dialog.exec() == QDialog.DialogCode.Accepted and self.print_config_data:
            folder = QFileDialog.getExistingDirectory(self, "Select Folder to Save Statements")
            if not folder:
                return
            
            from_str = self.print_config_data["from"]
            to_str = self.print_config_data["to"]
            config = self.print_config_data["config"]
            
            count = 0
            
            # Filter checks that are checked
            selected_checks = [cb for cb in checkboxes if cb.isChecked()]
            total_steps = len(selected_checks)
            
            if total_steps > 0:
                progress = QProgressDialog("Generating Statements...", "Cancel", 0, total_steps, self)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.setMinimumDuration(0)
                progress.show()
                
                for i, cb in enumerate(selected_checks):
                    if progress.wasCanceled():
                        break
                        
                    ind_id = cb.property("ind_id")
                    name = cb.property("name")
                    
                    progress.setLabelText(f"Generating Statement for {name}...")
                    
                    success = False
                    if self.print_mode == "pdf":
                        # Updated signature returns (success, path, format)
                        res = self._statement_generator.generate_pdf_statement(ind_id, name, folder, from_str, to_str, config=config)
                        if isinstance(res, tuple):
                             success = res[0]
                        else:
                             success = res
                    elif self.print_mode == "excel":
                        success = self._statement_generator.generate_excel_statement(ind_id, name, folder, from_str, to_str, config=config)
                        
                    if success:
                        count += 1
                    
                    progress.setValue(i + 1)
                    QApplication.processEvents()
                
                progress.close()
            
            if count > 0:
                QMessageBox.information(self, "Success", f"Saved {count} check(s) to:\n{folder}")
            else:
                QMessageBox.warning(self, "Warning", "No statements saved.")

    
    def get_printer_view(self):
        """Get or create the hidden QWebEngineView for printing."""
        if self.printer_view is None:
            try:
                from PyQt6.QtWebEngineWidgets import QWebEngineView
                self.printer_view = QWebEngineView()
                self.printer_view.hide()
            except ImportError:
                print("QtWebEngineWidgets not found. PDF printing not available.")
                return None
        return self.printer_view

    def open_settings(self):
        """Open settings dialog."""
        from PyQt6.QtWidgets import QComboBox, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        layout = QFormLayout(dialog)
        
        # FY Start
        fy_combo = QComboBox()
        months = ["January", "February", "March", "April", "May", "June", 
                  "July", "August", "September", "October", "November", "December"]
        fy_combo.addItems(months)
        
        # Load current
        current_fy_str = self.db.get_setting("fy_start_month", "November")
        if current_fy_str in months:
            fy_combo.setCurrentText(current_fy_str)
        
        layout.addRow("Financial Year Start:", fy_combo)
        
        # Theme Selection
        theme_combo = QComboBox()
        theme_combo.addItems(["Light", "Dark"])
        current_theme = self.theme_manager.current_theme_name
        theme_combo.setCurrentText(current_theme)
        layout.addRow("Application Theme:", theme_combo)
        
        # Deduction Same Month
        deduct_cb = QCheckBox("Allow Deduction in Same Month of Issue")
        deduct_val = self.db.get_setting("deduct_same_month", "false")
        deduct_cb.setChecked(deduct_val.lower() == "true")
        layout.addRow(deduct_cb)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout.addRow(btns)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_fy = fy_combo.currentText()
            self.db.set_setting("fy_start_month", new_fy)
            
            new_deduct = "true" if deduct_cb.isChecked() else "false"
            self.db.set_setting("deduct_same_month", new_deduct)
            
            # Save Theme
            selected_theme = theme_combo.currentText()
            if selected_theme != self.theme_manager.current_theme_name:
                self.theme_manager.set_theme(selected_theme)
                self.apply_theme()
                QMessageBox.information(self, "Settings Saved", f"Settings updated. Theme changed to {selected_theme}.")
            else:
                QMessageBox.information(self, "Settings Saved", "Settings have been updated successfully.")
            


    def open_mass_christmas_dialog(self):
        self._open_mass_fund_dialog("Mass Christmas Contribution (Catch Up)",
                                    self.engine.mass_catch_up_christmas, "contributions")

    def open_mass_benevolent_dialog(self):
        self._open_mass_fund_dialog("Mass Benevolent Deduction (Catch Up)",
                                    self.engine.mass_catch_up_benevolent, "deductions")

    def _open_mass_fund_dialog(self, title, engine_method, noun):
        """Catch up a fund across all members up to a chosen date, with progress.

        Members the fund doesn't apply to (no initial deposit / not enrolled) are
        skipped by the service, so passing every member id is safe.
        """
        from PyQt6.QtWidgets import QDateEdit
        from PyQt6.QtCore import QDate
        ids = [m[0] for m in self.db.get_individuals()]
        if not ids:
            QMessageBox.information(self, "No Members", "There are no members to process.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Catch up {noun} for all {len(ids)} members, up to:"))
        form = QFormLayout()
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDate(QDate.currentDate())
        form.addRow("Until (inclusive):", date_edit)
        layout.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        tq = date_edit.date()
        target = datetime(tq.year(), tq.month(), tq.day())
        progress = QProgressDialog(f"Processing {noun}...", "Cancel", 0, len(ids), self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()

        def cb(i, _item):
            if progress.wasCanceled():
                raise Exception("Operation Canceled by User")
            progress.setValue(i + 1)
            QApplication.processEvents()

        try:
            processed, total, errors = engine_method(ids, cb, target_date=target)
        except Exception as e:
            progress.close()
            if "Canceled" in str(e):
                QMessageBox.information(self, "Canceled",
                                       "Operation canceled. No changes were made (rolled back).")
            else:
                QMessageBox.critical(self, "Error", f"An error occurred: {e}")
            return

        progress.close()
        if total == 0:
            QMessageBox.information(self, "Up to Date", "All members are already up to date.")
        else:
            extra = f"\n{len(errors)} member(s) had errors." if errors else ""
            QMessageBox.information(self, "Success",
                                   f"Members updated: {processed}\nTotal {noun} created: {total}"
                                   f"\n\n(Use Mass Operations → Undo Last to revert.){extra}")

    def open_mass_deduction_dialog(self):
        """Open dialog for Mass Loan Deduction."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Mass Loan Deduction (Catch Up)")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)
        layout = QVBoxLayout(dialog)
        
        layout.addWidget(QLabel("<b>Select loans to catch up:</b>"))
        layout.addWidget(QLabel("Checked loans will be processed to catch up entirely to current date."))
        
        # Get all active loans
        # We need to fetch all active loans from DB.
        # We can use db.get_all_active_loans()
        active_loans = self.db.get_all_active_loans()
        
        if not active_loans:
            layout.addWidget(QLabel("No active loans found."))
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            btns.rejected.connect(dialog.reject)
            layout.addWidget(btns)
            dialog.exec()
            return

        # Filter Input
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("Filter by name or loan ref...")
        layout.addWidget(filter_input)
            
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        checkboxes = []
        for loan in active_loans:
            # We need individual name
            ind_name = self.db.get_individual_name(loan['individual_id'])
            
            # Label: Name | Loan Ref | Next Due | Status
            label_text = f"{ind_name} | {loan['ref']} | Due: {loan['next_due_date']}"
            
            cb = QCheckBox(label_text)
            cb.setChecked(True) # Default all checked
            cb.setProperty("loan_ref", loan['ref'])
            cb.setProperty("ind_id", loan['individual_id'])
            
            # Check if suspended
            is_suspended = loan.get('is_suspended', 0)
            suspend_until = loan.get('suspend_until', '')
            
            # Check if individual is retired
            ind_details = self.db.get_individual(loan['individual_id'])
            is_retired = bool(ind_details.get('is_retired', 0)) if ind_details else False
            
            if is_suspended:
                suffix = f" (\u23f8 SUSPENDED until {suspend_until})" if suspend_until else " (\u23f8 SUSPENDED)"
                cb.setText(label_text + suffix)
                cb.setEnabled(False)
                cb.setChecked(False)
            elif is_retired:
                cb.setText(label_text + " (Retired)")
            else:
                # Check if actually overdue?
                is_overdue = loan['next_due_date'] <= datetime.now().strftime("%Y-%m-%d")
                if not is_overdue:
                    cb.setText(label_text + " (Up to Date)")
                    cb.setEnabled(False)
                    cb.setChecked(False)
            
            checkboxes.append(cb)
            scroll_layout.addWidget(cb)
            
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)
        
        # Filter Logic
        def filter_items(text):
            text = text.lower()
            for cb in checkboxes:
                if text in cb.text().lower():
                    cb.setVisible(True)
                else:
                    cb.setVisible(False)
        
        filter_input.textChanged.connect(filter_items)
        
        # Select/Deselect All (Respects Filter)
        btn_layout = QHBoxLayout()
        sel_all = QPushButton("Select All Visible")
        
        def select_visible():
            for cb in checkboxes:
                if cb.isVisible() and cb.isEnabled():
                    cb.setChecked(True)
                    
        sel_all.clicked.connect(select_visible)
        
        clr_all = QPushButton("Clear All")
        clr_all.clicked.connect(lambda: [cb.setChecked(False) for cb in checkboxes])
        btn_layout.addWidget(sel_all)
        btn_layout.addWidget(clr_all)
        layout.addLayout(btn_layout)
        
        # Run Button
        run_btn = QPushButton("Run Mass Deduction")
        run_btn.setStyleSheet("background-color: #dc3545; color: white; font-weight: bold; padding: 10px;")
        layout.addWidget(run_btn)
        
        engine = self.engine
        
        def run_process():
            selected = [cb for cb in checkboxes if cb.isChecked()]
            if not selected:
                QMessageBox.warning(dialog, "Warning", "No loans selected.")
                return
            
            # Ask for Target Date instead of simple Confirm
            from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDateEdit, QDialogButtonBox, QFormLayout
            from PyQt6.QtCore import QDate
            from datetime import datetime
            
            # Default to Current Date
            default_target = datetime.now()
            
            d = QDialog(dialog)
            d.setWindowTitle("Confirm Mass Deduction")
            
            layout = QVBoxLayout(d)
            layout.addWidget(QLabel(f"Selected: {len(selected)} loans.\n\nLoans will be caught up until the specified date.\nThis operation can be undone."))
            
            form = QFormLayout()
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDate(QDate(default_target.year, default_target.month, default_target.day))
            date_edit.setDisplayFormat("yyyy-MM-dd")
            form.addRow("Catch up until (inclusive):", date_edit)
            layout.addLayout(form)
            
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(d.accept)
            buttons.rejected.connect(d.reject)
            layout.addWidget(buttons)
            
            if d.exec() == QDialog.DialogCode.Accepted:
                target_date_q = date_edit.date()
                target_date = datetime(target_date_q.year(), target_date_q.month(), target_date_q.day())
                processed_count = 0
                total_deductions = 0
                
                # Progress bar? For simplicity, synchronous loop.
                # If list is huge, UI might freeze.
                
                # Setup Progress Dialog
                progress = QProgressDialog("Processing Loans...", "Cancel", 0, len(selected), dialog)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.setMinimumDuration(0)
                progress.setValue(0)
                progress.show()

                def progress_cb(i, cb_item):
                     if progress.wasCanceled():
                         # In a real generator/callback, we need to signal stop.
                         # But here we just break update? 
                         # Actually if we raise Exception in callback it stops the engine loop?
                         raise Exception("Operation Canceled by User")
                     
                     # Extract name from checkbox text for feedback
                     # Use selected[i] because cb_item might be normalized tuple/id
                     current_cb = selected[i]
                     current_name = current_cb.text().split("|")[0].strip()
                     progress.setLabelText(f"Processing {current_name}...")
                     progress.setValue(i + 1)
                     QApplication.processEvents()

                try:
                    result = engine.mass_catch_up_loans(selected, progress_cb, target_date=target_date)
                    if len(result) == 3:
                        processed_count, total_deductions, errors = result
                    else:
                        processed_count, total_deductions = result
                        errors = []
                except Exception as e:
                    if "Canceled" in str(e):
                        QMessageBox.information(dialog, "Canceled", "Operation canceled. No changes were made (Transaction Rolled Back).")
                        progress.close()
                        return
                    else:
                        QMessageBox.critical(dialog, "Error", f"An error occurred: {e}")
                        progress.close()
                        return
                
                progress.close()
                
                if errors:
                    self.show_error_report(errors)
                
                QMessageBox.information(dialog, "Result", f"Process Complete.\nLoans Updated: {processed_count}\nTotal Deduction Transactions: {total_deductions}")
                dialog.accept()
        
        run_btn.clicked.connect(run_process)
        
        dialog.exec()
        # Maybe refresh list/ledger if open?
        # Dashboard doesn't show loan status directly so list refresh not strictly needed, but good practice.

    def undo_mass_operation(self):
        """Undo the last mass operation."""
        if not self.engine.can_undo():
            QMessageBox.information(self, "Undo", "Nothing to undo.")
            return
            
        # Optional: Check if the top item is a Mass Operation?
        desc = self.engine.get_undo_description()
        if not desc or not desc.startswith("Mass"):
             reply = QMessageBox.question(self, "Undo", 
                                          f"The last operation was '{desc}', not a Mass Operation.\nUndo anyway?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
             if reply == QMessageBox.StandardButton.No:
                 return

        cmd = self.engine.undo()
        if cmd:
            QMessageBox.information(self, "Undo", f"Undid: {cmd.description}")
            self.refresh_list()
        else:
             QMessageBox.warning(self, "Undo", "Failed to undo.")

    def _open_fund_report_dialog(self, fund, label):
        """Report dialog for a savings-style fund.

        Savings offers Quarter OR Custom; Christmas/Benevolent are custom-only
        (the monthly PF/Name/Status/months/Total layout).
        """
        from ..reports import ReportGenerator
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QDialogButtonBox,
                                     QFileDialog, QComboBox, QPushButton, QRadioButton, QDateEdit)
        from PyQt6.QtCore import QDate
        from dateutil.relativedelta import relativedelta
        from datetime import datetime
        import platform, subprocess, os

        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"{label} Report")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()

        allow_quarter = (fund == "savings")  # Christmas/Benevolent are custom-only
        rb_quarter = QRadioButton("Quarter")
        rb_custom = QRadioButton("Custom period")

        quarter_combo = QComboBox()
        def_dt = generator.get_default_quarter_date()
        default_idx = 0
        for i, q_start in enumerate(generator.get_recent_quarters()):
            m3_end = q_start + relativedelta(months=3, days=-1)
            quarter_combo.addItem(f"{q_start.strftime('%b %Y')} - {m3_end.strftime('%b %Y')}",
                                  q_start.strftime("%Y-%m-%d"))
            if q_start.year == def_dt.year and q_start.month == def_dt.month:
                default_idx = i
        quarter_combo.setCurrentIndex(default_idx)

        if allow_quarter:
            rb_quarter.setChecked(True)
            form.addRow(rb_quarter)
            form.addRow(rb_custom)
            form.addRow("Quarter:", quarter_combo)
        else:
            rb_custom.setChecked(True)

        # Custom period defaults to the current fiscal year.
        today = QDate.currentDate()
        fy = generator._get_fy_start_date(datetime(today.year(), today.month(), today.day()))
        from_date = QDateEdit(); from_date.setCalendarPopup(True)
        from_date.setDate(QDate(fy.year, fy.month, fy.day))
        to_date = QDateEdit(); to_date.setCalendarPopup(True)
        to_date.setDate(today)
        form.addRow("From:", from_date)
        form.addRow("To:", to_date)

        def update_mode():
            q = rb_quarter.isChecked()
            quarter_combo.setEnabled(q)
            from_date.setEnabled(not q)
            to_date.setEnabled(not q)
        rb_quarter.toggled.connect(update_mode)
        update_mode()

        layout.addLayout(form)

        fmt_btn = QPushButton("Format Settings...")

        def open_fmt():
            from ..dialogs import ExcelFormatDialog
            ExcelFormatDialog(self.db, self).exec()
        fmt_btn.clicked.connect(open_fmt)
        layout.addWidget(fmt_btn)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout.addWidget(btns)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        custom = rb_custom.isChecked()
        if custom:
            start = from_date.date().toString("yyyy-MM-dd")
            end = to_date.date().toString("yyyy-MM-dd")
            suggested = f"{label.replace('/', '-')}_Report_{start}_to_{end}.xlsx"
        else:
            start = quarter_combo.currentData()
            end = None
            suggested = f"{label.replace('/', '-')}_Quarterly_{start}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Report", suggested,
            "Excel Files (*.xlsx);;PDF Files (*.pdf);;CSV Files (*.csv)")
        if not path:
            return

        if fund == "savings" and not custom:
            success, msg = generator.generate_quarterly_savings_report(start, path)
        else:
            success, msg = generator.generate_fund_report(fund, path, start, end)

        if success:
            QMessageBox.information(self, "Success", f"Report saved to:\n{path}")
            try:
                if platform.system() == 'Windows':
                    os.startfile(path)
                elif platform.system() == 'Darwin':
                    subprocess.Popen(['open', path])
                else:
                    subprocess.Popen(['xdg-open', path])
            except Exception:
                pass
        else:
            QMessageBox.critical(self, "Error", f"Failed to generate report:\n{msg}")

    def generate_quarterly_report(self):
        """Generate quarterly loan interest report."""
        from ..reports import ReportGenerator
        from PyQt6.QtWidgets import QDialog, QFormLayout, QDialogButtonBox, QFileDialog, QMessageBox, QComboBox, QPushButton
        from PyQt6.QtCore import QDate
        from dateutil.relativedelta import relativedelta
        
        # Determine Default Date based on FY Setting
        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)
        def_dt = generator.get_default_quarter_date()

        # Dialog to select start date
        dialog = QDialog(self)
        dialog.setWindowTitle("Quarterly Loan Report Period")
        layout = QFormLayout(dialog)
        
        # Quarter Selector (Dropdown)
        quarter_combo = QComboBox()
        
        # Populate
        recent_quarters = generator.get_recent_quarters()
        default_idx = 0
        
        for i, q_start in enumerate(recent_quarters):
            # Calculate End Date for label
            # Month 3 end
            m3_end = q_start + relativedelta(months=3, days=-1)
            label = f"{q_start.strftime('%b %Y')} - {m3_end.strftime('%b %Y')}"
            
            quarter_combo.addItem(label, q_start.strftime("%Y-%m-%d"))
            
            # Select default
            if q_start.year == def_dt.year and q_start.month == def_dt.month:
                default_idx = i
                
        quarter_combo.setCurrentIndex(default_idx)
        layout.addRow("Select Quarter:", quarter_combo)
        
        # Format Settings Button
        fmt_btn = QPushButton("Format Settings...")
        fmt_btn.setStyleSheet("color: #2b5797; border: 1px solid #2b5797; padding: 4px; border-radius: 4px;")
        
        def open_fmt_dialog():
            from ..dialogs import ExcelFormatDialog
            dlg = ExcelFormatDialog(self.db, self)
            dlg.exec()
            
        fmt_btn.clicked.connect(open_fmt_dialog)
        layout.addRow("", fmt_btn)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout.addRow(btns)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            start_date = quarter_combo.currentData()
            
            # File save dialog
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Report", 
                f"Quarterly_Report_{start_date}.xlsx", 
                "Excel Files (*.xlsx);;CSV Files (*.csv);;PDF Files (*.pdf)"
            )
            
            if file_path:
                # Pass printer_view_getter for PDF support
                # generator already instantiated
                
                # Setup Progress Dialog
                progress = QProgressDialog("Generating Report...", "Cancel", 0, 100, self)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.setMinimumDuration(0)
                progress.setValue(0)
                
                # Setup Thread
                db_name = self.db.db_name if hasattr(self.db, 'db_name') else "loan_master.db"
                self.report_worker = ReportWorker(db_name, self.get_printer_view, start_date, file_path, report_type='loan')
                
                def on_progress(curr, total, msg):
                    progress.setMaximum(total)
                    progress.setValue(curr)
                    progress.setLabelText(msg)
                    if progress.wasCanceled():
                        if getattr(self, 'report_worker', None):
                            self.report_worker.terminate()
                        
                def on_finished(success, message):
                    progress.close()
                    if success:
                        QMessageBox.information(self, "Success", f"Report saved to:\n{file_path}")
                    else:
                        QMessageBox.critical(self, "Error", f"Failed to generate report:\n{message}")
                    self.report_worker = None
                    
                self.report_worker.progress.connect(on_progress)
                self.report_worker.finished.connect(on_finished)
                
                self.report_worker.start()

    def open_mass_savings_dialog(self):
        """Open dialog for Mass Savings Increment."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Mass Savings Increment (Catch Up)")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)
        layout = QVBoxLayout(dialog)
        
        layout.addWidget(QLabel("<b>Select individuals to increment savings:</b>"))
        layout.addWidget(QLabel("Checked individuals will be processed to catch up savings contributions."))
        default_amount = self.db.get_setting("default_savings_increment", "2500")
        layout.addWidget(QLabel(f"<i>Amt will be auto-detected from each user's last transaction (Default: {default_amount}).</i>"))
        
        # Get all individuals
        individuals = self.db.get_individuals()
        individuals.sort(key=lambda x: x[1].lower())
        
        if not individuals:
            layout.addWidget(QLabel("No individuals found."))
            btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            btns.rejected.connect(dialog.reject)
            layout.addWidget(btns)
            dialog.exec()
            return
            
        # Filter Input
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("Filter by name...")
        layout.addWidget(filter_input)
            
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        
        checkboxes = []
        
        # Helper to get suggested amount efficiently? 
        # get_suggested_savings_increment does a query. Might be slow if N is large.
        # But for local app < 1000 users it's fine.
        engine = self.engine
        
        for ind in individuals:
            # Skip retired individuals from mass savings
            ind_details = self.db.get_individual(ind[0])
            if ind_details and ind_details.get('is_retired', 0):
                continue
            
            label_text = f"{ind[1]}"
            cb = QCheckBox(label_text)
            cb.setChecked(False) 
            bal = self.db.get_savings_balance(ind[0])
            
            # Auto-detect amount
            auto_amt = engine.get_suggested_savings_increment(ind[0])
            
            if bal > 0:
                cb.setChecked(True)
                cb.setText(label_text + f" (Bal: {bal:,.0f}) [Auto: {auto_amt:,.0f}]")
            else:
                 # Even if bal is 0, show what auto would correspond to (likely 2500)
                 cb.setText(label_text + f" [Auto: {auto_amt:,.0f}]")
            
            cb.setProperty("ind_id", ind[0])
            checkboxes.append(cb)
            scroll_layout.addWidget(cb)
            
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll)
        
        # Filter Logic
        def filter_items(text):
            text = text.lower()
            for cb in checkboxes:
                if text in cb.text().lower():
                    cb.setVisible(True)
                else:
                    cb.setVisible(False)
        
        filter_input.textChanged.connect(filter_items)
        
        # Select/Deselect All
        btn_layout = QHBoxLayout()
        sel_all = QPushButton("Select All Visible")
        
        def select_visible():
            for cb in checkboxes:
                if cb.isVisible():
                    cb.setChecked(True)
        
        sel_all.clicked.connect(select_visible)
        
        clr_all = QPushButton("Clear All")
        clr_all.clicked.connect(lambda: [cb.setChecked(False) for cb in checkboxes])
        btn_layout.addWidget(sel_all)
        btn_layout.addWidget(clr_all)
        layout.addLayout(btn_layout)
        
        # Run Button
        run_btn = QPushButton("Run Mass Increment")
        run_btn.setStyleSheet("background-color: #20c997; color: white; font-weight: bold; padding: 10px;")
        layout.addWidget(run_btn)
        
        # engine already init above
        
        def run_process():
            selected = [cb for cb in checkboxes if cb.isChecked()]
            if not selected:
                QMessageBox.warning(dialog, "Warning", "No individuals selected.")
                return
            
            # Ask for Target Date instead of simple Confirm
            from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDateEdit, QDialogButtonBox, QFormLayout
            from PyQt6.QtCore import QDate
            from datetime import datetime
            
            # Default to Current Month
            default_target = datetime.now()
            
            d = QDialog(dialog)
            d.setWindowTitle("Confirm Mass Catch-Up")
            
            layout = QVBoxLayout(d)
            layout.addWidget(QLabel(f"Selected: {len(selected)} individuals.\n\nAmounts will be based on last deposit.\nThis operation can be undone."))
            
            form = QFormLayout()
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDate(QDate(default_target.year, default_target.month, default_target.day))
            date_edit.setDisplayFormat("yyyy-MM-dd")
            form.addRow("Catch up until (inclusive):", date_edit)
            layout.addLayout(form)
            
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(d.accept)
            buttons.rejected.connect(d.reject)
            layout.addWidget(buttons)
            
            if d.exec() == QDialog.DialogCode.Accepted:
                target_date_q = date_edit.date()
                target_date = datetime(target_date_q.year(), target_date_q.month(), target_date_q.day())
                processed_count = 0
                total_tx = 0
                
                # Setup Progress Dialog
                progress = QProgressDialog("Processing Savings...", "Cancel", 0, len(selected), dialog)
                progress.setWindowModality(Qt.WindowModality.WindowModal)
                progress.setMinimumDuration(0)
                progress.setValue(0)
                progress.show()

                def progress_cb(i, cb_item):
                     if progress.wasCanceled():
                         raise Exception("Operation Canceled by User")

                     # Extract name 
                     # Use selected[i] because cb_item might be normalized tuple/id
                     current_cb = selected[i]
                     current_name = current_cb.text().split("(")[0].split("[")[0].strip()
                     progress.setLabelText(f"Processing {current_name}...")
                     
                     progress.setValue(i + 1)
                     QApplication.processEvents()

                try:
                    result = engine.mass_catch_up_savings(selected, progress_cb, target_date=target_date)
                    if len(result) == 3:
                        processed_count, total_tx, errors = result
                    else:
                        processed_count, total_tx = result
                        errors = []
                    
                    progress.close()
                    
                    if errors:
                        self.show_error_report(errors)
                    
                    QMessageBox.information(dialog, "Result", f"Process Complete.\nIndividuals Funded: {processed_count}\nTotal Transactions: {total_tx}")
                    dialog.accept()
                except Exception as e:
                    if "Canceled" in str(e):
                         QMessageBox.information(dialog, "Canceled", "Operation canceled. No changes were made (Transaction Rolled Back).")
                         progress.close()
                         return
                    else:
                         QMessageBox.critical(dialog, "Error", f"An error occurred: {e}")
                         progress.close()
                         return
                
                progress.close()
                
                if total_tx == 0:
                     QMessageBox.information(dialog, "Up to Date", "Savings are already up to date for selected individuals.")
                else:    
                     QMessageBox.information(dialog, "Success", f"Process Complete.\nIndividuals Updated: {processed_count}\nTotal Deposits Created: {total_tx}")
                
                dialog.accept()
        
        run_btn.clicked.connect(run_process)
        
        dialog.exec()

    def show_import_history(self):
        """Show the Import History and Undo dialog."""
        dlg = ImportHistoryDialog(self.db, self)
        dlg.exec()
        # Refresh UI after potential undo
        self.load_individuals()

    def import_from_excel(self):
        """Import members / fund contributions from an .xlsx file."""
        dlg = ExcelImportDialog(self.db, self)
        if dlg.exec():
            self.load_individuals()

    def show_activity_log(self):
        """View the CRUD audit trail (member/loan create/update/delete with dates)."""
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
                                     QTableWidgetItem, QHeaderView, QDialogButtonBox)
        entries = self.db.get_audit_log(limit=1000)
        dlg = QDialog(self)
        dlg.setWindowTitle("Activity Log")
        dlg.setMinimumSize(680, 500)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Most recent {len(entries)} create/update/delete events:"))
        table = QTableWidget()
        table.setColumnCount(4)
        table.setHorizontalHeaderLabels(["When", "Operation", "Type", "Who / What"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            table.setItem(i, 0, QTableWidgetItem(str(e["ts"])))
            table.setItem(i, 1, QTableWidgetItem(e["operation"]))
            table.setItem(i, 2, QTableWidgetItem(e["entity"]))
            table.setItem(i, 3, QTableWidgetItem(str(e["summary"] or "")))
        layout.addWidget(table)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        layout.addWidget(bb)
        dlg.exec()

    def open_repair_loans_dialog(self):
        """Scan for legacy loans with stale terms / contaminated accruals and heal them."""
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
                                     QTableWidgetItem, QHeaderView, QDialogButtonBox, QProgressDialog)
        import shutil
        from datetime import datetime

        healable = self.engine.scan_healable_loans()
        dlg = QDialog(self)
        dlg.setWindowTitle("Repair Loans")
        dlg.setMinimumSize(640, 460)
        layout = QVBoxLayout(dlg)

        if not healable:
            layout.addWidget(QLabel("✓ No legacy loans need repair — all active loans "
                                    "compute with the current logic."))
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
            layout.addWidget(bb)
            dlg.exec()
            return

        layout.addWidget(QLabel(
            f"<b>{len(healable)} simple loan(s)</b> were created under older logic and will be "
            "repaired: stored terms are re-derived from each loan's issue note and contaminated "
            "accruals are recomputed. Repayment history is preserved. (Loans with top-ups or "
            "edited repayments are left untouched.)"))

        table = QTableWidget()
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["Member", "Loan", "What gets fixed"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setRowCount(len(healable))
        for i, h in enumerate(healable):
            issues = []
            if "total_amount" in h["changes"]:
                issues.append("total amount")
            if "installment" in h["changes"]:
                issues.append("installment")
            if h["contaminated_accruals"]:
                issues.append(f"{h['contaminated_accruals']} accrual(s)")
            table.setItem(i, 0, QTableWidgetItem(h["name"]))
            table.setItem(i, 1, QTableWidgetItem(h["ref"]))
            table.setItem(i, 2, QTableWidgetItem(", ".join(issues)))
        layout.addWidget(table)

        bb = QDialogButtonBox()
        repair_btn = bb.addButton(f"Repair All ({len(healable)})", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        def do_repair():
            if QMessageBox.question(
                    dlg, "Confirm Repair",
                    f"Repair {len(healable)} loan(s)? The database will be backed up first.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                    != QMessageBox.StandardButton.Yes:
                return
            src = getattr(self.db, "db_name", None)
            if src:
                try:
                    shutil.copy(src, f"{src}.pre-repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bak")
                except Exception as e:
                    QMessageBox.warning(dlg, "Backup", f"Could not back up DB ({e}). Aborted.")
                    return
            progress = QProgressDialog("Repairing loans…", None, 0, len(healable), dlg)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            done = 0
            for i, h in enumerate(healable):
                self.engine.heal_loan_terms(h["individual_id"], h["ref"])
                done += 1
                progress.setValue(i + 1)
            progress.close()
            QMessageBox.information(dlg, "Repair Complete", f"Repaired {done} loan(s).")
            self.load_individuals()
            dlg.accept()

        repair_btn.clicked.connect(do_repair)
        dlg.exec()

    def import_individuals(self):
        """Import individuals from another database with selection and progress."""
        try:
            dialog = ImportDialog(self)
            if dialog.exec():
                data = dialog.get_data()
                file_path = data["file_path"]
                options = {
                    "import_loans": data["import_loans"],
                    "import_savings": data["import_savings"],
                    "import_funds": data.get("import_funds", False),
                    "date_range": data["date_range"]
                }
                
                if not file_path or not os.path.exists(file_path):
                    QMessageBox.critical(self, "Error", "File does not exist.")
                    return
                
                # 0. Validate Schema
                is_valid, error_msg = self.db.validate_source_schema(file_path)
                if not is_valid:
                     QMessageBox.critical(self, "Invalid Database", f"The selected file cannot be imported:\n{error_msg}")
                     return

                # 1. Get Preview
                preview_list = self.db.get_import_preview(file_path)
                if not preview_list:
                    QMessageBox.warning(self, "Error", "Could not read individuals from the database (or it is empty).")
                    return
                    
                # 2. Selection Dialog
                sel_dialog = QDialog(self)
                sel_dialog.setWindowTitle("Select Individuals to Import")
                sel_dialog.setMinimumWidth(400)
                sel_dialog.setMinimumHeight(500)
                layout = QVBoxLayout(sel_dialog)
                
                layout.addWidget(QLabel(f"Found {len(preview_list)} individuals. Select who to import:"))
                
                # Filter input
                filter_input = QLineEdit()
                filter_input.setPlaceholderText("Filter name...")
                layout.addWidget(filter_input)
                
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll_widget = QWidget()
                scroll_layout = QVBoxLayout(scroll_widget)
                
                checkboxes = []
                for ind in preview_list:
                    label = f"{ind['name']}"
                    if ind.get('phone'): label += f" | {ind['phone']}"
                    
                    cb = QCheckBox(label)
                    cb.setChecked(True) # Default all
                    cb.setProperty("src_id", ind['id'])
                    checkboxes.append(cb)
                    scroll_layout.addWidget(cb)
                
                scroll.setWidget(scroll_widget)
                layout.addWidget(scroll)
                
                # Filter Logic
                def filter_cb(text):
                    t = text.lower()
                    for cb in checkboxes:
                        cb.setHidden(t not in cb.text().lower())
                filter_input.textChanged.connect(filter_cb)
                
                # Buttons
                btn_layout = QHBoxLayout()
                sel_all = QPushButton("Select All")
                sel_all.clicked.connect(lambda: [cb.setChecked(True) for cb in checkboxes])
                clr_all = QPushButton("Clear All")
                clr_all.clicked.connect(lambda: [cb.setChecked(False) for cb in checkboxes])
                btn_layout.addWidget(sel_all)
                btn_layout.addWidget(clr_all)
                layout.addLayout(btn_layout)
                
                btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
                btns.accepted.connect(sel_dialog.accept)
                btns.rejected.connect(sel_dialog.reject)
                layout.addWidget(btns)
                
                if sel_dialog.exec() == QDialog.DialogCode.Accepted:
                    selected_ids = [cb.property("src_id") for cb in checkboxes if cb.isChecked()]
                    
                    if not selected_ids:
                        return

                    # Generate Preview & Resolve Conflicts
                    decision_map = None
                    preview = self.db.generate_import_preview(file_path, selected_ids, options)
                    
                    if preview:
                        preview_dlg = ImportPreviewDialog(preview, self)
                        if preview_dlg.exec() == QDialog.DialogCode.Accepted:
                            decision_map = preview_dlg.get_decisions()
                        else:
                            return # Cancel
                    else:
                        QMessageBox.warning(self, "Error", "Failed to generate import preview.")
                        return
                    
                    # 3. Perform Import IN THREAD
                    
                    # Create Progress Dialog
                    progress = QProgressDialog("Starting Import...", None, 0, 100, self)
                    progress.setWindowTitle("Importing Data")
                    progress.setWindowModality(Qt.WindowModality.WindowModal)
                    progress.setMinimumDuration(0)
                    progress.setCancelButton(None) # Disable cancel for now as DB thread is hard to kill safely
                    progress.show()
                    
                    # Worker
                    self.import_worker = ImportWorker(self.db, file_path, selected_ids, options, decision_map)
                    
                    def on_progress(current, total, msg):
                        progress.setLabelText(msg)
                        progress.setMaximum(total)
                        progress.setValue(current)
                        
                    def on_finished(result):
                        progress.close()
                        self.handle_import_result(result, options)
                        
                    def on_error(err_msg):
                        progress.close()
                        QMessageBox.critical(self, "Error", f"Import Error: {err_msg}")
                    
                    self.import_worker.progress.connect(on_progress)
                    self.import_worker.finished.connect(on_finished)
                    self.import_worker.error.connect(on_error)
                    
                    self.import_worker.start()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"An unexpected error occurred: {str(e)}")

    def handle_import_result(self, result, options):
        """Handle the result dict from import worker."""
        if isinstance(result, dict) and "status" in result:
             stats = result.get("stats", {})
             status = result.get("status", "failed")
             errors = result.get("errors", [])
             
             msg = ""
             if status == "success":
                 msg = "Import Process Completed Successfully.\n\n"
                 msg += f"Individuals: {stats.get('individuals', 0)}\n"
                 if options.get("import_loans"):
                     msg += f"Loans: {stats.get('loans', 0)}\n"
                     msg += f"Ledger Entries: {stats.get('ledger', 0)}\n"
                 if options.get("import_savings"):
                     msg += f"Savings Entries: {stats.get('savings', 0)}"
                 
                 QMessageBox.information(self, "Success", msg)
                 self.refresh_list()
                 
             elif status == "partial":
                 msg = "Import Completed with Warnings (Partial Import).\n\n"
                 msg += f"Individuals: {stats.get('individuals', 0)} (Saved)\n"
                 msg += f"Loans: {stats.get('loans', 0)} (Failed/Rolled Back)\n"
                 msg += f"Savings: {stats.get('savings', 0)}\n\n"
                 msg += "Errors encountered:\n" + "\n".join(errors[:5])
                 
                 QMessageBox.warning(self, "Partial Import", msg)
                 self.refresh_list()
                 
             else: # failed
                 msg = "Import Failed.\n\nErrors:\n" + "\n".join(errors[:5])
                 QMessageBox.critical(self, "Error", msg)
                 
        # Fallback for legacy or unexpected return (should not happen with new db code)
        elif isinstance(result, int): 
            if result >= 0:
                QMessageBox.information(self, "Success", f"Import Process Completed (Legacy Mode). Items: {result}")
                self.refresh_list()
            else:
                QMessageBox.critical(self, "Error", "Failed to import (Unknown Error).")
        else:
             QMessageBox.critical(self, "Error", "Unexpected response from database.")

    def show_error_report(self, errors):
        """Show error report dialog."""
        if not errors: return
        
        msg = f"Operation completed with {len(errors)} errors:\n\n"
        # Limit to first 10 errors to avoid huge msg box
        shown_errors = errors[:10]
        for ref, err in shown_errors:
            msg += f"- {ref}: {err}\n"
            
        if len(errors) > 10:
            msg += f"\n... and {len(errors) - 10} more."
            
        QMessageBox.warning(self, "Partial Failures", msg)

    def open_treasury(self):
        """Open the Treasury and General Ledger dialog."""
        from .treasury import TreasuryDialog
        dialog = TreasuryDialog(self.db, self.theme_manager, self)
        dialog.exec()


from PyQt6.QtCore import QThread, pyqtSignal

class ImportWorker(QThread):
    """Worker thread for running imports."""
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    
    
    def __init__(self, db, file_path, selected_ids, options, decision_map=None):
        super().__init__()
        self.db = db
        self.file_path = file_path
        self.selected_ids = selected_ids
        self.options = options
        self.decision_map = decision_map
        
    def run(self):
        try:
            def callback(current, total, msg):
                self.progress.emit(current, total, msg)
                
            result = self.db.import_selected_data(
                self.file_path, 
                self.selected_ids, 
                self.options, 
                progress_callback=callback,
                decision_map=self.decision_map
            )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))
