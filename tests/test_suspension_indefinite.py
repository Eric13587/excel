"""Suspending a loan with no end date, until somebody resumes it.

A fixed-period suspension is a plan the software can finish by itself: catch-up
auto-resumes at the boundary. An open-ended one is a hold of unknown length —
payroll stopped, a member is on unpaid leave — and nothing but a person ends it.
The storage for it already existed (``loans.suspend_until`` is nullable and
every consumer reads NULL as "no end date"); these tests pin the behaviour the
UI now exposes.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.exceptions import LoanSuspendedError


@pytest.fixture
def loan():
    """A 100k loan issued 2025-01-01, caught up to 2025-05-01 (4 deductions)."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "i.db"), auto_backup=False)
    engine = LoanEngine(db)
    ind = db.add_individual("M", "07", "m@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                       "2025-01-01", "2025-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-001", 100000, 0,
                       100000, "issue")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-05-01")
    yield db, engine, ind, loan_id
    db.close()


def _repayments(db, ind):
    return [r[0] for r in db.conn.execute(
        "SELECT date FROM ledger WHERE individual_id=? AND loan_id='L-001' "
        "AND event_type='Repayment' ORDER BY date", (ind,))]


# --------------------------------------------------------------------------- #
# Recording it
# --------------------------------------------------------------------------- #

def test_no_end_date_is_stored_as_a_live_open_span(loan):
    db, _engine, _ind, loan_id = loan
    assert db.record_suspension(loan_id, "2025-06-01", None) == "active"

    row = db.get_loan_by_ref(_ind, "L-001")
    assert row["is_suspended"] == 1
    assert row["suspend_until"] is None

    span = db.loans.open_suspension(loan_id)
    assert span["start_date"] == "2025-06-01"
    assert span["suspend_until"] is None
    assert span["status"] == "active"


def test_an_open_span_is_never_filed_as_history(loan):
    """A past start date with no end is still in force — not a closed record."""
    db, _engine, _ind, loan_id = loan
    assert db.record_suspension(loan_id, "2020-01-01", None,
                                today="2025-06-01") == "active"
    assert db.get_loan_by_ref(_ind, "L-001")["is_suspended"] == 1


# --------------------------------------------------------------------------- #
# What it does to deductions
# --------------------------------------------------------------------------- #

def test_catch_up_never_deducts_while_it_is_open(loan):
    db, engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")

    before = _repayments(db, ind)
    assert engine.loan_service.catch_up_loan(ind, "L-001",
                                             target_date="2026-06-01") == 0
    assert _repayments(db, ind) == before


def test_the_term_extends_for_every_month_held(loan):
    db, engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-12-01")
    # Due date walked forward without a single posting behind it.
    assert db.get_loan_by_ref(ind, "L-001")["next_due_date"] > "2025-06-01"


def test_a_single_deduction_is_refused_and_says_why(loan):
    db, engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")
    with pytest.raises(LoanSuspendedError) as caught:
        engine.loan_service.deduct_single_loan(ind, "L-001")
    assert "indefinitely" in str(caught.value)


def test_resuming_restarts_deductions_and_leaves_the_gap(loan):
    db, engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-09-01")
    assert _repayments(db, ind) == ["2025-02-01", "2025-03-01", "2025-04-01",
                                    "2025-05-01"]

    db.resume_loan(loan_id, resumed_date="2025-10-01")
    assert db.get_loan_by_ref(ind, "L-001")["is_suspended"] == 0

    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-12-01")
    after = _repayments(db, ind)
    # The held months stay held; only months from the resume date are collected.
    assert not any("2025-06" <= d < "2025-10" for d in after)
    assert len(after) > 4


def test_a_resumed_open_span_records_the_real_end(loan):
    db, _engine, _ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")
    db.resume_loan(loan_id, resumed_date="2025-10-01")

    span = db.get_loan_suspensions_for_loan(loan_id)[0]
    assert span["resumed_date"] == "2025-10-01"
    assert span["status"] == "resumed"
    assert span["suspend_until"] is None


# --------------------------------------------------------------------------- #
# How it reads on a statement
# --------------------------------------------------------------------------- #

def test_the_statement_calls_it_indefinite(loan):
    from src.statement_generator import StatementGenerator

    db, _engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-06-01", None, today="2025-06-01")

    rows = StatementGenerator(db)._build_suspension_annotations(
        "L-001", db.get_loan_suspensions(ind), "2025-01-01", "2025-12-31", "%d %b %Y")
    assert len(rows) == 1
    assert "indefinitely" in rows[0].annotation_text


# --------------------------------------------------------------------------- #
# The rebuild path an open-ended suspension always takes
# --------------------------------------------------------------------------- #

def test_a_rebuild_keeps_the_months_before_the_suspension(loan):
    """Rebuilding a *currently suspended* loan used to delete everything.

    catch_up_loan's suspension guard fast-forwarded the due date from the
    freshly-reset issue date all the way through the suspension, so a rebuild
    wiped every repayment and re-created none — including the months that
    predate the suspension and are genuinely owed.
    """
    db, engine, ind, loan_id = loan
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-12-01")
    assert len(_repayments(db, ind)) == 11

    db.record_suspension(loan_id, "2025-06-01", None, today="2025-12-01")
    created = engine.loan_service.rebuild_loan_schedule(ind, "L-001")

    assert created == 4
    assert _repayments(db, ind) == ["2025-02-01", "2025-03-01", "2025-04-01",
                                    "2025-05-01"]


def test_the_same_holds_for_a_fixed_period_still_running(loan):
    db, engine, ind, loan_id = loan
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-12-01")

    db.record_suspension(loan_id, "2025-06-01", "2027-01-01", today="2025-12-01")
    created = engine.loan_service.rebuild_loan_schedule(ind, "L-001")

    assert created == 4
    assert _repayments(db, ind) == ["2025-02-01", "2025-03-01", "2025-04-01",
                                    "2025-05-01"]


def test_a_loan_suspended_from_issue_rebuilds_to_nothing(loan):
    """The guard still holds when there is nothing before the suspension."""
    db, engine, ind, loan_id = loan
    db.record_suspension(loan_id, "2025-01-01", None, today="2025-05-01")
    assert engine.loan_service.rebuild_loan_schedule(ind, "L-001") == 0
    assert _repayments(db, ind) == []


# --------------------------------------------------------------------------- #
# The dialog that offers it
# --------------------------------------------------------------------------- #

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QDate                                    # noqa: E402
from PyQt6.QtWidgets import (QApplication, QDialog, QLabel,       # noqa: E402
                             QMainWindow, QMessageBox, QRadioButton)


class _Stub(QMainWindow):
    last_operation_date = None
    last_report_range = None

    def show_dashboard(self):
        pass


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qt_app, loan):
    from src.views.ledger import LedgerView

    db, _engine, ind, loan_id = loan
    _Stub.last_operation_date = QDate.currentDate()
    _Stub.last_report_range = (QDate.currentDate().addMonths(-12), QDate.currentDate())
    v = LedgerView(_Stub(), db)
    v.current_individual_id = ind
    return v, db, ind, loan_id


def _accept_choosing(monkeypatch, label):
    """Run the next QDialog by ticking the radio whose text starts with ``label``."""
    def fake_exec(dialog):
        for radio in dialog.findChildren(QRadioButton):
            if radio.text().startswith(label):
                radio.setChecked(True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", fake_exec)
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))


def test_the_dialog_records_an_open_span(view, monkeypatch):
    v, db, _ind, loan_id = view
    _accept_choosing(monkeypatch, "Until I resume it")

    v.suspend_loan_dialog(loan_id, "L-001")

    span = db.loans.open_suspension(loan_id)
    assert span is not None and span["suspend_until"] is None


def test_the_dialog_still_records_a_fixed_period(view, monkeypatch):
    v, db, _ind, loan_id = view
    _accept_choosing(monkeypatch, "For a set number of months")

    v.suspend_loan_dialog(loan_id, "L-001")

    # Default is 3 months from the loan's next due date (2025-06-01). That
    # window has already closed against today's clock, so it is filed as a
    # closed historical span — the pre-existing behaviour, unchanged.
    spans = db.get_loan_suspensions_for_loan(loan_id)
    assert [(s["start_date"], s["suspend_until"]) for s in spans] == [
        ("2025-06-01", "2025-09-01")]


def test_the_header_says_an_open_hold_is_open(view, monkeypatch):
    v, _db, _ind, loan_id = view
    _accept_choosing(monkeypatch, "Until I resume it")
    v.suspend_loan_dialog(loan_id, "L-001")

    v.refresh_table()
    headings = [w.text() for w in v.findChildren(QLabel) if "SUSPENDED" in w.text()]
    assert headings and all("until resumed" in text for text in headings)
