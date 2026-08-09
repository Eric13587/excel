"""Suspensions drawn in the ledger table where their effect is visible.

A suspension shows up in a loan's history as a gap in the dates and nothing
else, which reads as missing data rather than a decision somebody took. The
band names it, and offers the way out at the point the question occurs.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import QApplication, QMainWindow

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.views.ledger import LedgerView


class _Stub(QMainWindow):
    last_operation_date = None
    last_report_range = None

    def show_dashboard(self):
        pass


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qt_app):
    _Stub.last_operation_date = QDate.currentDate()
    _Stub.last_report_range = (QDate.currentDate().addMonths(-12), QDate.currentDate())
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "v.db"), auto_backup=False)
    engine = LoanEngine(db)
    ind = db.add_individual("M", "07", "m@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                       "2025-01-01", "2025-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-001", 100000, 0, 100000, "issue")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-04-01")
    v = LedgerView(_Stub(), db)
    v.current_individual_id = ind
    yield v, db, ind, loan_id
    db.close()


def _bands(table):
    return [r for r in range(table.rowCount()) if table.columnSpan(r, 0) > 1]


def test_no_suspension_means_no_band(view):
    v, _db, _ind, _lid = view
    v.refresh_table()
    assert _bands(v.tables["L-001"]) == []


def test_a_suspension_draws_a_band_where_the_gap_is(view):
    v, db, _ind, lid = view
    db.record_suspension(lid, "2025-05-01", "2025-08-01", today="2026-01-01")
    v.refresh_table()
    table = v.tables["L-001"]

    bands = _bands(table)
    assert len(bands) == 1
    row = bands[0]
    text = table.item(row, 0).text()
    assert "2025-05-01" in text and "2025-08-01" in text
    assert "3 month(s) skipped" in text
    # it sits after everything dated before it
    assert table.item(row - 1, 0).text() < "2025-05-01"


def test_the_band_offers_a_way_out(view):
    v, db, _ind, lid = view
    db.record_suspension(lid, "2025-05-01", "2025-08-01", today="2026-01-01")
    v.refresh_table()
    table = v.tables["L-001"]
    row = _bands(table)[0]
    assert table.cellWidget(row, 9).text() == "Clear"


def test_the_band_is_not_mistaken_for_a_transaction(view):
    """It must not be editable, and on_item_changed must ignore it — which it
    does, because a band has no id cell."""
    v, db, _ind, lid = view
    db.record_suspension(lid, "2025-05-01", "2025-08-01", today="2026-01-01")
    v.refresh_table()
    table = v.tables["L-001"]
    row = _bands(table)[0]
    from PyQt6.QtCore import Qt
    assert not (table.item(row, 0).flags() & Qt.ItemFlag.ItemIsEditable)
    assert table.item(row, 2) is None


def test_an_open_ended_suspension_says_so(view):
    v, db, _ind, lid = view
    db.suspend_loan(lid, until_date=None, start_date="2025-05-01")
    v.refresh_table()
    text = v.tables["L-001"].item(_bands(v.tables["L-001"])[0], 0).text()
    assert "no end date" in text
    assert "currently suspended" in text


def test_several_suspensions_each_get_their_own_band(view):
    v, db, _ind, lid = view
    db.record_suspension(lid, "2025-02-10", "2025-03-10", today="2026-01-01")
    db.record_suspension(lid, "2025-05-01", "2025-07-01", today="2026-01-01")
    v.refresh_table()
    table = v.tables["L-001"]
    assert len(_bands(table)) == 2
    texts = [table.item(r, 0).text() for r in _bands(table)]
    assert any("2025-02-10" in t for t in texts)
    assert any("2025-05-01" in t for t in texts)


def test_clearing_removes_the_band(view):
    """The service call behind the button; the dialog itself is not headless."""
    v, db, ind, lid = view
    db.record_suspension(lid, "2025-05-01", "2025-08-01", today="2026-01-01")
    v.refresh_table()
    assert len(_bands(v.tables["L-001"])) == 1

    span = db.get_loan_suspensions_for_loan(lid)[0]
    v.engine.loan_service.remove_suspension(ind, "L-001", span["id"])
    v.refresh_table()
    assert _bands(v.tables["L-001"]) == []


def test_months_between_counts_whole_months():
    assert LedgerView._months_between("2025-01-01", "2025-04-01") == 3
    assert LedgerView._months_between("2025-01-01", None) is None
    assert LedgerView._months_between("nonsense", "2025-04-01") is None
