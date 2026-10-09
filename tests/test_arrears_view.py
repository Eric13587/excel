"""The arrears drill-down on the dashboard.

The KPI tile is now a door: it opens onto the loans behind the number, with the
filters and the print button somebody chasing arrears actually needs. These
tests drive the real dialog rather than a stand-in, so the wiring — tile to
dialog, filters to table, table to export — is what is checked.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QLineEdit, QMainWindow, QMessageBox,
                             QPushButton, QTableWidget)

from src.database import DatabaseManager
from src.reports import ReportGenerator
from src.views.dashboard import Dashboard, _ClickableFrame


class _Stub(QMainWindow):
    def show_ledger(self, *_a, **_kw):
        pass


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dash(qt_app):
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "d.db"), auto_backup=False)
    d = Dashboard(_Stub(), db)
    yield d, db
    db.close()


def _overdue_loan(db, name, ref, due, installment=5750, balance=100000):
    ind = db.add_individual(name, "0700", f"{name}@x")
    db.add_loan_record(ind, ref, balance, balance, balance, installment, 750,
                       "2020-01-01", due)
    return ind, db.get_loan_by_ref(ind, ref)["id"]


def _open(dash_pair, monkeypatch):
    """Open the arrears dialog without blocking, returning it."""
    opened = {}

    def fake_exec(dialog):
        opened["dialog"] = dialog
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    dash_pair[0].show_arrears_dialog()
    return opened.get("dialog")


def _table(dialog):
    return dialog.findChild(QTableWidget)


def _rows(dialog):
    table = _table(dialog)
    return [table.item(r, 0).text() for r in range(table.rowCount())]


# --------------------------------------------------------------------------- #
# The tile
# --------------------------------------------------------------------------- #

def test_the_arrears_tile_is_the_only_pressable_one(dash):
    d, _db = dash
    clickable = d.summary_frame.findChildren(_ClickableFrame)
    assert len(clickable) == 1
    assert clickable[0] is d.arrears_card


def test_the_tile_looks_pressable(dash):
    d, _db = dash
    assert d.arrears_card.cursor().shape() == Qt.CursorShape.PointingHandCursor
    assert d.arrears_card.property("clickable") is True
    assert "arrears" in d.arrears_card.toolTip().lower()


# --------------------------------------------------------------------------- #
# Opening it
# --------------------------------------------------------------------------- #

def test_an_empty_portfolio_says_so_rather_than_opening_a_blank_table(dash, monkeypatch):
    d, db = dash
    db.add_individual("Punctual", "0700", "p@x")
    said = {}
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda _p, _t, text, *a, **k: said.setdefault("text", text)))

    assert _open(dash, monkeypatch) is None
    assert "up to date" in said["text"]


def test_every_overdue_loan_is_listed(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")
    _overdue_loan(db, "Ben", "L-001", "2020-01-01")

    dialog = _open(dash, monkeypatch)
    assert sorted(_rows(dialog)) == ["Ann", "Ben"]


def test_the_table_carries_every_reported_column(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")

    table = _table(_open(dash, monkeypatch))
    headers = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    assert headers == [label for _key, label in ReportGenerator.ARREARS_COLUMNS]


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def test_the_search_box_narrows_the_list(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")
    _overdue_loan(db, "Ben", "L-001", "2020-01-01")

    dialog = _open(dash, monkeypatch)
    dialog.findChild(QLineEdit).setText("ann")
    assert _rows(dialog) == ["Ann"]


def test_suspended_loans_can_be_taken_out(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")
    _ind, loan_id = _overdue_loan(db, "Held", "L-001", "2020-01-01")
    db.record_suspension(loan_id, "2020-01-01", None)

    dialog = _open(dash, monkeypatch)
    assert sorted(_rows(dialog)) == ["Ann", "Held"]

    dialog.findChild(QCheckBox).setChecked(True)
    assert _rows(dialog) == ["Ann"]


def test_the_classification_filter_selects_one_band(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ancient", "L-001", "2019-01-01")                 # Loss
    _overdue_loan(db, "Recent", "L-001",
                  (date.today() - timedelta(days=3)).isoformat())       # Performing

    dialog = _open(dash, monkeypatch)
    combo = dialog.findChild(QComboBox)
    combo.setCurrentIndex(combo.findData("Loss"))
    assert _rows(dialog) == ["Ancient"]


def test_filtering_everything_out_leaves_an_honest_empty_table(dash, monkeypatch):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")

    dialog = _open(dash, monkeypatch)
    dialog.findChild(QLineEdit).setText("nobody by that name")
    assert _rows(dialog) == []


# --------------------------------------------------------------------------- #
# Printing what is on screen
# --------------------------------------------------------------------------- #

def test_the_export_prints_the_filtered_list(dash, monkeypatch, tmp_path):
    import pandas as pd

    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")
    _overdue_loan(db, "Ben", "L-001", "2020-01-01")

    dialog = _open(dash, monkeypatch)
    dialog.findChild(QLineEdit).setText("ben")

    out = tmp_path / "arrears.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    monkeypatch.setattr("src.views.file_actions.open_in_default_app",
                        lambda _path: None)
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

    # The button the user presses, not the method behind it.
    export = next(b for b in dialog.findChildren(QPushButton) if "Print" in b.text())
    export.click()

    written = pd.read_csv(out)
    assert list(written["Member"]) == ["Ben", "TOTAL — 1 member(s), 1 loan(s)"]


def test_money_columns_sort_as_numbers_not_text(dash, monkeypatch):
    """"2,787" must not sort before "230" the way strings do."""
    d, db = dash
    _overdue_loan(db, "Big", "L-001", "2026-01-01", balance=900000)
    _overdue_loan(db, "Small", "L-001", "2026-01-01", balance=80000)

    dialog = _open(dash, monkeypatch)
    table = _table(dialog)
    keys = [key for key, _label in ReportGenerator.ARREARS_COLUMNS]

    table.sortItems(keys.index("balance"), Qt.SortOrder.AscendingOrder)
    assert _rows(dialog) == ["Small", "Big"]
    table.sortItems(keys.index("balance"), Qt.SortOrder.DescendingOrder)
    assert _rows(dialog) == ["Big", "Small"]


def test_the_export_follows_the_order_on_screen(dash, monkeypatch, tmp_path):
    """Sorting the table then printing must not silently re-sort the file."""
    import pandas as pd

    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2019-01-01")     # worst — listed first
    _overdue_loan(db, "Ben", "L-001", "2026-01-01")

    dialog = _open(dash, monkeypatch)
    assert _rows(dialog) == ["Ann", "Ben"]

    # Sort by "Days" ascending — the reverse of the default worst-first order.
    table = _table(dialog)
    days_column = [key for key, _label in ReportGenerator.ARREARS_COLUMNS].index("days")
    table.sortItems(days_column, Qt.SortOrder.AscendingOrder)
    assert _rows(dialog) == ["Ben", "Ann"]

    out = tmp_path / "sorted.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    monkeypatch.setattr("src.views.file_actions.open_in_default_app", lambda _p: None)
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    next(b for b in dialog.findChildren(QPushButton) if "Print" in b.text()).click()

    assert list(pd.read_csv(out)["Member"])[:2] == ["Ben", "Ann"]


def test_cancelling_the_save_dialog_writes_nothing(dash, monkeypatch, tmp_path):
    d, db = dash
    _overdue_loan(db, "Ann", "L-001", "2020-01-01")
    dialog = _open(dash, monkeypatch)

    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: ("", "")))
    d._export_arrears(dialog, [{"individual_id": 1}], "2026-06-15")
    assert list(tmp_path.iterdir()) == []
