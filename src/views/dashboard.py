"""Dashboard view for LoanMaster."""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QListWidget, QListWidgetItem,
                             QTableWidgetItem, QLineEdit, QMessageBox,
                             QFileDialog, QDialog, QFormLayout, QCheckBox,
                             QScrollArea, QDialogButtonBox, QMenu, QFrame, QGraphicsDropShadowEffect, QProgressDialog, QApplication)
from PyQt6.QtGui import QAction, QPixmap, QFont, QColor
from PyQt6.QtCore import Qt, QSize
from datetime import datetime
import os
import sys


from ..dialogs import IndividualDialog, ImportDialog, StatementConfigDialog
from ..theme import ThemeManager


class IndividualCard(QFrame):
    """A custom widget to display individual details in the list."""
    
    def __init__(self, ind_id, name, phone, email, parent_dashboard):
        super().__init__()
        self.ind_id = ind_id
        self.name = name
        self.phone = phone
        self.email = email
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
        self.import_btn.clicked.connect(self.import_individuals)
        
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
        
        actions_layout.addWidget(self.open_btn)
        actions_layout.addWidget(self.edit_btn)
        actions_layout.addStretch()
        actions_layout.addWidget(self.delete_btn)
        
        content_layout.addWidget(self.actions_container)
        
        # --- FOOTER / UTILITY SECTION ---
        utility_layout = QHBoxLayout()
        utility_layout.setSpacing(15)
        
        self.print_selected_btn = QPushButton("Print Statements")
        self.print_selected_btn.clicked.connect(self.batch_print_selected)
        self.print_selected_btn.setStyleSheet("background-color: #3b82f6; color: white; padding: 8px 15px; border-radius: 5px;")
        
        self.report_btn = QPushButton("Quarterly Report")
        self.report_btn.clicked.connect(self.generate_quarterly_report)
        self.report_btn.setStyleSheet("background-color: #10B981; color: white; padding: 8px 15px; border-radius: 5px;")
        
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
        
        self.mass_ops_btn.setMenu(mass_menu)
        
        utility_layout.addWidget(self.print_selected_btn)
        utility_layout.addWidget(self.report_btn)
        utility_layout.addStretch()
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
        self.report_btn.setStyleSheet(f"background-color: {t.get_color('success')}; color: white; padding: 8px 15px; border-radius: 5px;")
        self.settings_btn.setStyleSheet(f"background-color: {t.get_color('text_secondary')}; color: white; padding: 8px 15px; border-radius: 5px;")
        self.mass_ops_btn.setStyleSheet(f"background-color: #8B5CF6; color: white; padding: 8px 15px; border-radius: 5px;") # Purple

        # Update all cards
        for card in self.card_widgets:
            card.apply_theme()


    def refresh_list(self):
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
             # ind: [id, name, phone, email]
             card = IndividualCard(ind[0], ind[1], ind[2], ind[3], self)
             self.scroll_content_layout.addWidget(card)
             self.card_widgets.append(card)
        
        # Sync to UIStateManager
        self._ui_state.set_cards(self.card_widgets)

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
            # Open Config Dialog
            conf_dialog = StatementConfigDialog(dialog)
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
            
            # Check if actually overdue?
            # catch_up_loan works if next_due <= current_date.
            # Visual cue if up to date?
            is_overdue = loan['next_due_date'] <= datetime.now().strftime("%Y-%m-%d")
            if not is_overdue:
                cb.setText(label_text + " (Up to Date)")
                cb.setEnabled(False) # Can't catch up if up to date
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
        
        from ..engine import LoanEngine
        engine = LoanEngine(self.db)
        
        def run_process():
            selected = [cb for cb in checkboxes if cb.isChecked()]
            if not selected:
                QMessageBox.warning(dialog, "Warning", "No loans selected.")
                return
            
            confirm = QMessageBox.question(dialog, "Confirm", 
                                           f"Are you sure you want to deduct/catch up for {len(selected)} loans?\n\nThis can be undone via Edit > Undo.",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
            if confirm == QMessageBox.StandardButton.Yes:
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
                    result = engine.mass_catch_up_loans(selected, progress_cb)
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

    def generate_quarterly_report(self):
        """Generate quarterly interest report."""
        from ..reports import ReportGenerator
        from PyQt6.QtWidgets import QDateEdit, QDialog, QFormLayout, QDialogButtonBox, QFileDialog, QMessageBox
        from PyQt6.QtCore import QDate
        
        # Determine Default Date based on FY Setting
        # Logic: Find the most recent "Quarter Start" relative to today.
        # Quarters are [Start, Start+3, Start+6, Start+9]
        
        fy_start_str = self.db.get_setting("fy_start_month", "November")
        months = ["January", "February", "March", "April", "May", "June", 
                  "July", "August", "September", "October", "November", "December"]
        try:
            start_month_idx = months.index(fy_start_str) + 1 # 1-based
        except ValueError:
            start_month_idx = 1
            
        today = QDate.currentDate()
        current_year = today.year()
        
        # Quarters for this year:
        q_starts = []
        for i in range(4):
            m = start_month_idx + (i * 3)
            y = current_year
            if m > 12:
                m -= 12
                # If start is late in year, next quarters spill to next year conceptually, 
                # but we want to find "most recent start".
                # Let's simplify: Build a list of candidate dates around today
            q_starts.append((y, m))
        
        # Also check previous year for context (e.g. if FY starts Nov, and it is Jan)
        for i in range(4):
            m = start_month_idx + (i * 3)
            y = current_year - 1
            if m > 12: m -= 12
            q_starts.append((y, m))
            
        # Convert to QDate and find closest in past
        candidates = []
        for y, m in q_starts:
            # Handle overflow logic properly if needed, but simplistic mapping works for 1-12
            # Wait, if start_month_idx is 11 (Nov):
            # Q1: Nov (11)
            # Q2: Feb (14 -> 2, Year+1)
            # Q3: May (17 -> 5, Year+1)
            # Q4: Aug (20 -> 8, Year+1)
            
            # Correct logic:
            base_date = datetime(current_year - 1, start_month_idx, 1) # Start of FY last year
            # Generate 8 quarters forward
            from dateutil.relativedelta import relativedelta
            
            for i in range(8):
                d = base_date + relativedelta(months=i*3)
                qd = QDate(d.year, d.month, d.day)
                if qd <= today:
                    candidates.append(qd)
        
        candidates.sort()
        default_date = candidates[-1] if candidates else QDate.currentDate()

        # Dialog to select start date
        dialog = QDialog(self)
        dialog.setWindowTitle("Quarterly Report Period")
        layout = QFormLayout(dialog)
        
        start_date_edit = QDateEdit()
        start_date_edit.setCalendarPopup(True)
        start_date_edit.setDate(default_date)
        layout.addRow("Quarter Start Date:", start_date_edit)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout.addRow(btns)
        
        if dialog.exec() == QDialog.DialogCode.Accepted:
            start_date = start_date_edit.date().toString("yyyy-MM-dd")
            
            # File save dialog
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Report", 
                f"Quarterly_Report_{start_date}.xlsx", 
                "Excel Files (*.xlsx)"
            )
            
            if file_path:
                generator = ReportGenerator(self.db)
                success = generator.generate_quarterly_report(start_date, file_path)
                
                if success:
                    QMessageBox.information(self, "Success", f"Report saved to:\n{file_path}")
                else:
                    QMessageBox.critical(self, "Error", "Failed to generate report. Check console for details.")

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
        from ..engine import LoanEngine
        engine = LoanEngine(self.db)
        
        for ind in individuals:
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
            
            confirm = QMessageBox.question(dialog, "Confirm", 
                                           f"Are you sure you want to catch up savings for {len(selected)} individuals?\nAmounts will be based on last deposit.\n\nThis can be undone via Edit > Undo.",
                                           QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            
            if confirm == QMessageBox.StandardButton.Yes:
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
                    result = engine.mass_catch_up_savings(selected, progress_cb)
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

    def import_individuals(self):
        """Import individuals from another database with selection."""
        try:
            dialog = ImportDialog(self)
            if dialog.exec():
                data = dialog.get_data()
                file_path = data["file_path"]
                options = {
                    "import_loans": data["import_loans"],
                    "import_savings": data["import_savings"]
                }
                
                if not file_path or not os.path.exists(file_path):
                    QMessageBox.critical(self, "Error", "File does not exist.")
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

                    # 3. Perform Import
                    count = self.db.import_selected_data(file_path, selected_ids, options)
                    
                    if isinstance(count, dict):
                         # New return format is stats dict? Or did I leave it returning int/dict mixed?
                         # import_selected_data returns 'start' dict usually?
                         # Let's check db implementation again.
                         # It returns 'stats' dict.
                         msg = "Import Complete.\n\n"
                         msg += f"Individuals: {count['individuals']}\n"
                         msg += f"Loans: {count['loans']}\n"
                         msg += f"Ledger Entries: {count['ledger']}"
                         QMessageBox.information(self, "Success", msg)
                         self.refresh_list()
                    elif count >= 0: # Fallback if I messed up return
                        QMessageBox.information(self, "Success", f"Import Process Completed.")
                        self.refresh_list()
                    else:
                        QMessageBox.critical(self, "Error", "Failed to import.")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"An unexpected error occurred: {str(e)}")
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
