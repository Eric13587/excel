"""Asking "catch up until when?" — one dialog, used by every catch-up.

Every catch-up in the application walks forward from where a member's record
stops until it reaches a date. That date used to be "today" for the
single-member buttons and a hand-rolled picker in two of the mass dialogs, so
the same question was asked three different ways and not at all in five places.

It matters more than it looks. A SACCO reconciling a payroll run from three
months ago needs the catch-up to stop at the month that run covered; carried to
today it invents deductions nobody took, and those are then real rows somebody
has to unpick.
"""
from datetime import datetime

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (QDateEdit, QDialog, QDialogButtonBox, QLabel,
                             QVBoxLayout)


def ask_catch_up_target(parent, what="deductions", default=None, hint=None):
    """Ask which month to catch up to. Returns a ``datetime``, or None if cancelled.

    ``default`` may be a ``date``/``datetime`` or None for today. The returned
    value is inclusive: catch-up runs through the month containing it.
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle("Catch Up To")
    dialog.setMinimumWidth(380)
    layout = QVBoxLayout(dialog)

    layout.addWidget(QLabel(f"Run {what} up to and including:"))

    picker = QDateEdit()
    picker.setCalendarPopup(True)
    picker.setDisplayFormat("MMMM yyyy")
    picker.setDate(QDate(default.year, default.month, default.day) if default
                   else QDate.currentDate())
    layout.addWidget(picker)

    note = QLabel(hint or
                  "Nothing after this month is created. Set it to the month your "
                  "last payroll run covered if you are reconciling the past.")
    note.setWordWrap(True)
    if hasattr(parent, "theme_manager"):
        note.setStyleSheet(parent.theme_manager.hint_css())
    layout.addWidget(note)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)

    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    chosen = picker.date()
    return datetime(chosen.year(), chosen.month(), chosen.day())
