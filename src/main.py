"""Main application window for LoanMaster."""
import logging
import sys
import os

# Disable GPU and software rasterizer to prevent crashes on older hardware (DirectX 11 issues)
# This is critical for systems where QtWebEngine fails to initialize GPU process.
# Must be set before QWebEngineWidgets is imported/initialized.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-software-rasterizer"

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
        """Push the theme's dialog stylesheet onto the QApplication.

        Called at startup and whenever the theme changes, so open-anew dialogs
        match the rest of the window.
        """
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(self.dashboard.theme_manager.dialog_stylesheet())

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

    def create_menus(self):
        """Build the application menu bar: File / Edit / Tools / Help."""
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.setStatusTip("Close LoanMaster")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # Undo/Redo keep their existing shortcuts; a menu action's shortcut is
        # active whenever this window is, so no separate addAction is needed.
        edit_menu = menubar.addMenu("&Edit")
        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence("Ctrl+Alt+Z"))
        undo_action.setStatusTip("Undo the last operation")
        undo_action.triggered.connect(self.undo_operation)
        edit_menu.addAction(undo_action)
        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence("Ctrl+Alt+Y"))
        redo_action.setStatusTip("Redo the last undone operation")
        redo_action.triggered.connect(self.redo_operation)
        edit_menu.addAction(redo_action)

        tools_menu = menubar.addMenu("&Tools")
        settings_action = QAction("Settings…", self)
        settings_action.setStatusTip("Organization, financial and appearance settings")
        settings_action.triggered.connect(self.open_settings)
        tools_menu.addAction(settings_action)
        health_action = QAction("Journal Health Check…", self)
        health_action.setStatusTip("Run every integrity check on this journal")
        health_action.triggered.connect(self.show_health_check)
        tools_menu.addAction(health_action)

        help_menu = menubar.addMenu("&Help")
        about_action = QAction("About LoanMaster", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        log_action = QAction("Open Log Folder", self)
        log_action.setStatusTip("Open the folder with the application log, to send to support")
        log_action.triggered.connect(self.open_log_folder)
        help_menu.addAction(log_action)

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

