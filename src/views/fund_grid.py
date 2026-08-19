"""A spreadsheet for the three member funds.

Rows are members, columns are months, and a cell is what moved that month. The
monthly job — checking that everyone paid, fixing the four who did not — is a
table, and doing it through per-member screens means opening two hundred of
them. This is that table.

HOW EDITING WORKS, AND WHY IT IS STAGED
---------------------------------------
Typing does not write. Edits collect as *staged changes*, shown in the accent
colour, until Save applies them as one batch. Three reasons, in order of
importance:

1. **It cannot half-happen.** One transaction covers the batch, so a failure
   leaves the funds exactly as they were rather than partly rewritten.
2. **You can see your work before it counts.** A number typed into the wrong
   row is obvious while it is still orange and reversible; it is not obvious
   once it is a deposit.
3. **It is fast.** A fund's running balance is a property of a member's whole
   ordered history, so writing live would re-derive it on every keystroke.

Everything else follows the spreadsheet the user already knows: arrow keys and
Tab to move, type to replace, Delete to clear, Ctrl+C / Ctrl+V to move a block
of figures in or out of Excel, Ctrl+Z to take back the last edit.

WHAT THIS SCREEN DELIBERATELY WILL NOT DO
-----------------------------------------
* **Loans are not here.** A loan schedule is derived from its terms — principal,
  interest, suspensions — and is not a set of independent monthly numbers. A
  grid that let you type over it would produce a ledger that no longer follows
  from anything. Loans keep their own screen.
* **A month with several transactions is read only.** Shown with a · marker and
  the net figure, because one editable number cannot represent two facts without
  destroying one of them. Open that member to work on those rows.
* **Nothing here bypasses a rule.** Every write goes through the same posting
  pipeline and membership policy as the per-member screens, so what is refused
  there is refused here — with the reason, against the cell that caused it.
"""
import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QColor, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QMessageBox, QProgressDialog,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QTabWidget, QVBoxLayout, QWidget)

from src.services import fund_grid as model

logger = logging.getLogger(__name__)

#: Marker on a cell holding more than one transaction.
MIXED_MARK = " ·"


def _money(value):
    """Grid figures are whole shillings; a dash reads better than a zero."""
    value = round(float(value or 0), 2)
    if value == 0:
        return "—"
    return f"{value:,.0f}"


class FundGridWindow(QDialog):
    """The grid, one tab per fund."""

    def __init__(self, parent, db, theme_manager, on_open_member=None, engine=None):
        super().__init__(parent)
        self.db = db
        self.theme = theme_manager
        self.on_open_member = on_open_member
        if engine is None:
            from src.engine import LoanEngine
            engine = LoanEngine(db)
        self.engine = engine

        self.setWindowTitle("Ledger Grid — bulk editing")
        self.setMinimumSize(1100, 640)
        self.setSizeGripEnabled(True)
        # Named rather than inherited, so the window is right even if the
        # application-level dialog stylesheet has not been applied. The tab pane
        # matters as much as the dialog: it is a widget of its own and keeps the
        # default (light) background otherwise, which is what left light text on
        # a white strip above a dark table.
        t = theme_manager
        self.setStyleSheet(f"""
            QDialog {{ background-color: {t.get_color('bg_secondary')}; }}
            QTabWidget::pane {{
                background-color: {t.get_color('bg_secondary')};
                border: 1px solid {t.get_color('border')};
                border-radius: 4px;
            }}
            QTabBar::tab {{
                background-color: {t.get_color('bg_primary')};
                color: {t.get_color('text_secondary')};
                border: 1px solid {t.get_color('border')};
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 6px 14px;
                margin-right: 2px;
            }}
            QTabBar::tab:selected {{
                background-color: {t.get_color('bg_secondary')};
                color: {t.get_color('accent')};
                font-weight: bold;
            }}
        """)

        self.pages = {}
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Type to edit, Delete to clear, Ctrl+C / Ctrl+V to move figures to and "
            "from Excel. On the fund tabs a negative number records a withdrawal; on "
            "Loans a figure takes that month's deduction. Nothing is written until "
            "you press Save.")
        intro.setWordWrap(True)
        intro.setStyleSheet(self.theme.hint_css())
        layout.addWidget(intro)

        self.tabs = QTabWidget()
        for backend in self._backends():
            page = FundGridPage(self, db, theme_manager, backend, engine=self.engine,
                                on_open_member=self._open_member)
            self.pages[backend.fund] = page
            self.tabs.addTab(page, backend.label)
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(self.reject)
        layout.addWidget(close_box)

        self.first_tab = next(iter(self.pages))
        self.pages[self.first_tab].reload()

    @staticmethod
    def _backends():
        """The ledgers this window edits, in the order they are offered.

        Loans come last because they are the constrained one: most of what a
        cell will accept there is decided by the loan's schedule rather than by
        the person typing.
        """
        from src.services.loan_grid import LoanBackend

        return [model.FundBackend(fund) for fund in model.FUND_ORDER] + [LoanBackend()]

    def _tab_changed(self, index):
        """Load a fund the first time its tab is looked at, not before."""
        page = self.tabs.widget(index)
        if page is not None and not page.loaded:
            page.reload()

    def _open_member(self, member_id, name):
        """Hand a member over to the ledger, after settling any unsaved work."""
        if not self.confirm_discard("open a member's ledger"):
            return
        if self.on_open_member:
            self.accept()
            self.on_open_member(member_id, name)

    def unsaved(self):
        """Every fund with staged changes, as {label: count}."""
        return {page.backend.label: len(page.staged)
                for page in self.pages.values() if page.staged}

    def confirm_discard(self, what="close"):
        """Ask before throwing staged work away. True means carry on."""
        pending = self.unsaved()
        if not pending:
            return True
        detail = ", ".join(f"{n} in {label}" for label, n in pending.items())
        answer = QMessageBox.question(
            self, "Unsaved Changes",
            f"There are unsaved changes ({detail}).\n\n"
            f"Discard them and {what}?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Discard

    def reject(self):
        if self.confirm_discard("close the grid"):
            super().reject()

    def closeEvent(self, event):
        if self.confirm_discard("close the grid"):
            event.accept()
        else:
            event.ignore()


class FundGridPage(QWidget):
    """One fund's grid: the controls, the table and the staged edits."""

    def __init__(self, window, db, theme_manager, backend, engine=None,
                 on_open_member=None):
        super().__init__()
        self.window_ref = window
        self.db = db
        self.theme = theme_manager
        self.backend = backend
        self.engine = engine
        self.fund = backend.fund
        self.spec = backend
        self.on_open_member = on_open_member

        self.loaded = False
        self.grid = None
        self.months = []
        self.rows = []                  # visible MemberRows, in table row order
        self.staged = {}                # (member_id, month) -> Change
        self.refused = {}               # (member_id, month) -> reason
        self.blocked = {}               # member_id -> reason
        self.hidden_members = 0         # past members left out of the current view
        self.edit_history = []          # for Ctrl+Z on staged edits
        self.last_batch = None          # for "Undo last save"
        self._filling = False           # guards the itemChanged signal
        self._defer_totals = False      # batch the totals redraw during a paste/fill

        self._build()

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    def _build(self):
        self._style_page()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("From:"))
        self.from_month = self._month_picker(QDate.currentDate().addMonths(-11))
        controls.addWidget(self.from_month)
        controls.addWidget(QLabel("To:"))
        self.to_month = self._month_picker(QDate.currentDate())
        controls.addWidget(self.to_month)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter members…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        controls.addWidget(self.search, 1)

        self.include_inactive = QCheckBox("Include past members")
        self.include_inactive.setToolTip(
            "Members who have retired, resigned or been terminated. They can "
            "still have withdrawals recorded, but not new contributions.")
        self.include_inactive.stateChanged.connect(lambda: self.reload())
        controls.addWidget(self.include_inactive)

        self.show_excluded = QCheckBox("Show excluded")
        self.show_excluded.setToolTip(
            "Members deliberately left out of this ledger — somebody who does "
            "not save, or is not on the benevolent scheme. Excluding is a "
            "display decision: it hides nobody's history and deletes nothing.")
        self.show_excluded.stateChanged.connect(lambda: self.reload())
        controls.addWidget(self.show_excluded)

        reload_btn = QPushButton("Reload")
        reload_btn.setToolTip("Re-read the journal, discarding unsaved changes")
        reload_btn.clicked.connect(self._reload_clicked)
        controls.addWidget(reload_btn)
        layout.addLayout(controls)

        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self._style_table()
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemDoubleClicked.connect(self._double_clicked)
        layout.addWidget(self.table)

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.fill_btn = QPushButton("Fill Forward…")
        self.fill_btn.setStyleSheet(self.theme.outline_button_css("accent", padding="6px 10px"))
        self.fill_btn.setToolTip(
            "Propose each member's usual amount for every empty month in range — "
            "the grid's version of Catch Up. Staged for review, not written.")
        self.fill_btn.clicked.connect(self._fill_forward)
        buttons.addWidget(self.fill_btn)

        export_btn = QPushButton("Export…")
        export_btn.setStyleSheet(self.theme.outline_button_css("accent", padding="6px 10px"))
        export_btn.setToolTip("Save the grid as shown to Excel, CSV or PDF")
        export_btn.clicked.connect(self._export)
        buttons.addWidget(export_btn)
        buttons.addStretch()

        self.undo_save_btn = QPushButton("Undo Last Save")
        self.undo_save_btn.setStyleSheet(self.theme.outline_button_css("warning", padding="6px 10px"))
        self.undo_save_btn.setEnabled(False)
        self.undo_save_btn.clicked.connect(self._undo_last_save)
        buttons.addWidget(self.undo_save_btn)

        self.discard_btn = QPushButton("Discard Changes")
        self.discard_btn.setStyleSheet(self.theme.button_css("neutral", padding="6px 12px"))
        self.discard_btn.setEnabled(False)
        self.discard_btn.clicked.connect(self._discard)
        buttons.addWidget(self.discard_btn)

        self.save_btn = QPushButton("Save Changes")
        self.save_btn.setStyleSheet(self.theme.button_css("success", padding="6px 14px", bold=True))
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)

        QShortcut(QKeySequence.StandardKey.Copy, self.table, self._copy)
        QShortcut(QKeySequence.StandardKey.Paste, self.table, self._paste)
        QShortcut(QKeySequence.StandardKey.Undo, self.table, self._undo_edit)
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table, self._clear_selection)

    def _style_page(self):
        """Theme this page's own widgets.

        The application-level dialog stylesheet themes dialog contents, but this
        page sits several widgets deep inside a tab, and its plain labels and
        checkbox were coming out in the default palette's near-black — invisible
        on a dark background. The main views style themselves per-widget for the
        same reason; this follows them rather than depending on a selector
        reaching down through the tab stack.
        """
        t = self.theme
        # Scoped by object name: a bare `QWidget { background }` rule would
        # propagate to every child and repaint the table and buttons too.
        self.setObjectName("fundGridPage")
        self.setStyleSheet(f"""
            #fundGridPage {{ background-color: {t.get_color('bg_secondary')}; }}
            QLabel {{ color: {t.get_color('text_primary')}; background: transparent; }}
            QCheckBox {{ color: {t.get_color('text_primary')}; }}
            QComboBox, QLineEdit {{
                background-color: {t.get_color('input_bg')};
                color: {t.get_color('text_primary')};
                border: 1px solid {t.get_color('border')};
                border-radius: 4px;
                padding: 3px 6px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {t.get_color('bg_secondary')};
                color: {t.get_color('text_primary')};
                selection-background-color: {t.get_color('accent')};
            }}
        """)

    def _style_table(self):
        """Theme the grid explicitly rather than inheriting from the palette.

        Alternating rows are what make a wide table readable across, but Qt
        takes the stripe colour from the palette unless it is told otherwise —
        so on a dark theme every other row came out near-white with dark-on-dark
        text. Both colours are named here, from the theme, along with the
        selection and gridlines, so the grid reads the same in either mode and
        does not depend on the application-level dialog stylesheet having been
        applied first.
        """
        t = self.theme
        self.table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {t.get_color('bg_secondary')};
                alternate-background-color: {t.get_color('bg_primary')};
                color: {t.get_color('text_primary')};
                gridline-color: {t.get_color('border')};
                border: 1px solid {t.get_color('border')};
                selection-background-color: {t.get_color('card_selected_bg')};
                selection-color: {t.get_color('text_primary')};
            }}
            QHeaderView::section {{
                background-color: {t.get_color('bg_primary')};
                color: {t.get_color('text_secondary')};
                border: none;
                border-right: 1px solid {t.get_color('border')};
                border-bottom: 1px solid {t.get_color('border')};
                padding: 4px 6px;
                font-weight: bold;
            }}
            QTableCornerButton::section {{
                background-color: {t.get_color('bg_primary')};
                border: none;
            }}
        """)

    def _month_picker(self, default):
        """Twelve years of months, newest last, as a plain combo.

        A QDateEdit would invite a day-of-month that the grid has no use for.
        """
        combo = QComboBox()
        cursor = QDate.currentDate().addYears(-6)
        end = QDate.currentDate().addYears(6)
        index = 0
        while cursor <= end:
            key = cursor.toString("yyyy-MM")
            combo.addItem(model.month_label(key), key)
            if key == default.toString("yyyy-MM"):
                index = combo.count() - 1
            cursor = cursor.addMonths(1)
        combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda: self.reload())
        return combo

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #

    def _reload_clicked(self):
        if self.staged and not self.window_ref.confirm_discard("reload"):
            return
        self.reload()

    def reload(self):
        """Re-read this fund from the journal and redraw."""
        start = self.from_month.currentData()
        end = self.to_month.currentData()
        self.months = model.month_range(start, end)
        if not self.months:
            self.grid = self.backend.empty()
            self._render()
            self._set_status("Choose an end month on or after the start month.",
                             "warning")
            self.loaded = True
            return

        try:
            self.grid = self.backend.load(
                self.db, self.engine, self.months,
                include_inactive=self.include_inactive.isChecked(),
                include_excluded=self.show_excluded.isChecked())
            # Counted, not guessed: a member missing from the grid without a word
            # said is how somebody concludes the figures are wrong.
            self.hidden_members = (
                0 if self.include_inactive.isChecked()
                else sum(1 for p in self.db.members.summaries()
                         if p.get("is_retired")
                         or (p.get("employment_status") or "Active") != "Active"))
            self.blocked = self.backend.blocked(self.db, self.grid.members)
        except Exception as e:                  # noqa: BLE001 - report, don't crash
            logger.exception("Could not load the %s grid", self.fund)
            QMessageBox.critical(self, "Fund Grid", f"Could not load this fund:\n{e}")
            return

        self.staged.clear()
        self.refused.clear()
        self.edit_history.clear()
        self.loaded = True
        self._render()
        self._refresh_status()

    def _apply_filter(self):
        """Hide rows that do not match the search box, without reloading."""
        text = self.search.text().strip().lower()
        for row, member in enumerate(self.rows):
            match = (not text or text in member.name.lower()
                     or text in (member.pf_no or "").lower())
            self.table.setRowHidden(row, not match)

    # ------------------------------------------------------------------ #
    # Drawing
    # ------------------------------------------------------------------ #

    def _columns(self):
        return (self.backend.lead_columns
                + [(model.month_label(m), m) for m in self.months]
                + self.backend.tail_columns)

    def _month_column(self, index):
        """The month key at a table column, or None for the fixed columns."""
        first = len(self.backend.lead_columns)
        if first <= index < first + len(self.months):
            return self.months[index - first]
        return None

    def _render(self):
        self.table.blockSignals(True)
        try:
            columns = self._columns()
            self.rows = list(self.grid.members) if self.grid else []
            self.table.clear()
            self.table.setColumnCount(len(columns))
            self.table.setHorizontalHeaderLabels([label for label, _key in columns])
            self.table.setRowCount(len(self.rows) + (1 if self.rows else 0))

            self._defer_totals = True
            try:
                for row, member in enumerate(self.rows):
                    self._draw_member(row, member)
            finally:
                self._defer_totals = False
            if self.rows:
                self._draw_totals(len(self.rows))

            self.table.resizeColumnsToContents()
            # resizeColumnsToContents fits the text exactly, which clips a name
            # by a pixel or two once the bold header sets the width.
            header = self.table.horizontalHeader()
            for column in range(self.table.columnCount()):
                header.resizeSection(column, header.sectionSize(column) + 10)
            self._fill_width()
        finally:
            self.table.blockSignals(False)
        self._apply_filter()

    def resizeEvent(self, event):
        """Re-fill the width when the window is resized.

        Drawing happens before the dialog reaches its final size, so the slack
        computed then is not the slack the user ends up looking at.
        """
        super().resizeEvent(event)
        if self.rows:
            self._fill_width()

    def _fill_width(self):
        """Any slack goes to the member column, so the table fills its window."""
        header = self.table.horizontalHeader()
        used = sum(header.sectionSize(c) for c in range(self.table.columnCount()))
        slack = self.table.viewport().width() - used
        if slack > 0:
            header.resizeSection(0, header.sectionSize(0) + slack)

    def _draw_member(self, row, member):
        note = self.backend.row_note(member, self.blocked)
        lead = self.backend.lead_values(member)

        label_item = QTableWidgetItem(str(lead[0]))
        if note:
            label_item.setForeground(QColor(self.theme.get_color("text_secondary")))
            label_item.setToolTip(note)
        self._place(row, 0, label_item, editable=False)
        for offset, value in enumerate(lead[1:], start=1):
            item = (self._figure(value) if isinstance(value, (int, float))
                    else QTableWidgetItem(str(value)))
            self._place(row, offset, item, editable=False)

        first = len(self.backend.lead_columns)
        for offset, month in enumerate(self.months):
            self._place(row, first + offset, self._cell_item(member, month),
                        editable=self._editable(member, month))

        tail = self.backend.tail_values(member, self.months)
        base = self.table.columnCount() - len(tail)
        for offset, value in enumerate(tail):
            self._place(row, base + offset, self._figure(value), editable=False)

    def _editable(self, member, month):
        """A cell takes typing only when the backend has no objection to it."""
        if getattr(member, "excluded", False):
            return False
        return self._refusal(member, month) is None

    def _refusal(self, member, month):
        """Why this cell cannot be typed into, or None.

        An excluded row answers first: it is shown only so it can be put back,
        and editing it would quietly contradict the decision to leave it out.

        The fund grid answers this from the cell alone; the loan grid has more
        to say (a suspension, a month the loan is not due for yet), so it is
        asked when it can answer.
        """
        if getattr(member, "excluded", False):
            return (f"{member.name} is excluded from this ledger. Right-click "
                    f"the row to include them again.")
        asked = getattr(self.backend, "cell_refusal", None)
        if asked is not None:
            return asked(self.grid, member, month)
        return (None if member.cell(month).editable else
                "This month has several transactions.")

    def _cell_item(self, member, month):
        """One month's item, carrying its staged state and its explanation."""
        key = (member.key, month)
        cell = member.cell(month)

        if key in self.staged:
            change = self.staged[key]
            text = ("—" if change.kind == model.ChangeKind.DELETE
                    else f"{change.amount:,.0f}")
        else:
            text = _money(cell.net)
        locked = self._refusal(member, month)
        if locked and cell.rows:
            text += MIXED_MARK

        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if locked:
            item.setToolTip(locked)
            item.setForeground(QColor(self.theme.get_color("text_secondary")))
        elif key in self.refused:
            item.setToolTip(self.refused[key])
            item.setBackground(QColor(self.theme.get_color("danger_bg")))
            item.setForeground(QColor(self.theme.get_color("danger")))
        elif key in self.staged:
            item.setBackground(QColor(self.theme.get_color("warning_bg")))
            item.setForeground(QColor(self.theme.get_color("warning")))
            item.setToolTip("Unsaved — press Save Changes to write it.")
        elif cell.state == model.CellState.SINGLE:
            row = cell.rows[0]
            item.setToolTip(f"{row['transaction_type']} of "
                            f"{float(row['amount'] or 0):,.0f} on {str(row['date'])[:10]}"
                            + (f"\n{row['notes']}" if row.get("notes") else ""))
        return item

    def _figure(self, value):
        item = QTableWidgetItem(_money(value))
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(QColor(self.theme.get_color("text_secondary")))
        return item

    def _place(self, row, column, item, editable):
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table.setItem(row, column, item)

    def _total_figure(self, value):
        item = self._figure(value)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        item.setForeground(QColor(self.theme.get_color("text_primary")))
        return item

    def _draw_totals(self, row):
        """A bold last row: what each month came to across everybody shown."""
        label = QTableWidgetItem("TOTAL")
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        self._place(row, 0, label, editable=False)
        self._place(row, 1, QTableWidgetItem(""), editable=False)
        for column in range(2, len(self.backend.lead_columns)):
            values = [self.backend.lead_values(m)[column] for m in self.rows]
            item = (self._total_figure(sum(values))
                    if all(isinstance(v, (int, float)) for v in values)
                    else QTableWidgetItem(""))
            self._place(row, column, item, editable=False)
        tails = [self.backend.tail_values(m, self.months) for m in self.rows]
        base = self.table.columnCount() - len(self.backend.tail_columns)
        for offset in range(1, len(self.backend.tail_columns)):
            self._place(row, base + offset,
                        self._total_figure(sum(t[offset] for t in tails)),
                        editable=False)
        self._redraw_totals()

    # ------------------------------------------------------------------ #
    # Editing
    # ------------------------------------------------------------------ #

    def _item_changed(self, item):
        if self._filling:
            return
        month = self._month_column(item.column())
        if month is None or item.row() >= len(self.rows):
            return
        self._stage_text(item.row(), month, item.text(), record_history=True)

    def _stage_text(self, row, month, text, record_history=False):
        """Put one typed value into the staging map and redraw its cell."""
        member = self.rows[row]
        key = (member.key, month)
        previous = self.staged.get(key)

        change, problem = self.backend.stage(self.grid, member, month, text)
        if problem:
            self._set_status(f"{member.name}, {model.month_label(month)}: {problem}",
                             "danger")
            self._redraw_cell(row, month)
            return False

        if record_history:
            self.edit_history.append((row, month, previous, key in self.staged))
        self.refused.pop(key, None)
        if change is None:
            self.staged.pop(key, None)
        else:
            self.staged[key] = change
        self._redraw_cell(row, month)
        self._refresh_status()
        return True

    def _redraw_cell(self, row, month):
        self._filling = True
        try:
            member = self.rows[row]
            column = len(self.backend.lead_columns) + self.months.index(month)
            self._place(row, column, self._cell_item(member, month),
                        editable=member.cell(month).editable)
            self._redraw_derived(row)
        finally:
            self._filling = False

    def _effective(self, member, month):
        """What the cell will be worth once the staged edits are saved.

        Every derived figure on the screen reads through this, so the Period
        column and the TOTAL row move with an edit instead of contradicting it —
        the point of staging is to *see* the consequence before committing to it.
        """
        change = self.staged.get((member.key, month))
        if change is None:
            return member.cell(month).net
        return 0.0 if change.kind == model.ChangeKind.DELETE else change.amount

    def _redraw_derived(self, row):
        """Keep the member's Period column, and the totals, honest."""
        member = self.rows[row]
        self._place(row, self._period_column(),
                    self._figure(round(sum(self._effective(member, m)
                                           for m in self.months), 2)),
                    editable=False)
        if not self._defer_totals:
            self._redraw_totals()

    def _period_column(self):
        """The first trailing column — the one that tracks staged edits."""
        return self.table.columnCount() - len(self.backend.tail_columns)

    def _redraw_totals(self):
        """Recompute the bold last row from the staged-aware figures."""
        if not self.rows:
            return
        row = len(self.rows)
        for offset, month in enumerate(self.months):
            total = sum(self._effective(m, month) for m in self.rows)
            self._place(row, len(self.backend.lead_columns) + offset,
                        self._total_figure(total), editable=False)
        period = sum(self._effective(m, month)
                     for m in self.rows for month in self.months)
        self._place(row, self._period_column(),
                    self._total_figure(period), editable=False)

    def _selected_month_cells(self):
        """(row, month) for every selected editable month cell, in reading order."""
        out = []
        for index in sorted(self.table.selectedIndexes(),
                            key=lambda i: (i.row(), i.column())):
            month = self._month_column(index.column())
            if month is not None and index.row() < len(self.rows):
                out.append((index.row(), month))
        return out

    def _clear_selection(self):
        cells = self._selected_month_cells()
        if not cells:
            return
        with self._batched():
            for row, month in cells:
                self._stage_text(row, month, "", record_history=True)
        self._set_status(f"Cleared {len(cells)} cell(s) — not yet saved.", "warning")

    @contextmanager
    def _batched(self):
        """Redraw the totals once for a multi-cell operation, not per cell.

        A paste of two hundred figures would otherwise recompute every column
        total two hundred times.
        """
        self._defer_totals = True
        try:
            yield
        finally:
            self._defer_totals = False
            self._redraw_totals()

    def _undo_edit(self):
        """Take back the last staged edit (Ctrl+Z), one at a time."""
        if not self.edit_history:
            self._set_status("Nothing to undo.", "muted")
            return
        row, month, previous, existed = self.edit_history.pop()
        key = (self.rows[row].key, month)
        if existed and previous is not None:
            self.staged[key] = previous
        else:
            self.staged.pop(key, None)
        self._redraw_cell(row, month)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Clipboard
    # ------------------------------------------------------------------ #

    def _copy(self):
        """Selection to the clipboard as TSV — the format Excel pastes."""
        indexes = sorted(self.table.selectedIndexes(),
                         key=lambda i: (i.row(), i.column()))
        if not indexes:
            return
        grid = {}
        for index in indexes:
            item = self.table.item(index.row(), index.column())
            grid.setdefault(index.row(), {})[index.column()] = (
                item.text().replace(MIXED_MARK, "").replace("—", "") if item else "")
        lines = []
        for row in sorted(grid):
            columns = sorted(grid[row])
            lines.append("\t".join(grid[row][c] for c in columns))
        QApplication.clipboard().setText("\n".join(lines))
        self._set_status(f"Copied {len(indexes)} cell(s).", "muted")

    def _paste(self):
        """Paste a TSV block from the top-left of the selection.

        Anything past the edge of the grid is dropped rather than wrapping, and
        cells that will not take a value are reported together at the end — a
        paste of two hundred figures must not become two hundred dialogs.
        """
        text = QApplication.clipboard().text()
        if not text.strip():
            return
        cells = self._selected_month_cells()
        if not cells:
            self._set_status("Select a cell to paste into first.", "warning")
            return

        anchor_row, anchor_month = cells[0]
        start = self.months.index(anchor_month)
        pasted = skipped = 0
        with self._batched():
            for row_offset, line in enumerate(text.replace("\r\n", "\n").split("\n")):
                if not line.strip():
                    continue
                row = anchor_row + row_offset
                if row >= len(self.rows):
                    skipped += len([v for v in line.split("\t") if v.strip()])
                    continue
                for column_offset, value in enumerate(line.split("\t")):
                    index = start + column_offset
                    if index >= len(self.months):
                        skipped += 1 if value.strip() else 0
                        continue
                    if self._stage_text(row, self.months[index], value):
                        pasted += 1
                    else:
                        skipped += 1

        note = f"Pasted {pasted} cell(s)."
        if skipped:
            note += f" {skipped} did not fit or could not be set."
        self._set_status(note, "warning" if skipped else "muted")

    # ------------------------------------------------------------------ #
    # Fill forward
    # ------------------------------------------------------------------ #

    def _fill_forward(self):
        """Stage each member's usual amount into every empty month in range."""
        if not self.grid or not self.grid.members:
            return
        chosen = {self.rows[row] for row, _month in self._selected_month_cells()}
        targets = sorted(chosen, key=lambda m: m.name) or list(self.grid.members)
        scope = ("the selected members" if chosen else "every member shown")

        plan = self.backend.plan_fill(self.db, self.grid, targets, self.months)
        if not plan.changes:
            reasons = "\n".join(f"  • {reason}" for _member, reason in plan.skipped[:8])
            excluded = "\n".join(f"  • {note}" for note in plan.notes())
            QMessageBox.information(
                self, "Fill Forward",
                f"Nothing to fill for {scope}.\n\n{reasons}"
                + (f"\n{excluded}" if excluded else ""))
            return

        # What was left out matters as much as what was proposed: a fill that
        # silently skipped six months looks identical to one that had nothing
        # to skip.
        detail = "".join(f"\n  • {note}" for note in plan.notes())
        if plan.skipped:
            detail += (f"\n  • {len(plan.skipped)} member(s) skipped — "
                       f"e.g. {plan.skipped[0][1]}")
        answer = QMessageBox.question(
            self, "Fill Forward",
            f"Propose {len(plan.changes)} "
            f"entr{'y' if len(plan.changes) == 1 else 'ies'} across "
            f"{plan.members} member(s), using each member's usual "
            f"{self.spec.noun}?\n\n"
            f"Only months that were missed are filled — never a month before "
            f"the member's account began, never a month still to come, and "
            f"never one that already has an entry.\n\n"
            f"Nothing is written until you press Save Changes."
            + (f"\n{detail}" if detail else ""),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        changes = plan.changes

        rows_by_id = {m.key: row for row, m in enumerate(self.rows)}
        for change in changes:
            self.staged[(change.key, change.month)] = change
        self.edit_history.clear()       # a fill is undone with Discard, not Ctrl+Z
        with self._batched():
            for change in changes:
                row = rows_by_id.get(change.key)
                if row is not None:
                    self._redraw_cell(row, change.month)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    # Saving
    # ------------------------------------------------------------------ #

    def _save(self):
        if not self.staged:
            return
        changes = list(self.staged.values())
        progress = QProgressDialog(f"Saving {len(changes)} change(s)…", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.show()
        QApplication.processEvents()
        try:
            result = self.backend.apply(self.db, self.engine, changes)
        except Exception as e:                  # noqa: BLE001 - report, don't crash
            progress.close()
            logger.exception("Saving the %s grid failed", self.fund)
            QMessageBox.critical(
                self, "Save Failed",
                f"Nothing was saved:\n{e}\n\nThe journal is unchanged.")
            return
        finally:
            progress.close()

        self.last_batch = result if result.applied else None
        self.undo_save_btn.setEnabled(bool(self.last_batch))

        # Applied cells go back to being plain data; refused ones stay staged
        # and carry the reason, so the operator can fix or abandon them.
        self.refused = {(r.change.key, r.change.month): r.reason
                        for r in result.refused}
        keep = set(self.refused)
        self.staged = {k: v for k, v in self.staged.items() if k in keep}
        self.edit_history.clear()

        self._reload_preserving_staged()

        if result.refused:
            listed = "\n".join(
                f"  • {self._name_of(r.change.key)}, "
                f"{model.month_label(r.change.month)}: {r.reason}"
                for r in result.refused[:10])
            more = (f"\n  … and {len(result.refused) - 10} more"
                    if len(result.refused) > 10 else "")
            QMessageBox.warning(
                self, "Saved With Refusals",
                f"{result.summary()}.\n\nThese were not written:\n{listed}{more}\n\n"
                f"They are still marked in the grid.")
        else:
            self._set_status(f"Saved — {result.summary()}.", "success")

    def _reload_preserving_staged(self):
        """Re-read after a save, keeping the cells that were refused.

        The refused changes are re-based onto the figures just read: the
        operator's number stays staged, but it now sits on top of what the
        journal actually holds. Without that, a cell refused because somebody
        else had edited it would be refused forever — its recorded "before"
        would never match again, however many times Save was pressed.
        """
        staged, refused = dict(self.staged), dict(self.refused)
        self.reload()
        for key, change in staged.items():
            row_key, month = key
            member = next((m for m in self.rows if m.key == row_key), None)
            if member is None:
                continue
            cell = member.cell(month)
            if not cell.editable:
                continue                # became a multi-transaction month; let it go
            rebased = replace(change, before=cell.fingerprint(),
                              row_id=cell.row_id if cell.rows else None)
            if rebased.kind == model.ChangeKind.DELETE and not cell.rows:
                continue                # already gone; nothing left to delete
            if rebased.kind == model.ChangeKind.UPDATE and not cell.rows:
                rebased = replace(rebased, kind=model.ChangeKind.INSERT, row_id=None)
            elif rebased.kind == model.ChangeKind.INSERT and cell.rows:
                rebased = replace(rebased, kind=model.ChangeKind.UPDATE,
                                  row_id=cell.row_id)
            self.staged[key] = rebased
            self.refused[key] = refused.get(key) or "Not saved."
        self._render()
        self._refresh_status()

    def _undo_last_save(self):
        if not self.last_batch:
            return
        answer = QMessageBox.question(
            self, "Undo Last Save",
            f"Put back the {len(self.last_batch.applied)} change(s) from the last "
            f"save?\n\nThis reverses them exactly, including anything the grid "
            f"deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.revert(self.db, self.engine, self.last_batch)
        except Exception as e:                  # noqa: BLE001 - report, don't crash
            logger.exception("Reverting the %s grid batch failed", self.fund)
            QMessageBox.critical(self, "Undo Failed", f"Could not undo:\n{e}")
            return
        self.last_batch = None
        self.undo_save_btn.setEnabled(False)
        self.reload()
        self._set_status("The last save was undone.", "warning")

    def _discard(self):
        if not self.staged:
            return
        self.staged.clear()
        self.refused.clear()
        self.edit_history.clear()
        self._render()
        self._refresh_status()
        self._set_status("Unsaved changes discarded.", "muted")

    # ------------------------------------------------------------------ #
    # Status line
    # ------------------------------------------------------------------ #

    def _name_of(self, member_id):
        member = next((m for m in self.rows if m.key == member_id), None)
        return member.label if member else f"Row {member_id}"

    def _set_status(self, text, tone="muted"):
        colour = {"danger": "danger", "warning": "warning", "success": "success"}.get(tone)
        self.status.setStyleSheet(
            f"color: {self.theme.get_color(colour)}; font-size: 12px; font-weight: bold;"
            if colour else self.theme.hint_css(12))
        self.status.setText(text)

    def _refresh_status(self):
        self.save_btn.setEnabled(bool(self.staged))
        self.discard_btn.setEnabled(bool(self.staged))
        if not self.staged:
            if not (self.grid and self.rows):
                self._set_status("Nothing to show.")
                return
            hidden = (f"  {self.hidden_members} past member(s) hidden — "
                      f"tick “Include past members” to see them."
                      if self.hidden_members else "")
            self._set_status(
                f"{len(self.rows):,} member(s) · {len(self.months)} month(s) · "
                f"{self.grid.grand_total():,.0f} moved in this period.{hidden}")
            return
        note = f"{len(self.staged)} unsaved change(s)."
        if self.refused:
            note += f" {len(self.refused)} refused — hover a red cell for the reason."
        self._set_status(note, "danger" if self.refused else "warning")

    # ------------------------------------------------------------------ #
    # Context menu and navigation
    # ------------------------------------------------------------------ #

    def _double_clicked(self, item):
        """A locked cell explains itself by offering the screen that can edit it."""
        month = self._month_column(item.column())
        if month is None or item.row() >= len(self.rows):
            return
        member = self.rows[item.row()]
        if member.cell(month).editable:
            return                              # normal edit; let Qt handle it
        answer = QMessageBox.question(
            self, "Several Transactions",
            f"{model.month_label(month)} holds {len(member.cell(month).rows)} "
            f"transactions for {member.name}, so it cannot be edited as one "
            f"number.\n\nOpen {member.name}'s ledger to work on them?",
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Open and self.on_open_member:
            self.on_open_member(member.member_id, member.name)

    def _context_menu(self, position):
        row = self.table.rowAt(position.y())
        if row < 0 or row >= len(self.rows):
            return
        member = self.rows[row]
        menu = QMenu(self)
        open_action = menu.addAction(f"Open {member.name}'s ledger")
        clear_action = menu.addAction("Clear selected cells")
        menu.addSeparator()
        excluded = getattr(member, "excluded", False)
        rows = self._selected_rows() or [member]
        if len(rows) > 1:
            verb = f"{len(rows)} selected members"
        else:
            verb = member.name
        toggle = menu.addAction(
            f"Include {verb} in {self.backend.label}" if excluded
            else f"Exclude {verb} from {self.backend.label}")
        chosen = menu.exec(self.table.viewport().mapToGlobal(position))
        if chosen is open_action and self.on_open_member:
            self.on_open_member(member.member_id, member.name)
        elif chosen is clear_action:
            self._clear_selection()
        elif chosen is toggle:
            self._set_excluded(rows, not excluded)

    def _selected_rows(self):
        """The distinct member rows touched by the selection."""
        seen, out = set(), []
        for index in self.table.selectedIndexes():
            if index.row() < len(self.rows) and index.row() not in seen:
                seen.add(index.row())
                out.append(self.rows[index.row()])
        return out

    def _set_excluded(self, rows, excluded):
        """Take rows out of this ledger's grid, or put them back.

        A display decision, and only that: nothing is deleted, no history moves,
        and the member's own screen is untouched. What it does change is what
        Fill Forward proposes, which is the point — a member who does not save
        should not be offered a savings deposit every month.
        """
        if self.staged and not self.window_ref.confirm_discard("change who is included"):
            return
        for row in rows:
            self.db.members.set_excluded(row.member_id, self.backend.fund, excluded)
        self.db.conn.commit()
        if excluded and not self.show_excluded.isChecked():
            note = (f"{len(rows)} row(s) excluded from {self.backend.label}. "
                    f"Tick “Show excluded” to see them.")
        else:
            note = f"{len(rows)} row(s) {'excluded' if excluded else 'included'}."
        self.reload()
        self._set_status(note, "warning" if excluded else "success")

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    def _dataframe(self):
        """The grid as shown, for export."""
        import pandas as pd

        headers = [label for label, _key in self._columns()]
        lead = [label for label, _key in self.backend.lead_columns]
        tail = [label for label, _key in self.backend.tail_columns]
        records = []
        for row, member in enumerate(self.rows):
            if self.table.isRowHidden(row):
                continue
            # Built from the backend's own columns, not a fixed set: the loan
            # grid's leading columns are the loan and its due month, not a PF
            # number and an opening balance.
            record = dict(zip(lead, self.backend.lead_values(member)))
            for month in self.months:
                record[model.month_label(month)] = member.cell(month).net
            record.update(zip(tail, self.backend.tail_values(member, self.months)))
            records.append(record)
        return pd.DataFrame(records, columns=headers)

    def _export(self):
        from src.reports import ReportGenerator
        from src.views.file_actions import report_saved

        frame = self._dataframe()
        if frame.empty:
            QMessageBox.information(self, "Export", "There is nothing to export.")
            return
        if self.staged:
            QMessageBox.information(
                self, "Export",
                "The export shows the saved figures. Unsaved changes are not "
                "included — save them first if they should be.")

        stamp = datetime.now().strftime("%Y%m%d")
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export Fund Grid",
            f"{self.spec.label.replace(' / ', '_').replace(' ', '_')}_Grid_{stamp}.xlsx",
            "Excel Files (*.xlsx);;CSV Files (*.csv);;PDF Files (*.pdf)")
        if not path:
            return

        window = self.window()
        getter = getattr(window, "printer_view_getter", None)
        generator = ReportGenerator(self.db, printer_view_getter=getter)
        title = f"{self.spec.label} — monthly grid"
        subtitle = (f"{model.month_label(self.months[0])} to "
                    f"{model.month_label(self.months[-1])}" if self.months else "")
        if path.endswith(".csv"):
            ok, message = generator._export_to_csv(frame, path)
        elif path.endswith(".pdf"):
            ok, message = generator._export_to_pdf_titled(frame, path, title, subtitle)
        else:
            ok, message = generator._export_to_excel(frame, path,
                                                     sheet_name="Grid",
                                                     has_total_row=False)
        if ok:
            report_saved(self, "Fund Grid", path, what="grid")
        else:
            QMessageBox.critical(self, "Export Failed", message)
