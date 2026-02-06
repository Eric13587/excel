"""Main application window for LoanMaster."""
import sys
import os

# Disable GPU and software rasterizer to prevent crashes on older hardware (DirectX 11 issues)
# This is critical for systems where QtWebEngine fails to initialize GPU process.
# Must be set before QWebEngineWidgets is imported/initialized.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-software-rasterizer"

# Import QtWebEngineWidgets BEFORE QApplication is created (needed for PDF generation)
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    pass  # Not installed, PDF generation will fall back to HTML

from PyQt6.QtWidgets import (QApplication, QMainWindow, QStackedWidget, QMessageBox, 
                             QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QWidget)
from PyQt6.QtGui import QIcon, QAction, QKeySequence
from PyQt6.QtCore import Qt, QDate

from .database import DatabaseManager
from .views.dashboard import Dashboard
from .views.ledger import LedgerView


class StartupDialog(QDialog):
    """Dialog to select or create a database file."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LoanMaster - Select Journal")
        self.setFixedSize(400, 200)
        self.selected_db = None
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        title = QLabel("Welcome to LoanMaster")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #2b5797;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        subtitle = QLabel("Please select a journal to continue:")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        
        btn_new = QPushButton("Create New Journal")
        btn_new.setMinimumHeight(40)
        btn_new.clicked.connect(self.create_new)
        layout.addWidget(btn_new)
        
        btn_open = QPushButton("Open Existing Journal")
        btn_open.setMinimumHeight(40)
        btn_open.clicked.connect(self.open_existing)
        layout.addWidget(btn_open)

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
        self.setWindowTitle(f"LoanMaster Multi-User - [{db_path}]")
        self.resize(1100, 700) # Slightly larger default size
        
        # Set Application Icon
        # Set Application Icon
        if getattr(sys, 'frozen', False):
             base_path = os.path.dirname(sys.executable)
        else:
             base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
             
        icon_path = os.path.join(base_path, "resources", "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.db = DatabaseManager(db_path)
        
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

    def create_menus(self):
        """Create application menus."""
        menubar = self.menuBar()
        
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
    app = QApplication(sys.argv)
    
    # Show startup dialog first
    dialog = StartupDialog()
    if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_db:
        window = MainApp(dialog.selected_db)
        window.show()
        sys.exit(app.exec())
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()

