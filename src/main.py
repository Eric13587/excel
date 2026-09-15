"""Main application window for LoanMaster."""
import logging
import sys
import os

# Disable GPU and software rasterizer to prevent crashes on older hardware (DirectX 11 issues)
# This is critical for systems where QtWebEngine fails to initialize GPU process.
# Must be set before QWebEngineWidgets is imported/initialized.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-software-rasterizer --disable-gpu-compositing"
os.environ.setdefault("QT_QUICK_BACKEND", "software")

# Import QtWebEngineWidgets BEFORE QApplication is created (needed for PDF generation)
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 — import for side effect
except ImportError:
    pass  # Not installed, PDF generation will fall back to HTML

from PyQt6.QtWidgets import (QApplication, QMainWindow, QStackedWidget, QMessageBox, 
                             QDialog, QVBoxLayout, QPushButton, QLabel, QFileDialog)
from PyQt6.QtGui import QIcon, QAction, QKeySequence
from PyQt6.QtCore import Qt, QDate

from . import __version__
from .database import DatabaseManager
from .logging_setup import (setup_logging, install_crash_handler,
                            install_qt_message_handler, get_log_dir)
from .views.dashboard import Dashboard
from .views.ledger import LedgerView

logger = logging.getLogger(__name__)


class StartupDialog(QDialog):
    """Dialog to select or create a database file."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoanMaster - Select Journal")
        self.setFixedSize(420, 240)
        self.selected_db = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(30, 25, 30, 25)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("LoanMaster")
        title.setStyleSheet("font-size: 22px; font-weight: bold; color: #1F2937;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(f"Loan, savings & fund management — v{__version__}")
        subtitle.setStyleSheet("font-size: 11px; color: #6B7280;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(8)

        btn_open = QPushButton("Open Existing Journal")
        btn_open.setMinimumHeight(42)
        btn_open.setStyleSheet(
            "QPushButton { background-color: #2563EB; color: white; border-radius: 6px; "
            "font-weight: bold; } QPushButton:hover { background-color: #1D4ED8; }")
        btn_open.clicked.connect(self.open_existing)
        btn_open.setDefault(True)
        layout.addWidget(btn_open)

        btn_new = QPushButton("Create New Journal")
        btn_new.setMinimumHeight(42)
        btn_new.setStyleSheet(
            "QPushButton { background-color: #F3F4F6; color: #1F2937; border: 1px solid #E5E7EB; "
            "border-radius: 6px; } QPushButton:hover { border-color: #2563EB; }")
        btn_new.clicked.connect(self.create_new)
        layout.addWidget(btn_new)

    def create_new(self):
        path, _ = QFileDialog.getSaveFileName(self, "Create New Journal", "my_journal.db", "Database Files (*.db)")
        if path:
            if not path.endswith('.db'):
                path += '.db'
            self.selected_db = path
            self.accept()

    def open_existing(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Existing Journal", "", "Database Files (*.db)")
        if path:
            self.selected_db = path
            self.accept()


class MainApp(QMainWindow):
    """Main application window."""
    
    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.resize(1100, 700) # Slightly larger default size

        # Set Application Icon
        if getattr(sys, 'frozen', False):
             base_path = os.path.dirname(sys.executable)
        else:
             base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
             
        icon_path = os.path.join(base_path, "resources", "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        # auto_backup snapshots the journal into backups/ before any schema
        # migration runs, keeping the last few copies.
        self.db = DatabaseManager(db_path, auto_backup=True)
        if not self.db.integrity_ok:
            answer = QMessageBox.warning(
                self, "Database Warning",
                "The integrity check on this journal reported problems.\n\n"
                "The journal was still opened so you can export your data, "
                "but you should restore a recent backup from the 'backups' "
                "folder next to the journal file as soon as possible.\n\n"
                "Would you like to see what was found?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer == QMessageBox.StandardButton.Yes:
                self.show_health_check()
        # A migration that could not be applied leaves the journal on an older
        # schema — usable, but without cascading deletes or the new indexes.
        # Silent degradation is exactly what we must not do with a financial
        # journal, so say so and name the remedy.
        if not self.db.migration_report.ok:
            QMessageBox.warning(
                self, "Schema Update Incomplete",
                f"This journal could not be brought fully up to date:\n\n"
                f"{self.db.migration_report.summary()}\n\n"
                "It has been opened on its existing schema and is safe to use, "
                "but some integrity protections are not active.\n\n"
                "Mass Operations \u2192 Journal Health Check shows the details, and "
                "the Repair tools there can fix most causes.")

        # Persistent date defaults
        self.last_operation_date = QDate.currentDate()
        self.last_report_range = (QDate.currentDate().addMonths(-12), QDate.currentDate())
        
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.dashboard = Dashboard(self, self.db)
        self.ledger_view = LedgerView(self, self.db)
        
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.ledger_view)

        # Theme dialogs at the application level (they have no ThemeManager of
        # their own). Reuses the dashboard's manager so all three agree.
        self.apply_app_theme()

        self.show_dashboard()

        self.create_menus()
        self._build_status_bar()
        self.refresh_window_title()

    def _build_status_bar(self):
        """A status bar gives setStatusTip somewhere to show and a permanent
        home for which journal is open."""
        self.status_journal = QLabel()
        self.statusBar().addPermanentWidget(self.status_journal)

    def apply_app_theme(self):
        """Push the theme's dialog and menu stylesheets onto the QApplication.

        Called at startup and whenever the theme changes, so open-anew dialogs
        match the rest of the window. Menus go on at the application level
        rather than per-widget because they are created on demand all over the
        app — the member right-click, the ledger's context menus, the menu bar —
        and one of them left unstyled is one that renders in the platform's
        colours instead of the theme's.
        """
        app = QApplication.instance()
        if app is not None:
            theme = self.dashboard.theme_manager
            app.setStyleSheet(theme.dialog_stylesheet() + theme.menu_css())

    def refresh_window_title(self):
        """Title carries the configured organization name when one is set.

        The title shows the journal's file name only; the full path (which can
        be long and can leak a location in a shared screenshot) lives in the
        status bar tooltip.
        """
        from src import branding
        org = branding.get_org_name(self.db)
        name = os.path.basename(self.db_path)
        prefix = f"{org} — LoanMaster {__version__}" if org else f"LoanMaster {__version__}"
        self.setWindowTitle(f"{prefix} — {name}")
        if hasattr(self, "status_journal"):
            self.status_journal.setText(f"{org}  •  {name}" if org else name)
            self.status_journal.setToolTip(self.db_path)

    def _menu_action(self, menu, text, slot, tip=None):
        """Add one action to a menu, wired to a slot. Returns the QAction."""
        action = QAction(text, self)
        if tip:
            action.setStatusTip(tip)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def create_menus(self):
        """Build the application menu bar.

        Whole-journal actions that used to be a wall of buttons on the dashboard
        live here — Reports, Operations and Tools — so they are reachable from
        either screen and the list itself stays uncluttered.
        """
        menubar = self.menuBar()
        d = self.dashboard

        file_menu = menubar.addMenu("&File")
        exit_action = self._menu_action(file_menu, "E&xit", self.close, "Close LoanMaster")
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)

        # Undo/Redo keep their existing shortcuts; a menu action's shortcut is
        # active whenever this window is, so no separate addAction is needed.
        edit_menu = menubar.addMenu("&Edit")
        undo_action = self._menu_action(edit_menu, "Undo", self.undo_operation,
                                        "Undo the last operation")
        undo_action.setShortcut(QKeySequence("Ctrl+Alt+Z"))
        redo_action = self._menu_action(edit_menu, "Redo", self.redo_operation,
                                        "Redo the last undone operation")
        redo_action.setShortcut(QKeySequence("Ctrl+Alt+Y"))

        reports_menu = menubar.addMenu("&Reports")
        self._menu_action(reports_menu, "Loan (Quarterly) Report…",
                          d.generate_quarterly_report)
        self._menu_action(reports_menu, "Savings / Shares Report…",
                          lambda: d._open_fund_report_dialog("savings", "Savings / Shares"))
        self._menu_action(reports_menu, "Christmas Report…",
                          lambda: d._open_fund_report_dialog("christmas", "Christmas"))
        self._menu_action(reports_menu, "Benevolent Report…",
                          lambda: d._open_fund_report_dialog("benevolent", "Benevolent"))
        reports_menu.addSeparator()
        self._menu_action(reports_menu, "Loans in Arrears…", d.show_arrears_dialog,
                          "Who is behind on a loan, and by how much — with a "
                          "printable list")
        self._menu_action(reports_menu, "Members List (Excel / PDF)…",
                          d.export_members_list)
        self._menu_action(reports_menu, "Statements…", d.batch_print_selected,
                          "Generate member statements as PDF or Excel")

        ops_menu = menubar.addMenu("&Operations")
        grid_action = self._menu_action(
            ops_menu, "Fund Grid (bulk edit)…", d.open_fund_grid,
            "Edit savings, Christmas and benevolent for every member in one table")
        grid_action.setShortcut(QKeySequence("Ctrl+G"))
        ops_menu.addSeparator()
        self._menu_action(ops_menu, "Mass Deduct (Loans)…", d.open_mass_deduction_dialog)
        self._menu_action(ops_menu, "Mass Savings (Increments)…", d.open_mass_savings_dialog)
        self._menu_action(ops_menu, "Mass Christmas (Contributions)…", d.open_mass_christmas_dialog)
        self._menu_action(ops_menu, "Mass Benevolent (Deductions)…", d.open_mass_benevolent_dialog)
        ops_menu.addSeparator()
        self._menu_action(ops_menu, "Undo Last Mass Operation", d.undo_mass_operation)

        tools_menu = menubar.addMenu("&Tools")
        self._menu_action(tools_menu, "Settings…", self.open_settings,
                          "Organization, financial and appearance settings")
        tools_menu.addSeparator()
        self._menu_action(tools_menu, "Repair Loans (legacy terms)…", d.open_repair_loans_dialog)
        self._menu_action(tools_menu, "Repair Orphaned Records…", d.open_orphan_repair_dialog)
        self._menu_action(tools_menu, "Activity Log…", d.show_activity_log)
        self._menu_action(tools_menu, "Restore From Backup…", d.open_restore_dialog)
        self._menu_action(tools_menu, "Journal Health Check…", self.show_health_check,
                          "Run every integrity check on this journal")

        help_menu = menubar.addMenu("&Help")
        self._menu_action(help_menu, "About LoanMaster", self.show_about)
        self._menu_action(help_menu, "Open Log Folder", self.open_log_folder,
                          "Open the folder with the application log, to send to support")

    def open_settings(self):
        """Open the settings dialog (owned by the dashboard)."""
        self.dashboard.open_settings()

    def show_about(self):
        QMessageBox.about(
            self, "About LoanMaster",
            f"<b>LoanMaster {__version__}</b><br>"
            "Loan, savings & fund management for SACCOs.<br><br>"
            f"Log folder:<br>{get_log_dir()}")

    def open_log_folder(self):
        """Open the log folder in the file manager, for sending logs to support."""
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_log_dir()))

    def undo_operation(self):
        """Perform undo."""
        # Check if engine exists (it should)
        if hasattr(self.dashboard, 'engine') and self.dashboard.engine:
            undo_mgr = self.dashboard.engine.undo_manager
            cmd = undo_mgr.undo() # This returns the command if success, None if stack empty
            
            if cmd:
                QMessageBox.information(self, "Undo", f"Undid: {cmd.description}")
                # Refresh UI
                if self.stack.currentWidget() == self.dashboard:
                    self.dashboard.refresh_list()
                elif self.stack.currentWidget() == self.ledger_view:
                    # Refresh ledger logic?
                    # LedgerView usually needs reload for specific individual.
                    # We can just check which ledger is open?
                    # LedgerView has self.current_individual_id
                    if hasattr(self.ledger_view, 'current_individual_id') and self.ledger_view.current_individual_id:
                        self.ledger_view.refresh_ledger()
            else:
                QMessageBox.information(self, "Undo", "Nothing to undo.")

    def redo_operation(self):
        """Perform redo."""
        if hasattr(self.dashboard, 'engine') and self.dashboard.engine:
            undo_mgr = self.dashboard.engine.undo_manager
            cmd = undo_mgr.redo()
            
            if cmd:
                QMessageBox.information(self, "Redo", f"Redone: {cmd.description}")
                if self.stack.currentWidget() == self.dashboard:
                    self.dashboard.refresh_list()
                elif self.stack.currentWidget() == self.ledger_view:
                    if hasattr(self.ledger_view, 'current_individual_id') and self.ledger_view.current_individual_id:
                        self.ledger_view.refresh_ledger()
            else:
                QMessageBox.information(self, "Redo", "Nothing to redo.")

    def show_health_check(self):
        """Open the full journal integrity report.

        Reachable from the startup warning and from Treasury, so the 35 checks
        are usable without a terminal.
        """
        from src.theme import ThemeManager
        from src.views.integrity_report import IntegrityReportDialog
        IntegrityReportDialog(self.db, ThemeManager(self.db), self).exec()

    def show_dashboard(self):
        self.dashboard.refresh_list()
        self.stack.setCurrentWidget(self.dashboard)

    def show_ledger(self, ind_id, name):
        self.ledger_view.load_individual(ind_id, name)
        self.stack.setCurrentWidget(self.ledger_view)

    def closeEvent(self, event):
        """Close cleanly. Writes commit as they happen, so there is nothing
        unsaved to guard — only interrupt if a report is still generating."""
        worker = getattr(self.dashboard, "report_worker", None)
        if worker is not None and worker.isRunning():
            reply = QMessageBox.question(
                self, "Report in progress",
                "A report is still being generated. Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main():
    """Entry point for the application."""
    log_path = setup_logging()
    install_crash_handler()
    install_qt_message_handler()
    logger.info("LoanMaster %s starting (log file: %s)", __version__, log_path)

    app = QApplication(sys.argv)

    # Show startup dialog first
    dialog = StartupDialog()
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_db:
        logger.info("Opening journal: %s", dialog.selected_db)
        window = MainApp(dialog.selected_db)
        window.show()
        exit_code = app.exec()
        logger.info("LoanMaster exiting (code %s)", exit_code)
        sys.exit(exit_code)
    else:
        logger.info("No journal selected; exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()

