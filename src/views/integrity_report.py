"""The journal integrity report, as a screen rather than a terminal command.

``src.integrity`` runs 35 checks across the structure, the referential links,
the books and the projection. Until now the application ran exactly one of them
— a yes/no at startup — and, when something was wrong, told the user to run

    python -m src.integrity "path/to/journal.db"

which for a SACCO clerk is a dead end. The detail existed and was unreachable.

This shows the whole report, grouped, with failures first, and explains what
each state means in terms of what to do about it.
"""
import logging

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QHBoxLayout,
                             QHeaderView, QLabel, QMessageBox, QProgressDialog,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from src import integrity

logger = logging.getLogger(__name__)

# What each status means for the person reading it, not for the code.
_MEANING = {
    integrity.FAIL: ("Problem", "danger",
                     "Something is wrong that affects the figures. Fix these first."),
    integrity.WARN: ("Check", "warning",
                     "Worth a look. The books still balance, but something does "
                     "not add up as expected."),
    integrity.PASS: ("OK", "success", ""),
}
_ORDER = {integrity.FAIL: 0, integrity.WARN: 1, integrity.PASS: 2}


class IntegrityReportDialog(QDialog):
    """Read-only view of every integrity check on the open journal."""

    def __init__(self, db_manager, theme_manager, parent=None):
        super().__init__(parent)
        self.db = db_manager
        self.theme_manager = theme_manager
        self.report = None

        self.setWindowTitle("Journal Health Check")
        self.resize(880, 620)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build(self):
        layout = QVBoxLayout(self)

        self.summary = QLabel("Running checks…")
        self.summary.setStyleSheet("font-size: 15px; font-weight: bold;")
        layout.addWidget(self.summary)

        self.advice = QLabel("")
        self.advice.setWordWrap(True)
        self.advice.setStyleSheet(self.theme_manager.hint_css())
        layout.addWidget(self.advice)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["", "Check", "Area", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.btn_refresh = QPushButton("Re-run Checks")
        self.btn_refresh.clicked.connect(self.refresh)
        buttons.addWidget(self.btn_refresh)
        self.btn_copy = QPushButton("Copy Report")
        self.btn_copy.setToolTip("Copy the full report as text, to paste into an email")
        self.btn_copy.clicked.connect(self.copy_report)
        buttons.addWidget(self.btn_copy)
        buttons.addStretch()
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ #
    def refresh(self):
        """Run every check and repaint. The integrity_check pragma reads the
        whole file, so this is not instant on a large journal."""
        pd = QProgressDialog("Checking the journal…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setMinimumDuration(300)
        pd.setCancelButton(None)
        QApplication.processEvents()
        try:
            self.report = integrity.verify(self.db)
        except Exception as e:                     # noqa: BLE001 - report, never crash
            logger.exception("Integrity check failed to run")
            pd.close()
            QMessageBox.critical(self, "Check Failed",
                                 f"The health check could not complete:\n\n{e}")
            return
        finally:
            pd.close()
        self._paint()

    def _paint(self):
        checks = sorted(self.report.checks,
                        key=lambda c: (_ORDER.get(c.status, 3), c.category, c.name))
        failures, warnings = len(self.report.failures), len(self.report.warnings)

        if failures:
            headline = f"{failures} problem(s) found"
            role = "danger"
        elif warnings:
            headline = f"No problems. {warnings} thing(s) worth checking."
            role = "warning"
        else:
            headline = "Everything checks out."
            role = "success"
        self.summary.setText(f"{headline}   —   {len(checks)} checks run")
        self.summary.setStyleSheet(
            f"font-size: 15px; font-weight: bold; color: {self.theme_manager.get_color(role)};")

        if failures:
            self.advice.setText(
                "A problem means the figures are affected. Start with the Repair "
                "tools under Mass Operations: 'Repair Loans' re-derives loan "
                "balances from the ledger, and 'Repair Orphaned Records' fixes "
                "broken member links. If you are unsure, restore a recent backup.")
        elif warnings:
            self.advice.setText(
                "Warnings are not errors: the books balance. They flag figures that "
                "look unusual and are usually a question for whoever keeps the "
                "records rather than something the software can decide.")
        else:
            self.advice.setText(
                "Structure, member links, the ledger and the period projection "
                "all agree.")

        self.table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            label, role, meaning = _MEANING.get(check.status, ("?", "text_primary", ""))
            status_item = QTableWidgetItem(label)
            status_item.setForeground(QColor(self.theme_manager.get_color(role)))
            if meaning:
                # What this status means for the reader, e.g. "Fix these first".
                status_item.setToolTip(meaning)
            self.table.setItem(row, 0, status_item)
            self.table.setItem(row, 1, QTableWidgetItem(check.name))
            self.table.setItem(row, 2, QTableWidgetItem(check.category))
            self.table.setItem(row, 3, QTableWidgetItem(check.detail))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

    def copy_report(self):
        """Put the plain-text report on the clipboard.

        The same text the command-line tool prints, so a user can paste it into
        an email instead of being asked to run anything.
        """
        if self.report is None:
            return
        QApplication.clipboard().setText(self.report.format_text())
        QMessageBox.information(self, "Copied",
                                "The full report has been copied to the clipboard.")
