"""Restore the journal from a backup, without leaving the application.

Backups were already being written — on every open, and before each
irreversible change — and the user was told the path. Opening one meant finding
it on disk and knowing what to do with it, which is not a thing to ask of
someone mid-mistake. This is the other half of that safety net.

Restoring is itself destructive, so the screen is built around three things:

* **show what is in each file** before choosing, not just its name and size;
* **refuse a file that is damaged or is not a journal**, checked before
  anything is touched;
* **snapshot the current state first**, because restoring the wrong file is the
  commonest way to lose data with a restore feature.
"""
import logging
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog,
                             QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                             QProgressDialog, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

logger = logging.getLogger(__name__)


def _human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class RestoreBackupDialog(QDialog):
    """Pick a backup, see what is in it, and restore it."""

    def __init__(self, db_manager, theme_manager, parent=None, on_restored=None):
        super().__init__(parent)
        self.db = db_manager
        self.theme_manager = theme_manager
        self.on_restored = on_restored
        self.backups = []

        self.setWindowTitle("Restore From Backup")
        self.resize(860, 520)
        self._build()
        self.reload()

    # ------------------------------------------------------------------ #
    def _build(self):
        layout = QVBoxLayout(self)

        blurb = QLabel(
            "Restoring replaces everything in the current journal with the "
            "contents of the backup. A copy of the journal as it stands now is "
            "saved first, so this can be undone by restoring that copy.")
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Taken", "Members", "Loans", "Ledger entries", "File"])
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self.table)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(self.theme_manager.hint_css())
        layout.addWidget(self.detail)

        buttons = QHBoxLayout()
        self.btn_browse = QPushButton("Choose a File…")
        self.btn_browse.setToolTip("Restore from a backup kept somewhere else")
        self.btn_browse.clicked.connect(self.browse)
        buttons.addWidget(self.btn_browse)
        buttons.addStretch()
        self.btn_restore = QPushButton("Restore Selected")
        self.btn_restore.setStyleSheet(self.theme_manager.button_css("danger"))
        self.btn_restore.setEnabled(False)
        self.btn_restore.clicked.connect(self.restore_selected)
        buttons.addWidget(self.btn_restore)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        layout.addLayout(buttons)

    # ------------------------------------------------------------------ #
    def reload(self):
        """List the backups beside the journal, newest first.

        Each is opened read-only to read its counts, so the table says what is
        *in* a file rather than only how big it is — the difference between
        choosing a backup and guessing at one.
        """
        self.backups = self.db.available_backups()
        self.table.setRowCount(len(self.backups))
        for row, backup in enumerate(self.backups):
            info = self.db.inspect_backup(backup["path"])
            backup["info"] = info
            counts = info["counts"]
            cells = (
                backup["modified"].strftime("%Y-%m-%d %H:%M"),
                str(counts.get("individuals", "—")) if info["ok"] else "—",
                str(counts.get("loans", "—")) if info["ok"] else "—",
                str(counts.get("ledger", "—")) if info["ok"] else "—",
                backup["name"],
            )
            for col, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if not info["ok"]:
                    item.setForeground(
                        self.palette().color(self.foregroundRole()))
                    item.setToolTip(info["problem"] or "")
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        if not self.backups:
            self.detail.setText(
                "No backups found next to this journal. One is written every "
                "time the application opens it, and before any irreversible "
                "change.")
        self.btn_restore.setEnabled(False)

    def _selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        return self.backups[min(rows)] if rows else None

    def _on_selection(self):
        backup = self._selected()
        if backup is None:
            self.detail.setText("")
            self.btn_restore.setEnabled(False)
            return
        info = backup["info"]
        if not info["ok"]:
            self.detail.setText(f"This file cannot be used: {info['problem']}")
            self.btn_restore.setEnabled(False)
            return
        counts = info["counts"]
        self.detail.setText(
            f"{backup['name']} — {_human_size(backup['size'])}, taken "
            f"{backup['modified']:%d %b %Y at %H:%M}. Contains "
            f"{counts.get('individuals', 0)} members, {counts.get('loans', 0)} loans, "
            f"{counts.get('ledger', 0)} ledger entries and "
            f"{counts.get('savings', 0)} savings transactions.")
        self.btn_restore.setEnabled(True)

    # ------------------------------------------------------------------ #
    def browse(self):
        start = os.path.dirname(os.path.abspath(self.db.db_name or "."))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a Backup", start, "Journals (*.db *.bak);;All files (*)")
        if path:
            self._restore(path)

    def restore_selected(self):
        backup = self._selected()
        if backup:
            self._restore(backup["path"])

    def _restore(self, path):
        info = self.db.inspect_backup(path)
        if not info["ok"]:
            QMessageBox.critical(self, "Cannot Restore",
                                 f"That file cannot be restored:\n\n{info['problem']}")
            return

        counts = info["counts"]
        live = {"individuals": self.db.conn.execute(
                    "SELECT COUNT(*) FROM individuals").fetchone()[0],
                "ledger": self.db.conn.execute(
                    "SELECT COUNT(*) FROM ledger").fetchone()[0]}
        if QMessageBox.question(
                self, "Restore Journal",
                f"Replace the current journal with this backup?\n\n"
                f"Now:      {live['individuals']} members, {live['ledger']} ledger entries\n"
                f"Backup:   {counts.get('individuals', 0)} members, "
                f"{counts.get('ledger', 0)} ledger entries\n\n"
                f"Everything recorded since this backup was taken will be "
                f"removed from the journal. A copy of the current state is saved "
                f"first, so this can be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return

        pd = QProgressDialog("Restoring…", None, 0, 0, self)
        pd.setWindowModality(Qt.WindowModality.WindowModal)
        pd.setCancelButton(None)
        pd.setMinimumDuration(200)
        QApplication.processEvents()
        try:
            result = self.db.restore_from(path)
        except Exception as e:                      # noqa: BLE001 - report, never crash
            logger.exception("Restore failed")
            pd.close()
            QMessageBox.critical(
                self, "Restore Failed",
                f"The journal could not be restored:\n\n{e}\n\n"
                f"Nothing has been changed.")
            return
        finally:
            pd.close()

        migration = result["migration_report"]
        extra = ""
        if migration.applied:
            extra = ("\n\nThe backup was on an older schema and has been brought "
                     "up to date.")
        if not result["integrity_ok"]:
            extra += ("\n\nWarning: the restored journal did not pass its "
                      "integrity check. Check Treasury → Health Check.")
        QMessageBox.information(
            self, "Restored",
            f"The journal has been restored from:\n{os.path.basename(path)}\n\n"
            f"The state before restoring was saved to:\n"
            f"{result['safety_copy'] or '(not needed)'}{extra}")

        if self.on_restored:
            self.on_restored()
        self.reload()
