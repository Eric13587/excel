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
                            install_qt_message_handler)
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
            QMessageBox.warning(
                self, "Database Warning",
                "The integrity check on this journal reported problems.\n\n"
                "The journal was still opened so you can export your data, "
                "but you should restore a recent backup from the 'backups' "
                "folder next to the journal file as soon as possible.")

        # Live general-ledger posting: member loan/savings activity posts to the
        # double-entry GL the moment it is written. Idempotent and defensive, so
        # it never breaks a core write; edits/undos reconcile on the next sync.
        from src.services.gl_service import GLService
        self.gl = GLService(self.db)
        self.db.ledger_post_hook = self.gl.post_ledger_rows
        self.db.savings_post_hook = self.gl.post_savings_rows

        # Persistent date defaults
        self.last_operation_date = QDate.currentDate()
        self.last_report_range = (QDate.currentDate().addMonths(-12), QDate.currentDate())
        
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        
        self.dashboard = Dashboard(self, self.db)
        self.ledger_view = LedgerView(self, self.db)
        
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.ledger_view)
        
        self.show_dashboard()

        self.create_menus()
        self.refresh_window_title()

    def refresh_window_title(self):
        """Title carries the configured organization name when one is set."""
        from src import branding
        org = branding.get_org_name(self.db)
        prefix = f"{org} — LoanMaster {__version__}" if org else f"LoanMaster {__version__}"
        self.setWindowTitle(f"{prefix} - [{self.db_path}]")

    def create_menus(self):
        """Create application menus."""
        self.menuBar()
        
        # Global Undo/Redo Actions (No Edit Menu)
        
        # Undo Action
        undo_action = QAction("Undo", self)
        undo_action.setShortcut(QKeySequence("Ctrl+Alt+Z"))
        undo_action.setStatusTip("Undo last operation")
        undo_action.triggered.connect(self.undo_operation)
        self.addAction(undo_action)
        
        # Redo Action
        redo_action = QAction("Redo", self)
        redo_action.setShortcut(QKeySequence("Ctrl+Alt+Y"))
        redo_action.setStatusTip("Redo last operation")
        redo_action.triggered.connect(self.redo_operation)
        self.addAction(redo_action)

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

    def show_dashboard(self):
        self.dashboard.refresh_list()
        self.stack.setCurrentWidget(self.dashboard)

    def show_ledger(self, ind_id, name):
        self.ledger_view.load_individual(ind_id, name)
        self.stack.setCurrentWidget(self.ledger_view)

    def closeEvent(self, event):
        """Confirm before exiting."""
        reply = QMessageBox.question(self, "Exit", "Are you sure you want to exit?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            event.accept()
        else:
            event.ignore()


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

