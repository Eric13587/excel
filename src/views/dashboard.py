"""Dashboard view for LoanMaster."""
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QPushButton, QListWidget, QListWidgetItem,
                             QTableWidgetItem, QLineEdit, QMessageBox,
                             QFileDialog, QDialog, QFormLayout, QCheckBox,
                             QScrollArea, QDialogButtonBox, QMenu, QFrame, QGraphicsDropShadowEffect, QProgressDialog, QApplication)
from PyQt6.QtGui import QAction, QPixmap, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from datetime import datetime
import logging
import os
import sys

logger = logging.getLogger(__name__)


from src.dialogs import IndividualDialog, ImportDialog, StatementConfigDialog, ImportHistoryDialog, ImportPreviewDialog, ExcelImportDialog
from src.reports import ReportGenerator
from ..theme import ThemeManager
from ..database import DatabaseManager
from ..money import format_money


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

    def contextMenuEvent(self, event):
        """Per-member actions on right-click, so the list needs no button bar."""
        self.dashboard.select_individual(self)
        menu = QMenu(self)
        open_a = menu.addAction("Open Ledger")
        edit_a = menu.addAction("Edit Details…")
        retire_a = menu.addAction("Reinstate" if self.is_retired else "Retire")
        menu.addSeparator()
        delete_a = menu.addAction("Delete")
        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return
        if chosen == open_a:
            self.dashboard.open_ledger_by_id(self.ind_id, self.name)
        elif chosen == edit_a:
            self.dashboard.edit_individual()
        elif chosen == retire_a:
            self.dashboard.retire_individual_btn()
        elif chosen == delete_a:
            self.dashboard.delete_individual()

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



class _ClickableFrame(QFrame):
    """A KPI tile that can be pressed.

    A number that answers "how many" invites the question "which ones", and the
    tile is where that question is asked. Plain QFrame swallows clicks, so the
    press is caught here and handed to the callback the tile was built with.
    """

    def __init__(self, on_click):
        super().__init__()
        self._on_click = on_click
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self._on_click()
        super().mouseReleaseEvent(event)


class _NumericItem(QTableWidgetItem):
    """A cell that shows a formatted number but sorts on the number itself.

    QTableWidgetItem treats Qt.EditRole and Qt.DisplayRole as the same storage,
    so stashing an int sort key and then calling setText replaces the key with
    the formatted text — after which "2,787" sorts *before* "230", as strings
    do. Keeping the number in a role of its own and comparing it here keeps what
    is displayed and what is ordered independent.
    """

    SORT_ROLE = Qt.ItemDataRole.UserRole + 2

    def __init__(self, text, value):
        super().__init__(text)
        self.setData(self.SORT_ROLE, value)

    def __lt__(self, other):
        mine = self.data(self.SORT_ROLE)
        theirs = other.data(self.SORT_ROLE) if isinstance(other, QTableWidgetItem) else None
        if mine is None or theirs is None:
            return super().__lt__(other)
        return mine < theirs


class _ReportCancelled(Exception):
    """Raised inside the worker's progress callback to unwind on cancel."""


class ReportWorker(QThread):
    """Background worker for report generation."""
    progress = pyqtSignal(int, int, str) # current, total, message
    finished = pyqtSignal(bool, str)     # success, message
    
    finished = pyqtSignal(bool, str)     # success, message
    
    def __init__(self, db_name, printer_view_getter, start_date, output_path,
                 report_type='loan', columns=None, end_date=None, fund=None):
        super().__init__()
        self.db_name = db_name
        self.printer_view_getter = printer_view_getter
        self.start_date = start_date
        self.output_path = output_path
        self.report_type = report_type
        self.columns = columns
        self.end_date = end_date          # 'fund' custom-range reports only
        self.fund = fund                  # 'fund' reports: which ledger
        self._cancelled = False

    def cancel(self):
        """Request a cooperative stop.

        The flag is checked in the progress callback, so the worker unwinds
        through its normal ``finally`` (closing the SQLite connection) instead
        of being killed mid-write by ``terminate()``.
        """
        self._cancelled = True

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
                if self._cancelled:
                    raise _ReportCancelled()
                self.progress.emit(current, total, msg)

            if self.report_type == 'savings':
                success, msg = generator.generate_quarterly_savings_report(
                    self.start_date,
                    self.output_path,
                    columns=self.columns,
                    progress_callback=callback
                )
            elif self.report_type == 'fund':
                success, msg = generator.generate_fund_report(
                    self.fund,
                    self.output_path,
                    self.start_date,
                    self.end_date,
                    columns=self.columns,
                    progress_callback=callback
                )
            else:
                success, msg = generator.generate_quarterly_report(
                    self.start_date,
                    self.output_path,
                    columns=self.columns,
                    progress_callback=callback
                )
            self.finished.emit(success, msg)

        except _ReportCancelled:
            # Cooperative cancel: remove any half-written output so the user
            # can't mistake a truncated file for a finished report.
            try:
                if self.output_path and os.path.exists(self.output_path):
                    os.remove(self.output_path)
            except OSError:
                logger.warning("Could not remove cancelled report output: %s",
                               self.output_path, exc_info=True)
            self.finished.emit(False, "Cancelled")
        except Exception as e:
            self.finished.emit(False, str(e))
        finally:
            if db_manager and hasattr(db_manager, 'conn'):
                try:
                    db_manager.conn.close()
                except Exception:       # noqa: BLE001 - worker teardown
                    # Nothing useful can be done about a connection that will
                    # not close on a worker thread, but it must not vanish
                    # either: it is how a leaked handle gets noticed.
                    logger.warning("Report worker could not close its connection",
                                   exc_info=True)


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

        # Portfolio summary — the home screen's job is money management, so the
        # totals belong up top rather than one member-ledger at a time.
        content_layout.addWidget(self._build_summary_band())

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

        # Empty-state message: shown when there are no members at all, or when a
        # search matches none. It stays in the layout across refreshes (cards are
        # removed around it), so a fresh install and a no-match search are no
        # longer a silent blank panel.
        self.empty_label = QLabel("")
        self.empty_label.setObjectName("emptyState")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setContentsMargins(20, 40, 20, 40)
        self.empty_label.setVisible(False)
        self.scroll_content_layout.addWidget(self.empty_label)

        self.scroll_area.setWidget(self.scroll_content)
        content_layout.addWidget(self.scroll_area)

        # Per-member actions (Open Ledger / Edit / Retire / Delete) live on the
        # card right-click menu; whole-journal actions (Reports, Operations,
        # Settings) live on the window menu bar — so the list stays uncluttered.

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
        except Exception:       # noqa: BLE001 - cosmetic only
             # A theme without a parseable shadow colour must not stop the
             # dashboard painting; debug rather than warning, since the only
             # consequence is a flat card edge.
             logger.debug("Could not apply the shadow colour", exc_info=True)

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

        # Update all cards
        for card in self.card_widgets:
            card.apply_theme()

        # KPI band tracks the theme too; refresh_summary re-sets the arrears colour.
        self._style_summary_band()
        self.refresh_summary()

        if hasattr(self, "empty_label"):
            self.empty_label.setStyleSheet(t.hint_css(15))


    def _make_stat_card(self, caption, on_click=None):
        """One KPI tile: a big value over a small caption. Returns (card, value_label).

        Passing ``on_click`` makes the whole tile pressable and marks it as such
        — a pointer cursor and a "›" on the caption — so it does not look like
        the tiles that only display.
        """
        card = _ClickableFrame(on_click) if on_click else QFrame()
        card.setObjectName("statCard")
        card.setProperty("clickable", bool(on_click))
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(2)
        value = QLabel("—")
        value.setObjectName("statValue")
        cap = QLabel(f"{caption}  ›" if on_click else caption)
        cap.setObjectName("statCaptionLink" if on_click else "statCaption")
        v.addWidget(value)
        v.addWidget(cap)
        return card, value

    def _build_summary_band(self):
        """A row of KPI tiles: members, active loans, outstanding, arrears, savings."""
        self.summary_frame = QFrame()
        row = QHBoxLayout(self.summary_frame)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(16)
        specs = [
            ("stat_members", "Members", None),
            ("stat_loans", "Active Loans", None),
            ("stat_outstanding", "Outstanding", None),
            # The only tile that names work to be done, so it is the only one
            # that opens onto the people behind the number.
            ("stat_arrears", "Loans in Arrears", self.show_arrears_dialog),
            ("stat_savings", "Total Savings", None),
        ]
        for attr, caption, on_click in specs:
            card, value = self._make_stat_card(caption, on_click)
            setattr(self, attr, value)
            if on_click:
                self.arrears_card = card
                card.setToolTip("Click to see who is in arrears, and print the list")
            row.addWidget(card)
        return self.summary_frame

    def _style_summary_band(self):
        """Theme the KPI tiles (called on build and on theme change)."""
        if not hasattr(self, "summary_frame"):
            return
        t = self.theme_manager
        self.summary_frame.setStyleSheet(
            f"#statCard {{ background-color: {t.get_color('card_bg')}; "
            f"border: 1px solid {t.get_color('card_border')}; border-radius: 10px; }} "
            f"#statValue {{ font-size: 22px; font-weight: bold; "
            f"color: {t.get_color('text_primary')}; }} "
            f"#statCaption {{ font-size: 11px; color: {t.get_color('text_secondary')}; }} "
            # The pressable tile borrows the accent colour on its caption and
            # lights its border on hover — the same affordance the member cards
            # use, so "this one does something" reads without a legend.
            f"#statCaptionLink {{ font-size: 11px; font-weight: bold; "
            f"color: {t.get_color('accent')}; }} "
            f"#statCard[clickable=\"true\"]:hover {{ border-color: "
            f"{t.get_color('card_hover_border')}; }}")

    def refresh_summary(self):
        """Recompute the KPI band from cheap aggregate queries (no per-member loop)."""
        if not hasattr(self, "stat_members"):
            return
        try:
            active_loans = self.db.get_all_active_loans()
            outstanding = sum(float(loan.get("balance") or 0) for loan in active_loans)
            members = len(self.db.get_individuals())
            arrears = self.db.get_overdue_count()
            savings = self.db.get_total_savings()
        except Exception:
            logger.exception("Could not compute the dashboard summary")
            return

        self.stat_members.setText(f"{members:,}")
        self.stat_loans.setText(f"{len(active_loans):,}")
        self.stat_outstanding.setText(format_money(outstanding))
        self.stat_savings.setText(format_money(savings))

        # Arrears is the number to act on: colour it when there is anything there.
        self.stat_arrears.setText(f"{arrears:,}")
        colour = self.theme_manager.get_color("danger" if arrears else "text_primary")
        self.stat_arrears.setStyleSheet(
            f"font-size: 22px; font-weight: bold; color: {colour};")

    # ------------------------------------------------------------------ #
    # Arrears
    # ------------------------------------------------------------------ #

    def show_arrears_dialog(self):
        """Who is behind, how far, and a way to print it.

        Opened from the arrears KPI tile and from Reports. The list is the same
        population the tile counts — including suspended loans, which are shown
        with their state rather than hidden, since whether a deliberate hold
        belongs in an arrears chase is the user's judgement and not this
        dialog's. A filter takes them out for whoever wants only real defaults.
        """
        from PyQt6.QtWidgets import QTableWidget, QComboBox, QHeaderView
        from src.config import PAR_THRESHOLD_DAYS
        from src.services import arrears as arrears_service
        from src.reports import ReportGenerator

        as_of = datetime.now().strftime("%Y-%m-%d")
        try:
            all_rows = self.engine.get_arrears(as_of)
        except Exception as e:                  # noqa: BLE001 - report, don't crash
            logger.exception("Could not compute arrears")
            QMessageBox.critical(self, "Loans in Arrears",
                                 f"Could not work out who is in arrears:\n{e}")
            return

        if not all_rows:
            QMessageBox.information(
                self, "Loans in Arrears",
                "Nothing is in arrears — every active loan is up to date as at "
                f"{as_of}.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Loans in Arrears")
        dialog.setMinimumSize(1020, 560)
        layout = QVBoxLayout(dialog)

        headline = QLabel()
        headline.setStyleSheet(
            f"font-size: 14px; font-weight: bold; "
            f"color: {self.theme_manager.get_color('text_primary')};")
        layout.addWidget(headline)

        subhead = QLabel()
        subhead.setWordWrap(True)
        subhead.setStyleSheet(self.theme_manager.hint_css())
        layout.addWidget(subhead)

        controls = QHBoxLayout()
        search = QLineEdit()
        search.setPlaceholderText("Filter by member, PF number or loan…")
        search.setClearButtonEnabled(True)
        controls.addWidget(search, 2)

        band_filter = QComboBox()
        band_filter.addItem("All classifications", "")
        for label in arrears_service.BAND_LABELS:
            band_filter.addItem(label, label)
        controls.addWidget(band_filter, 1)

        hide_suspended = QCheckBox("Hide suspended loans")
        hide_suspended.setToolTip(
            "Suspended loans are past due because deductions were deliberately "
            "paused, not because a member defaulted.")
        controls.addWidget(hide_suspended)
        layout.addLayout(controls)

        columns = ReportGenerator.ARREARS_COLUMNS
        table = QTableWidget()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels([label for _key, label in columns])
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Not setStretchLastSection: the last column is the loan's status, and
        # stretching it to whatever space is left elided "Suspended (open-ended)"
        # down to "Suspended (open-…". Any slack goes to the member's name
        # instead, which is the column that can always use more room.
        layout.addWidget(table)

        hint = QLabel(
            "Double-click a row to open that member's ledger. “Overdue (est.)” "
            "is missed instalments × the current instalment, capped at the "
            "balance — the ledger records what was received, not what was due.")
        hint.setWordWrap(True)
        hint.setStyleSheet(self.theme_manager.hint_css())
        layout.addWidget(hint)

        visible = list(all_rows)
        money_columns = {"installment", "overdue_amount", "balance"}
        numeric_columns = {"days", "months_missed"} | money_columns
        # Which arrears row each table row came from, so the export can follow
        # the order on screen after the user sorts by a column.
        ROW_INDEX = Qt.ItemDataRole.UserRole + 1

        def _fill_width():
            """Give any leftover width to the member column, not the last one."""
            header = table.horizontalHeader()
            used = sum(header.sectionSize(c) for c in range(table.columnCount()))
            slack = table.viewport().width() - used
            if slack > 0:
                header.resizeSection(0, header.sectionSize(0) + slack)

        def repopulate():
            text = search.text().strip().lower()
            band = band_filter.currentData()
            visible.clear()
            for row in all_rows:
                if hide_suspended.isChecked() and row["suspended"]:
                    continue
                if band and row["band"] != band:
                    continue
                if text and text not in " ".join(
                        (row["name"], row["pf_no"], row["ref"], row["phone"])).lower():
                    continue
                visible.append(row)

            # Sorting is switched off while the rows are written, or Qt re-sorts
            # mid-fill and the cells land against the wrong rows.
            table.setSortingEnabled(False)
            table.setRowCount(len(visible))
            for r, row in enumerate(visible):
                for c, (key, _label) in enumerate(columns):
                    value = (ReportGenerator._arrears_status(row) if key == "status"
                             else row[key])
                    if key in numeric_columns:
                        # Shown with separators, sorted as a number.
                        shown = (format_money(value) if key in money_columns
                                 else f"{int(value):,}")
                        item = _NumericItem(shown, int(value))
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight
                                              | Qt.AlignmentFlag.AlignVCenter)
                    else:
                        item = QTableWidgetItem(str(value))
                    if row["suspended"]:
                        item.setForeground(QColor(self.theme_manager.get_color("warning")))
                    elif row["days"] > PAR_THRESHOLD_DAYS:
                        item.setForeground(QColor(self.theme_manager.get_color("danger")))
                    item.setData(Qt.ItemDataRole.UserRole, row["individual_id"])
                    item.setData(ROW_INDEX, r)
                    table.setItem(r, c, item)
            table.setSortingEnabled(True)
            table.resizeColumnsToContents()
            _fill_width()

            totals = arrears_service.summarize(visible)
            headline.setText(
                f"{totals['loans']:,} loan(s) · {totals['members']:,} member(s) · "
                f"{format_money(totals['overdue_amount'])} overdue")
            held = (f" {totals['suspended']} of them are suspended."
                    if totals["suspended"] else "")
            shown = ("" if len(visible) == len(all_rows)
                     else f" Showing {len(visible)} of {len(all_rows)}.")
            subhead.setText(
                f"As at {as_of}. Worst is {totals['worst_days']:,} day(s) past due; "
                f"{format_money(totals['balance'])} of principal sits behind these "
                f"loans.{held}{shown}")

        def rows_in_view():
            """The filtered rows in the order the table is currently showing.

            Printing something other than what is on screen is how a report
            loses an argument, so the export walks the table rather than the
            unsorted list behind it.
            """
            out = []
            for r in range(table.rowCount()):
                item = table.item(r, 0)
                if item is None:
                    continue
                index = item.data(ROW_INDEX)
                if index is not None and index < len(visible):
                    out.append(visible[index])
            return out

        search.textChanged.connect(repopulate)
        band_filter.currentIndexChanged.connect(repopulate)
        hide_suspended.stateChanged.connect(repopulate)

        def open_member(item):
            ind_id = item.data(Qt.ItemDataRole.UserRole)
            details = self.db.get_individual(ind_id) or {}
            if not details:
                return
            dialog.accept()
            self.open_ledger_by_id(ind_id, details.get("name", ""))

        table.itemDoubleClicked.connect(open_member)

        buttons = QHBoxLayout()
        export_btn = QPushButton("Print / Export…")
        export_btn.setStyleSheet(self.theme_manager.button_css("accent"))
        export_btn.setToolTip("Save the list shown here as PDF, Excel or CSV")
        export_btn.clicked.connect(
            lambda: self._export_arrears(dialog, rows_in_view(), as_of))
        buttons.addWidget(export_btn)
        buttons.addStretch()
        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(dialog.reject)
        buttons.addWidget(close_box)
        layout.addLayout(buttons)

        repopulate()
        dialog.exec()

    def _export_arrears(self, parent, rows, as_of):
        """Save the arrears list the user is looking at, and open it."""
        from src.reports import ReportGenerator

        if not rows:
            QMessageBox.information(parent, "Print / Export",
                                    "There is nothing in the list to print.")
            return

        path, _ = QFileDialog.getSaveFileName(
            parent, "Save Arrears List", f"Loans_In_Arrears_{as_of}.pdf",
            "PDF Files (*.pdf);;Excel Files (*.xlsx);;CSV Files (*.csv)")
        if not path:
            return

        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)
        progress = QProgressDialog("Preparing the arrears list…", None, 0, 0, parent)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        QApplication.processEvents()
        try:
            success, message = generator.generate_arrears_report(path, rows, as_of=as_of)
        finally:
            progress.close()

        if success:
            from .file_actions import report_saved
            report_saved(parent, "Arrears List", path, what="arrears list")
        else:
            QMessageBox.critical(parent, "Arrears List", f"Export failed:\n{message}")

    def refresh_list(self, select_id=None):
        # Keep the portfolio totals in step with every add/edit/delete/import.
        self.refresh_summary()
        # Clear existing items
        self.card_widgets = []
        self.selected_card = None

        # Remove the previous cards, but keep the empty-state label in place.
        for i in reversed(range(self.scroll_content_layout.count())):
            widget = self.scroll_content_layout.itemAt(i).widget()
            if widget is not None and widget is not self.empty_label:
                self.scroll_content_layout.takeAt(i)
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

        # Fresh install / everyone deleted: guide the user instead of a blank panel.
        if individuals:
            self.empty_label.setVisible(False)
        else:
            self.empty_label.setText(
                "No members yet.\nClick “+ New Individual” above to add one.")
            self.empty_label.setVisible(True)

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
        # Tell the user when a search matches nobody, rather than showing blank.
        if self.card_widgets and not self._ui_state.get_visible_cards():
            self.empty_label.setText(f"No members match “{text}”.")
            self.empty_label.setVisible(True)
        elif self.card_widgets:
            self.empty_label.setVisible(False)
        
    def _on_selection_changed(self, card):
        """Callback when selection changes via UIStateManager."""
        self.selected_card = card  # Sync legacy attribute

    def select_individual(self, card):
        """Select a card using UIStateManager."""
        self._ui_state.select(card)
        # Legacy sync is handled via _on_selection_changed callback

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
                msg += f"\u2022 Savings balance of {format_money(savings_bal)} will be auto-withdrawn\n"
            if has_loans:
                msg += "\u2022 Outstanding loans will remain active until cleared\n"
                msg += "\u2022 They will still appear in Loan Reports\n"
            msg += "\n\u2022 They will be excluded from Savings/Shares Reports\n"
            msg += "\u2022 They will be excluded from Mass Savings operations"
            
            confirm = QMessageBox.question(
                self, "Retire Individual", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                result = self.engine.retire_individual(ind_id)
                withdrawn = result.get('savings_withdrawn', 0)
                
                msg = f"'{name}' has been retired."
                if withdrawn > 0:
                    msg += f"\nSavings withdrawal of {format_money(withdrawn)} was created."
                
                self.refresh_list(select_id=ind_id)
                QMessageBox.information(self, "Retired", msg)
    
    @staticmethod
    def _format_names(names, cap=10):
        """Join names for a summary, capping a long list so the dialog stays readable."""
        names = list(names)
        if len(names) <= cap:
            return ", ".join(names)
        return ", ".join(names[:cap]) + f", and {len(names) - cap} more"

    def _show_batch_summary(self, dest, saved, html_fallbacks=(), skipped_empty=(), failed=()):
        """Report a batch statement run: what saved, what was skipped, what failed.

        Replaces a bare "Saved N" that hid every statement which errored, had no
        activity, or fell back to HTML.
        """
        total_saved = len(saved) + len(html_fallbacks)
        lines = [f"Saved {total_saved} statement(s) to:\n{dest}"]
        if html_fallbacks:
            lines.append(f"\n{len(html_fallbacks)} saved as HTML (PDF unavailable): "
                         + self._format_names(html_fallbacks))
        if skipped_empty:
            lines.append(f"\n{len(skipped_empty)} skipped — no activity in this period: "
                         + self._format_names(skipped_empty))
        if failed:
            lines.append(f"\n{len(failed)} failed — see the log: "
                         + self._format_names(failed))
        text = "\n".join(lines)
        if failed:
            QMessageBox.warning(self, "Batch complete with problems", text)
        elif total_saved == 0:
            QMessageBox.warning(self, "Nothing saved", text)
        else:
            QMessageBox.information(self, "Batch complete", text)

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
        
        saved, html_fallbacks, skipped_empty, failed = [], [], [], []
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

            if success and fmt == "html":
                html_fallbacks.append(ind[1])
            elif success:
                saved.append(ind[1])
            elif fmt == "empty":
                skipped_empty.append(ind[1])
            else:
                failed.append(ind[1])
            progress.setValue(i + 1)
            QApplication.processEvents() # Ensure UI remains responsive

        progress.close()
        self._show_batch_summary(folder, saved, html_fallbacks, skipped_empty, failed)
    
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
            
            saved, html_fallbacks, skipped_empty, failed = [], [], [], []

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
                    
                    if self.print_mode == "pdf":
                        # Updated signature returns (success, path, format)
                        res = self._statement_generator.generate_pdf_statement(ind_id, name, folder, from_str, to_str, config=config)
                        success, fmt = (res[0], res[2]) if isinstance(res, tuple) else (res, "pdf")
                        if success and fmt == "html":
                            html_fallbacks.append(name)
                        elif success:
                            saved.append(name)
                        elif fmt == "empty":
                            skipped_empty.append(name)
                        else:
                            failed.append(name)
                    elif self.print_mode == "excel":
                        if self._statement_generator.generate_excel_statement(ind_id, name, folder, from_str, to_str, config=config):
                            saved.append(name)
                        else:
                            failed.append(name)

                    progress.setValue(i + 1)
                    QApplication.processEvents()

                progress.close()

            if saved or html_fallbacks or skipped_empty or failed:
                self._show_batch_summary(folder, saved, html_fallbacks, skipped_empty, failed)
            else:
                QMessageBox.warning(self, "Warning", "No statements saved.")

    
    def open_fund_grid(self):
        """Open the members × months spreadsheet over the three funds.

        A second way into the same data, not a second copy of it: the grid
        writes through the same posting pipeline the member screens use, so
        nothing here depends on it and nothing it does is invisible to them.
        """
        from .fund_grid import FundGridWindow

        window = FundGridWindow(self, self.db, self.theme_manager,
                                on_open_member=self.open_ledger_by_id)
        # The grid's export offers PDF, which needs the hidden web view the
        # report exports already share.
        window.printer_view_getter = self.get_printer_view
        window.exec()
        # Fund edits move the savings total on the KPI band.
        self.refresh_summary()

    def get_printer_view(self):
        """Get or create the hidden QWebEngineView for printing."""
        if self.printer_view is None:
            try:
                from PyQt6.QtWebEngineWidgets import QWebEngineView
                self.printer_view = QWebEngineView()
                self.printer_view.hide()
            except ImportError:
                logger.warning("QtWebEngineWidgets not found. PDF printing not available.")
                return None
        return self.printer_view

    def open_settings(self):
        """Open settings dialog (Organization / Financial / Appearance)."""
        from PyQt6.QtWidgets import (QComboBox, QDialogButtonBox, QDoubleSpinBox,
                                     QGroupBox)
        from src import branding
        dialog = QDialog(self)
        dialog.setWindowTitle("Settings")
        dialog.setMinimumWidth(480)
        outer = QVBoxLayout(dialog)

        # --- Organization (branding used on every statement and report) ---
        org_box = QGroupBox("Organization")
        org_form = QFormLayout(org_box)
        org_name_edit = QLineEdit(self.db.settings.org_name)
        org_name_edit.setPlaceholderText("e.g. Umoja Staff Co-operative")
        org_form.addRow("Organization Name:", org_name_edit)

        logo_row = QHBoxLayout()
        logo_edit = QLineEdit(self.db.settings.org_logo_path)
        logo_edit.setPlaceholderText("Bundled default logo")
        logo_edit.setReadOnly(True)
        browse_btn = QPushButton("Browse…")
        clear_btn = QPushButton("Clear")

        def pick_logo():
            path, _ = QFileDialog.getOpenFileName(
                dialog, "Choose Logo", "",
                "Images (*.png *.jpg *.jpeg *.gif *.svg)")
            if path:
                logo_edit.setText(path)

        browse_btn.clicked.connect(pick_logo)
        clear_btn.clicked.connect(lambda: logo_edit.setText(""))
        logo_row.addWidget(logo_edit); logo_row.addWidget(browse_btn); logo_row.addWidget(clear_btn)
        org_form.addRow("Logo:", logo_row)
        org_form.addRow(QLabel("<small>Shown on statements, reports and exports. "
                               "Leave blank to use the bundled logo.</small>"))
        outer.addWidget(org_box)

        # --- Financial ---
        fin_box = QGroupBox("Financial")
        fin_form = QFormLayout(fin_box)
        fy_combo = QComboBox()
        months = ["January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December"]
        fy_combo.addItems(months)
        current_fy_str = self.db.settings.fy_start_month
        if current_fy_str in months:
            fy_combo.setCurrentText(current_fy_str)
        fin_form.addRow("Financial Year Start:", fy_combo)

        interest_rate = QDoubleSpinBox()
        interest_rate.setRange(0, 100)
        interest_rate.setDecimals(2)
        interest_rate.setSuffix(" %")
        interest_rate.setValue(self.db.settings.default_interest_rate * 100)
        interest_rate.setToolTip("Flat rate used to gross-up a loan's total "
                                 "obligation on the ledger and statements.")
        fin_form.addRow("Loan Interest Rate:", interest_rate)

        deduct_cb = QCheckBox("Allow Deduction in Same Month of Issue")
        deduct_cb.setChecked(self.db.settings.deduct_same_month)
        fin_form.addRow(deduct_cb)
        outer.addWidget(fin_box)

        # --- Funds ---
        # These three were readable by the code but had no UI, so the only way
        # to change them was to edit the settings table by hand.
        fund_box = QGroupBox("Funds")
        fund_form = QFormLayout(fund_box)

        unlock_combo = QComboBox()
        unlock_combo.addItems(months)
        unlock_idx = self.db.settings.christmas_unlock_month
        if 1 <= unlock_idx <= 12:
            unlock_combo.setCurrentIndex(unlock_idx - 1)
        fund_form.addRow("Christmas Withdrawal Month:", unlock_combo)

        savings_inc = QDoubleSpinBox()
        savings_inc.setRange(0, 10_000_000)
        savings_inc.setDecimals(2)
        savings_inc.setGroupSeparatorShown(True)
        savings_inc.setValue(self.db.settings.default_savings_increment)
        fund_form.addRow("Default Monthly Savings:", savings_inc)

        xmas_inc = QDoubleSpinBox()
        xmas_inc.setRange(0, 10_000_000)
        xmas_inc.setDecimals(2)
        xmas_inc.setGroupSeparatorShown(True)
        xmas_inc.setValue(self.db.settings.default_christmas_increment)
        fund_form.addRow("Default Monthly Christmas:", xmas_inc)

        fund_form.addRow(QLabel("<small>Defaults used when a member has no "
                                "contribution history to infer an amount from.</small>"))
        outer.addWidget(fund_box)

        # --- Appearance ---
        app_box = QGroupBox("Appearance")
        app_form = QFormLayout(app_box)
        theme_combo = QComboBox()
        theme_combo.addItems(["Light", "Dark"])
        theme_combo.setCurrentText(self.theme_manager.current_theme_name)
        app_form.addRow("Application Theme:", theme_combo)
        outer.addWidget(app_box)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        outer.addWidget(btns)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.db.set_setting(branding.ORG_NAME_KEY, org_name_edit.text().strip())
            self.db.set_setting(branding.ORG_LOGO_KEY, logo_edit.text().strip())
            self.db.set_setting("fy_start_month", fy_combo.currentText())
            self.db.set_setting("default_interest_rate", interest_rate.value() / 100)
            self.db.set_setting("deduct_same_month",
                                "true" if deduct_cb.isChecked() else "false")
            self.db.set_setting("christmas_unlock_month", unlock_combo.currentIndex() + 1)
            self.db.set_setting("default_savings_increment", savings_inc.value())
            self.db.set_setting("default_christmas_increment", xmas_inc.value())

            # Reflect the organization name in the window title immediately.
            main_win = self.window()
            if hasattr(main_win, "refresh_window_title"):
                main_win.refresh_window_title()

            selected_theme = theme_combo.currentText()
            if selected_theme != self.theme_manager.current_theme_name:
                self.theme_manager.set_theme(selected_theme)
                self.apply_theme()
                # The ledger view holds its own ThemeManager instance; hand it
                # the updated one so it restyles without an app restart.
                if hasattr(main_win, "ledger_view"):
                    main_win.ledger_view.theme_manager = self.theme_manager
                    main_win.ledger_view.apply_theme()
                # Re-theme dialogs (application-level stylesheet) to match.
                if hasattr(main_win, "apply_app_theme"):
                    main_win.apply_app_theme()
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
        run_btn.setStyleSheet(self.theme_manager.button_css("danger", padding="10px", bold=True))
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
                                     QFileDialog, QComboBox, QPushButton, QRadioButton, QDateEdit,
                                     QListWidget, QListWidgetItem, QGroupBox, QHBoxLayout)
        from PyQt6.QtCore import QDate, Qt
        from dateutil.relativedelta import relativedelta
        from datetime import datetime

        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"{label} Report")
        dialog.setMinimumWidth(420)
        dialog.setMinimumHeight(450)
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

        # Columns selection
        col_list = QListWidget()
        col_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        col_list.setFixedHeight(150)
        
        user_states = {}
        
        def get_expected_columns():
            custom = rb_custom.isChecked()
            if fund == "savings" and not custom:
                start_str = quarter_combo.currentData()
                if not start_str:
                    return []
                start_dt = datetime.strptime(start_str, "%Y-%m-%d")
                m1_start = start_dt
                m2_start = start_dt + relativedelta(months=1)
                m3_start = start_dt + relativedelta(months=2)
                
                bf_date_obj = m1_start + relativedelta(days=-1)
                day = bf_date_obj.day
                suffix = "TH" if 11 <= day <= 13 else {1:"ST",2:"ND",3:"RD"}.get(day%10, "TH")
                bf_label = f"B/F {day}{suffix} {bf_date_obj.strftime('%b %Y')}".upper()
                
                return ["Name", bf_label, m1_start.strftime("%b-%y"), m2_start.strftime("%b-%y"), m3_start.strftime("%b-%y"), "Sub Total", "Cash Out", "Grand Total"]
            else:
                start = from_date.date()
                end = to_date.date()
                start_dt = datetime(start.year(), start.month(), start.day())
                end_dt = datetime(end.year(), end.month(), end.day())
                if end_dt < start_dt:
                    return ["PF No", "Name", "Employment Status", "Total"]
                
                cur = start_dt.replace(day=1)
                last = end_dt.replace(day=1)
                months = []
                while cur <= last:
                    months.append(cur.strftime("%b-%y"))
                    cur = cur + relativedelta(months=1)
                return ["PF No", "Name", "Employment Status"] + months + ["Total"]

        def update_columns_list():
            current_cols = []
            for i in range(col_list.count()):
                item = col_list.item(i)
                current_cols.append(item.text())
                user_states[item.text()] = (item.checkState() == Qt.CheckState.Checked)
            
            col_list.clear()
            expected = get_expected_columns()
            
            existing_expected = [c for c in current_cols if c in expected]
            new_expected = [c for c in expected if c not in current_cols]
            sorted_expected = existing_expected + new_expected
            
            for col in sorted_expected:
                item = QListWidgetItem(col)
                checked = user_states.get(col, True)
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                col_list.addItem(item)

        rb_quarter.toggled.connect(update_columns_list)
        rb_custom.toggled.connect(update_columns_list)
        quarter_combo.currentIndexChanged.connect(update_columns_list)
        from_date.dateChanged.connect(update_columns_list)
        to_date.dateChanged.connect(update_columns_list)
        
        update_columns_list()

        col_group = QGroupBox("Columns to Include & Order")
        col_layout = QHBoxLayout()
        col_layout.addWidget(col_list)
        
        btns_layout = QVBoxLayout()
        up_btn = QPushButton("▲ Up")
        down_btn = QPushButton("▼ Down")
        up_btn.setFixedWidth(60)
        down_btn.setFixedWidth(60)
        btns_layout.addWidget(up_btn)
        btns_layout.addWidget(down_btn)
        btns_layout.addStretch()
        col_layout.addLayout(btns_layout)
        col_group.setLayout(col_layout)
        
        layout.addWidget(col_group)
        
        def move(delta):
            r = col_list.currentRow()
            nr = r + delta
            if r < 0 or nr < 0 or nr >= col_list.count():
                return
            col_list.insertItem(nr, col_list.takeItem(r))
            col_list.setCurrentRow(nr)
        up_btn.clicked.connect(lambda: move(-1))
        down_btn.clicked.connect(lambda: move(1))

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

        chosen_columns = [col_list.item(i).text()
                          for i in range(col_list.count())
                          if col_list.item(i).checkState() == Qt.CheckState.Checked]

        # Generate off the GUI thread with progress + safe cancel, instead of
        # freezing the window until the file is written.
        db_name = self.db.db_name if hasattr(self.db, 'db_name') else "loan_master.db"
        if fund == "savings" and not custom:
            worker = ReportWorker(db_name, self.get_printer_view, start, path,
                                  report_type='savings', columns=chosen_columns)
        else:
            worker = ReportWorker(db_name, self.get_printer_view, start, path,
                                  report_type='fund', columns=chosen_columns,
                                  end_date=end, fund=fund)
        self._run_report_worker(worker, path)

    def _run_report_worker(self, worker, file_path):
        """Drive a ReportWorker with a modal progress dialog and safe cancel.

        Shared by the loan and fund/savings report flows so each generates off
        the GUI thread with real progress and cooperative cancellation instead
        of freezing the window. On success the saved file is offered for opening
        via report_saved.
        """
        progress = QProgressDialog("Generating Report...", "Cancel", 0, 100, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        self.report_worker = worker

        def on_progress(curr, total, msg):
            progress.setMaximum(total)
            progress.setValue(curr)
            progress.setLabelText(msg)
            if progress.wasCanceled() and getattr(self, 'report_worker', None):
                self.report_worker.cancel()

        def on_finished(success, message):
            progress.close()
            if success:
                from .file_actions import report_saved
                report_saved(self, "Success", file_path)
            elif message == "Cancelled":
                pass  # user asked to stop; nothing to report
            else:
                QMessageBox.critical(self, "Error", f"Failed to generate report:\n{message}")
            self.report_worker = None

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.start()

    def generate_quarterly_report(self):
        """Generate quarterly loan interest report."""
        from ..reports import ReportGenerator
        from PyQt6.QtWidgets import (QDialog, QFormLayout, QDialogButtonBox, QFileDialog,
                                     QComboBox, QPushButton, QListWidget, QListWidgetItem, QGroupBox,
                                     QHBoxLayout, QVBoxLayout)
        from PyQt6.QtCore import Qt
        from dateutil.relativedelta import relativedelta
        from datetime import datetime
        
        # Determine Default Date based on FY Setting
        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)
        def_dt = generator.get_default_quarter_date()

        # Dialog to select start date
        dialog = QDialog(self)
        dialog.setWindowTitle("Quarterly Loan Report Period")
        dialog.setMinimumWidth(420)
        dialog.setMinimumHeight(420)
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

        # Columns selection
        col_list = QListWidget()
        col_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        col_list.setFixedHeight(150)
        
        user_states = {}
        
        def get_expected_columns():
            start_str = quarter_combo.currentData()
            if not start_str:
                return []
            start_dt = datetime.strptime(start_str, "%Y-%m-%d")
            m1_start = start_dt
            m2_start = start_dt + relativedelta(months=1)
            m3_start = start_dt + relativedelta(months=2)
            
            bf_date_label = f"B/F at {m1_start.strftime('%b %Y')}"
            m1_name = m1_start.strftime("%b-%y")
            m2_name = m2_start.strftime("%b-%y")
            m3_name = m3_start.strftime("%b-%y")
            
            return ["Name", "Loan Amount", bf_date_label, m1_name, m2_name, m3_name, "Sub Total", "Grand Total"]

        def update_columns_list():
            current_cols = []
            for i in range(col_list.count()):
                item = col_list.item(i)
                current_cols.append(item.text())
                user_states[item.text()] = (item.checkState() == Qt.CheckState.Checked)
            
            col_list.clear()
            expected = get_expected_columns()
            
            existing_expected = [c for c in current_cols if c in expected]
            new_expected = [c for c in expected if c not in current_cols]
            sorted_expected = existing_expected + new_expected
            
            for col in sorted_expected:
                item = QListWidgetItem(col)
                checked = user_states.get(col, True)
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                col_list.addItem(item)
                
        quarter_combo.currentIndexChanged.connect(update_columns_list)
        update_columns_list()

        col_group = QGroupBox("Columns to Include & Order")
        col_layout = QHBoxLayout()
        col_layout.addWidget(col_list)
        
        btns_layout = QVBoxLayout()
        up_btn = QPushButton("▲ Up")
        down_btn = QPushButton("▼ Down")
        up_btn.setFixedWidth(60)
        down_btn.setFixedWidth(60)
        btns_layout.addWidget(up_btn)
        btns_layout.addWidget(down_btn)
        btns_layout.addStretch()
        col_layout.addLayout(btns_layout)
        col_group.setLayout(col_layout)
        
        layout.addRow(col_group)
        
        def move(delta):
            r = col_list.currentRow()
            nr = r + delta
            if r < 0 or nr < 0 or nr >= col_list.count():
                return
            col_list.insertItem(nr, col_list.takeItem(r))
            col_list.setCurrentRow(nr)
        up_btn.clicked.connect(lambda: move(-1))
        down_btn.clicked.connect(lambda: move(1))
        
        # Format Settings Button
        fmt_btn = QPushButton("Format Settings...")
        fmt_btn.setStyleSheet(self.theme_manager.outline_button_css("accent", padding="4px"))
        
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
            
            chosen_columns = [col_list.item(i).text()
                              for i in range(col_list.count())
                              if col_list.item(i).checkState() == Qt.CheckState.Checked]
            
            # File save dialog
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Report", 
                f"Quarterly_Report_{start_date}.xlsx", 
                "Excel Files (*.xlsx);;CSV Files (*.csv);;PDF Files (*.pdf)"
            )
            
            if file_path:
                # Generate off the GUI thread with progress + safe cancel.
                db_name = self.db.db_name if hasattr(self.db, 'db_name') else "loan_master.db"
                worker = ReportWorker(db_name, self.get_printer_view, start_date,
                                      file_path, report_type='loan',
                                      columns=chosen_columns)
                self._run_report_worker(worker, file_path)

    def open_mass_savings_dialog(self):
        """Open dialog for Mass Savings Increment."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Mass Savings Increment (Catch Up)")
        dialog.setMinimumWidth(500)
        dialog.setMinimumHeight(400)
        layout = QVBoxLayout(dialog)
        
        layout.addWidget(QLabel("<b>Select individuals to increment savings:</b>"))
        layout.addWidget(QLabel("Checked individuals will be processed to catch up savings contributions."))
        default_amount = self.db.settings.default_savings_increment
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
                cb.setText(label_text + f" (Bal: {format_money(bal)}) [Auto: {format_money(auto_amt)}]")
            else:
                 # Even if bal is 0, show what auto would correspond to (likely 2500)
                 cb.setText(label_text + f" [Auto: {format_money(auto_amt)}]")
            
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
        run_btn.setStyleSheet(self.theme_manager.button_css("success", padding="10px", bold=True))
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
        self.refresh_list()

    def import_from_excel(self):
        """Import members / fund contributions from an .xlsx file."""
        dlg = ExcelImportDialog(self.db, self)
        if dlg.exec():
            self.refresh_list()

    def export_members_list(self):
        """Export a members directory to Excel/PDF/CSV with user-chosen columns."""
        from ..reports import ReportGenerator
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialogButtonBox, QFileDialog,
                                     QProgressDialog)

        generator = ReportGenerator(self.db, printer_view_getter=self.get_printer_view)
        default_on = {"name", "pf_no", "id_no", "phone", "employment_status"}

        dlg = QDialog(self)
        dlg.setWindowTitle("Members List — choose & order columns")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Tick the columns to include. Drag, or use ▲ / ▼, to set their order:"))

        col_list = QListWidget()
        col_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        for key, label in ReportGenerator.MEMBER_LIST_COLUMNS:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in default_on else Qt.CheckState.Unchecked)
            col_list.addItem(item)

        row = QHBoxLayout()
        row.addWidget(col_list)
        side = QVBoxLayout()
        up_btn = QPushButton("▲ Up")
        down_btn = QPushButton("▼ Down")
        side.addWidget(up_btn)
        side.addWidget(down_btn)
        side.addStretch()
        row.addLayout(side)
        layout.addLayout(row)

        def move(delta):
            r = col_list.currentRow()
            nr = r + delta
            if r < 0 or nr < 0 or nr >= col_list.count():
                return
            col_list.insertItem(nr, col_list.takeItem(r))  # preserves check state
            col_list.setCurrentRow(nr)
        up_btn.clicked.connect(lambda: move(-1))
        down_btn.clicked.connect(lambda: move(1))

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Export…")
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # Columns in the user's chosen order (checked items only).
        columns = [col_list.item(i).data(Qt.ItemDataRole.UserRole)
                   for i in range(col_list.count())
                   if col_list.item(i).checkState() == Qt.CheckState.Checked]
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Members List", "Members_List.xlsx",
            "Excel Files (*.xlsx);;PDF Files (*.pdf);;CSV Files (*.csv)")
        if not path:
            return

        progress = QProgressDialog("Generating members list…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        QApplication.processEvents()
        success, msg = generator.generate_members_list(path, columns)
        progress.close()

        if success:
            from .file_actions import report_saved
            report_saved(self, "Members List", path, what="members list")
        else:
            QMessageBox.critical(self, "Members List", f"Export failed:\n{msg}")

    @staticmethod
    def _format_before_image(raw):
        """Render an audit before-image as a short, readable line.

        Stored as JSON by the triggers so it survives any schema change; shown
        as `date 2026-01-15, deducted 5,750` rather than raw JSON, with the
        full record on the tooltip.
        """
        if not raw:
            return ""
        import json
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return str(raw)
        skip = {"individual_id"}
        parts = []
        for key, value in data.items():
            if key in skip or value in (None, 0, 0.0, ""):
                continue
            if isinstance(value, float):
                value = f"{value:,.2f}"
            parts.append(f"{key.replace('_', ' ')} {value}")
        return ", ".join(parts)

    def show_health_check(self):
        """Open the full integrity report."""
        from .integrity_report import IntegrityReportDialog
        IntegrityReportDialog(self.db, self.theme_manager, self).exec()

    def open_restore_dialog(self):
        """Restore the journal from a backup, refreshing everything after."""
        from .restore_backup import RestoreBackupDialog
        RestoreBackupDialog(self.db, self.theme_manager, self,
                            on_restored=self._after_restore).exec()

    def _after_restore(self):
        """Every cached figure on screen is now stale — reload from the journal."""
        try:
            self.refresh_list()
        except Exception:                       # noqa: BLE001 - never leave a dead screen
            logger.exception("Could not refresh the dashboard after a restore")
            QMessageBox.information(
                self, "Restored",
                "The journal was restored. Please close and reopen the "
                "application so every screen reloads.")

    def show_activity_log(self):
        """View the CRUD audit trail (member/loan create/update/delete with dates)."""
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
                                     QHeaderView, QDialogButtonBox)
        entries = self.db.get_audit_log(limit=1000)
        dlg = QDialog(self)
        dlg.setWindowTitle("Activity Log")
        dlg.setMinimumSize(680, 500)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(f"Most recent {len(entries)} create/update/delete events:"))
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(
            ["When", "Operation", "Type", "Who / What", "Previous values"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            table.setItem(i, 0, QTableWidgetItem(str(e["ts"])))
            table.setItem(i, 1, QTableWidgetItem(e["operation"]))
            table.setItem(i, 2, QTableWidgetItem(e["entity"]))
            table.setItem(i, 3, QTableWidgetItem(str(e["summary"] or "")))
            # The before-image recorded by the migration-007 triggers. Without
            # it the log answers "this was changed" but never "from what?",
            # which is the only question worth asking about a money row.
            before = QTableWidgetItem(self._format_before_image(e.get("before_image")))
            before.setToolTip(str(e.get("before_image") or ""))
            table.setItem(i, 4, before)
        layout.addWidget(table)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
        layout.addWidget(bb)
        dlg.exec()

    def open_repair_loans_dialog(self):
        """Scan for legacy loans with stale terms / contaminated accruals and heal them."""
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
                                     QHeaderView, QDialogButtonBox, QProgressDialog, QMessageBox)
        from src import migrations

        if migrations.schema_version(self.db.conn) < 3:
            QMessageBox.warning(
                self, "Migration Required",
                "Database migrations have not completed. Please run the Orphan Repair tool "
                "first to resolve legacy data issues before repairing loan terms."
            )
            return

        try:
            healable = self.engine.scan_healable_loans()
        except Exception as e:
            from src.views.error_handling import report
            report(e, parent=self, context="scanning healable loans")
            return

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
                    self.db.snapshot("repair-loans")
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
            self.refresh_list()
            dlg.accept()

        repair_btn.clicked.connect(do_repair)
        dlg.exec()

    def open_orphan_repair_dialog(self):
        """Review and clean up records left behind by deleted members.

        Legacy journals (written before foreign keys were enforced) can hold
        ledger/savings/loan rows whose member no longer exists. They skew
        report totals and keep foreign-key protection disabled. The dialog
        shows every orphan group, offers an Excel audit export, and deletes
        them only after an explicit confirmation and a fresh backup.
        """
        from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTableWidget,
                                     QHeaderView, QDialogButtonBox)
        from datetime import datetime

        orphans = self.db.find_orphaned_rows()
        dlg = QDialog(self)
        dlg.setWindowTitle("Repair Orphaned Records")
        dlg.setMinimumSize(640, 460)
        layout = QVBoxLayout(dlg)

        if not orphans:
            layout.addWidget(QLabel(
                "✓ No orphaned records — every transaction belongs to an "
                "existing member.\n\nForeign-key protection is active, so new "
                "orphans cannot be created."))
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
            layout.addWidget(bb)
            dlg.exec()
            return

        total_rows = sum(o["rows"] for o in orphans)
        layout.addWidget(QLabel(
            f"<b>{total_rows} record(s)</b> reference members that were deleted "
            "before database protection was enabled. They can distort report "
            "totals and keep foreign-key protection switched off.<br><br>"
            "Recommended: <b>export an audit copy first</b>, then delete them. "
            "A backup of the journal is taken automatically before deleting."))

        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["Table", "Deleted Member ID", "Rows",
                                         "First Date", "Last Date"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setRowCount(len(orphans))
        for i, o in enumerate(orphans):
            table.setItem(i, 0, QTableWidgetItem(o["table"]))
            table.setItem(i, 1, QTableWidgetItem(str(o["individual_id"])))
            table.setItem(i, 2, QTableWidgetItem(str(o["rows"])))
            table.setItem(i, 3, QTableWidgetItem(str(o["first_date"] or "—")))
            table.setItem(i, 4, QTableWidgetItem(str(o["last_date"] or "—")))
        layout.addWidget(table)

        bb = QDialogButtonBox()
        export_btn = bb.addButton("Export Audit Copy…", QDialogButtonBox.ButtonRole.ActionRole)
        delete_btn = bb.addButton(f"Delete All ({total_rows})", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.rejected.connect(dlg.reject)
        layout.addWidget(bb)

        def do_export():
            default = f"orphaned_records_{datetime.now().strftime('%Y%m%d')}.xlsx"
            path, _ = QFileDialog.getSaveFileName(dlg, "Export Audit Copy",
                                                  default, "Excel Files (*.xlsx)")
            if not path:
                return
            try:
                n = self.db.export_orphaned_rows(path)
                QMessageBox.information(dlg, "Export Complete",
                                        f"Exported {n} record(s) to:\n{path}")
            except Exception as e:
                QMessageBox.critical(dlg, "Export Failed", f"Could not export: {e}")

        def do_delete():
            if QMessageBox.question(
                    dlg, "Confirm Delete",
                    f"Permanently delete {total_rows} orphaned record(s)?\n\n"
                    "The journal will be backed up first. This cannot be "
                    "undone from within the app.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            src = getattr(self.db, "db_name", None)
            if src:
                try:
                    self.db.snapshot("orphan-clean")
                except Exception as e:
                    QMessageBox.warning(dlg, "Backup", f"Could not back up DB ({e}). Aborted.")
                    return
            try:
                counts = self.db.delete_orphaned_rows()
            except Exception as e:
                QMessageBox.critical(dlg, "Delete Failed",
                                     f"Nothing was deleted (transaction rolled back): {e}")
                return
            detail = "\n".join(f"  {t}: {n}" for t, n in counts.items())
            fk_on = self.db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
            fk_msg = ("\n\nForeign-key protection is now ACTIVE — future "
                      "member deletions cannot leave orphans behind."
                      if fk_on else "")
            QMessageBox.information(dlg, "Repair Complete",
                                    f"Deleted rows:\n{detail}{fk_msg}")
            self.refresh_list()
            dlg.accept()

        export_btn.clicked.connect(do_export)
        delete_btn.clicked.connect(do_delete)
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
