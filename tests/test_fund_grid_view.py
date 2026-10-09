"""The fund grid as a screen: typing, pasting, staging, saving, undoing.

These drive the real widget rather than a stand-in, because the parts most
likely to break are the wiring — a signal that fires during a redraw, a total
that stops agreeing with the cells above it, a refusal that never clears.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QMainWindow, QMessageBox

from src.database import DatabaseManager
from src.services import fund_grid as model
from src.services.benevolent_service import BenevolentService
from src.theme import ThemeManager
from src.views.fund_grid import FundGridPage, FundGridWindow

SAVINGS = "savings"
BENEVOLENT = "benevolent_ledger"
MONTHS = ["2026-01", "2026-02", "2026-03"]
LEAD = 3                                    # Member, PF No, Opening


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


class _Host(QMainWindow):
    """Stands in for the FundGridWindow a page normally lives in."""

    def confirm_discard(self, _what="close"):
        return True


@pytest.fixture
def db():
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "v.db"), auto_backup=False)
    yield d
    d.close()


def _page(qt_app, db, fund=SAVINGS, months=MONTHS):
    from src.engine import LoanEngine
    from src.services.fund_grid import FundBackend

    page = FundGridPage(_Host(), db, ThemeManager(db), FundBackend(fund),
                        engine=LoanEngine(db))
    page.from_month.blockSignals(True)
    page.to_month.blockSignals(True)
    page.from_month.setCurrentIndex(page.from_month.findData(months[0]))
    page.to_month.setCurrentIndex(page.to_month.findData(months[-1]))
    page.from_month.blockSignals(False)
    page.to_month.blockSignals(False)
    page.reload()
    return page


def _member(db, name, status="Active"):
    return db.add_individual(name, "0700", f"{name}@x", employment_status=status)


def _row_of(page, name):
    return next(i for i, m in enumerate(page.rows) if m.name == name)


def _column_of(page, month):
    return LEAD + page.months.index(month)


def _type(page, name, month, text):
    """What Qt does when somebody edits a cell: set the text, let the signal fly."""
    page.table.item(_row_of(page, name), _column_of(page, month)).setText(text)


def _text(page, name, month):
    return page.table.item(_row_of(page, name), _column_of(page, month)).text()


def _total(page, month):
    return page.table.item(len(page.rows), _column_of(page, month)).text()


# --------------------------------------------------------------------------- #
# Loading and drawing
# --------------------------------------------------------------------------- #

def test_the_grid_draws_a_row_per_member_and_a_total(qt_app, db):
    for name in ("Ann", "Ben"):
        _member(db, name)
    page = _page(qt_app, db)

    assert page.table.rowCount() == 3                # two members + TOTAL
    assert page.table.item(2, 0).text() == "TOTAL"


def test_a_month_with_no_movement_shows_a_dash(qt_app, db):
    _member(db, "Ann")
    assert _text(_page(qt_app, db), "Ann", "2026-01") == "—"


def test_figures_land_in_their_month(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 2000)
    page = _page(qt_app, db)

    assert _text(page, "Ann", "2026-01") == "—"
    assert _text(page, "Ann", "2026-02") == "2,000"


def test_a_month_with_several_transactions_shows_its_entries(qt_app, db):
    from PyQt6.QtCore import Qt

    from src.views.fund_grid import EDIT_ROLE

    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-05", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-25", "Withdrawal", 500)
    page = _page(qt_app, db)

    item = page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    assert item.text() == "@D=2,000, @W=500"
    assert item.data(EDIT_ROLE) == "@D=2,000, @W=500"
    assert item.flags() & Qt.ItemFlag.ItemIsEditable
    assert "Withdrawal of 500 on 2026-01-25" in item.toolTip()


def test_a_month_with_a_row_no_operation_describes_is_marked_and_locked(qt_app, db):
    from PyQt6.QtCore import Qt

    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-05", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-25", "Interest", 500)
    page = _page(qt_app, db)

    item = page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    assert item.text() == "2,500 ·"
    assert not (item.flags() & Qt.ItemFlag.ItemIsEditable)
    assert "no grid operation describes" in item.toolTip()


def test_past_members_are_left_out_by_default(qt_app, db):
    _member(db, "Ann")
    _member(db, "Gone", status="Resigned")
    page = _page(qt_app, db)

    assert {m.name for m in page.rows} == {"Ann"}
    # …but never silently: the count is on the status line.
    assert "1 past member(s) hidden" in page.status.text()


def test_past_members_come_back_when_asked_for(qt_app, db):
    _member(db, "Ann")
    _member(db, "Gone", status="Resigned")
    page = _page(qt_app, db)

    page.include_inactive.setChecked(True)
    assert {m.name for m in page.rows} == {"Ann", "Gone"}
    assert "hidden" not in page.status.text()


def test_the_filter_hides_rows_without_reloading(qt_app, db):
    _member(db, "Ann")
    _member(db, "Ben")
    page = _page(qt_app, db)

    page.search.setText("ann")
    assert not page.table.isRowHidden(_row_of(page, "Ann"))
    assert page.table.isRowHidden(_row_of(page, "Ben"))


# --------------------------------------------------------------------------- #
# Typing
# --------------------------------------------------------------------------- #

def test_typing_stages_without_writing(qt_app, db):
    ind = _member(db, "Ann")
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "2000")
    assert len(page.staged) == 1
    assert page.save_btn.isEnabled()
    assert db.get_savings_balance(ind) == 0          # nothing written yet


def test_a_staged_cell_is_marked_as_unsaved(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)
    _type(page, "Ann", "2026-01", "2000")

    item = page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    assert "Unsaved" in item.toolTip()


def test_nonsense_is_rejected_and_the_cell_snaps_back(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "abc")
    assert page.staged == {}
    assert _text(page, "Ann", "2026-01") == "2,000"
    assert "not a number" in page.status.text()


def test_the_totals_follow_the_staged_edits(qt_app, db):
    """A staged figure the total ignores is a total that is lying."""
    for name in ("Ann", "Ben"):
        ind = _member(db, name)
        db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    page = _page(qt_app, db)
    assert _total(page, "2026-01") == "2,000"

    _type(page, "Ann", "2026-01", "3000")
    assert _total(page, "2026-01") == "4,000"


def test_the_period_column_follows_too(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-02", "500")
    period = page.table.item(_row_of(page, "Ann"), page.table.columnCount() - 2)
    assert period.text() == "1,500"


def test_clearing_a_cell_stages_a_deletion(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "")
    change = page.staged[(ind, "2026-01")]
    assert change.kind == model.ChangeKind.DELETE
    assert _text(page, "Ann", "2026-01") == "—"


def test_ctrl_z_takes_back_the_last_edit(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "3000")
    _type(page, "Ann", "2026-02", "1000")
    assert len(page.staged) == 2

    page._undo_edit()
    assert set(page.staged) == {(ind, "2026-01")}
    page._undo_edit()
    assert page.staged == {}
    assert _text(page, "Ann", "2026-01") == "2,000"


def test_undo_with_nothing_staged_says_so(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)
    page._undo_edit()
    assert "Nothing to undo" in page.status.text()


def test_discard_puts_every_cell_back(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "9999")
    _type(page, "Ann", "2026-03", "1")
    page._discard()

    assert page.staged == {}
    assert _text(page, "Ann", "2026-01") == "2,000"
    assert not page.save_btn.isEnabled()
    assert db.get_savings_balance(ind) == 2000


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #

def test_pasting_a_block_fills_a_rectangle(qt_app, db):
    for name in ("Ann", "Ben"):
        _member(db, name)
    page = _page(qt_app, db)

    QApplication.clipboard().setText("1000\t2000\n3000\t4000")
    page.table.setCurrentCell(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    page._paste()

    assert _text(page, "Ann", "2026-01") == "1,000"
    assert _text(page, "Ann", "2026-02") == "2,000"
    assert _text(page, "Ben", "2026-01") == "3,000"
    assert _text(page, "Ben", "2026-02") == "4,000"
    assert len(page.staged) == 4


def test_a_paste_past_the_edge_is_dropped_not_wrapped(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)

    QApplication.clipboard().setText("1\t2\t3\t4\t5\n6\t7")
    page.table.setCurrentCell(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    page._paste()

    assert len(page.staged) == 3                 # three months exist
    assert "did not fit" in page.status.text()


def test_pasting_with_nothing_selected_asks_for_a_cell(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)
    page.table.clearSelection()
    QApplication.clipboard().setText("500")
    page._paste()
    assert "Select a cell" in page.status.text()


def test_copy_puts_the_selection_on_the_clipboard(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 3000)
    page = _page(qt_app, db)

    page.table.setRangeSelection = None          # not needed; select cells directly
    page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-01")).setSelected(True)
    page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-02")).setSelected(True)
    page._copy()
    assert QApplication.clipboard().text() == "2,000\t3,000"


def test_delete_clears_every_selected_cell(qt_app, db):
    ind = _member(db, "Ann")
    for month in ("2026-01", "2026-02"):
        db.add_savings_transaction(ind, f"{month}-01", "Deposit", 1000)
    page = _page(qt_app, db)

    for month in ("2026-01", "2026-02"):
        page.table.item(_row_of(page, "Ann"), _column_of(page, month)).setSelected(True)
    page._clear_selection()

    assert len(page.staged) == 2
    assert all(c.kind == model.ChangeKind.DELETE for c in page.staged.values())


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def test_saving_writes_the_batch(qt_app, db, monkeypatch):
    ind = _member(db, "Ann")
    page = _page(qt_app, db)
    _type(page, "Ann", "2026-01", "2000")
    _type(page, "Ann", "2026-02", "2000")

    page._save()

    assert db.get_savings_balance(ind) == 4000
    assert page.staged == {}
    assert not page.save_btn.isEnabled()
    assert "Saved" in page.status.text()


def test_a_refused_cell_stays_marked_with_its_reason(qt_app, db, monkeypatch):
    said = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda _p, _t, text, *a, **k: said.setdefault("text", text)))
    ann = _member(db, "Ann")
    gone = _member(db, "Gone", status="Deceased")
    page = _page(qt_app, db)
    page.include_inactive.setChecked(True)
    _type(page, "Ann", "2026-01", "2000")
    _type(page, "Gone", "2026-01", "2000")

    page._save()

    assert db.get_savings_balance(ann) == 2000
    assert db.get_savings_balance(gone) == 0
    assert (gone, "2026-01") in page.staged           # still there to fix or drop
    assert "is deceased" in page.refused[(gone, "2026-01")]
    assert "is deceased" in said["text"]


def test_the_refused_cell_carries_its_reason_as_a_tooltip(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    _member(db, "Gone", status="Retired")
    page = _page(qt_app, db)
    page.include_inactive.setChecked(True)
    _type(page, "Gone", "2026-01", "2000")
    page._save()

    item = page.table.item(_row_of(page, "Gone"), _column_of(page, "2026-01"))
    assert "has retired" in item.toolTip()


def test_a_second_save_after_the_journal_moved_still_works(qt_app, db, monkeypatch):
    """A refused cell must be retryable, not poisoned by its stale fingerprint."""
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    page = _page(qt_app, db)
    _type(page, "Ann", "2026-01", "5000")

    # Somebody edits the same row on the member's own screen.
    row_id = db.conn.execute("SELECT id FROM savings").fetchone()[0]
    db.update_savings_transaction(row_id, "2026-01-01", 1200, "corrected")

    page._save()                                  # refused: it changed elsewhere
    assert (ind, "2026-01") in page.staged
    assert db.get_savings_balance(ind) == 1200

    page._save()                                  # retried against fresh figures
    assert db.get_savings_balance(ind) == 5000
    assert page.staged == {}


def test_undo_last_save_puts_it_all_back(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-02", "2000")
    _type(page, "Ann", "2026-01", "")
    page._save()
    assert db.get_savings_balance(ind) == 2000
    assert page.undo_save_btn.isEnabled()

    page._undo_last_save()
    assert db.get_savings_balance(ind) == 1000
    assert not page.undo_save_btn.isEnabled()
    assert _text(page, "Ann", "2026-01") == "1,000"


def test_saving_nothing_is_a_no_op(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)
    page._save()
    assert page.last_batch is None


# --------------------------------------------------------------------------- #
# Fill forward
# --------------------------------------------------------------------------- #

def test_fill_forward_stages_the_gaps(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-12-01", "Deposit", 1500)
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1500)
    page = _page(qt_app, db)

    page._fill_forward()

    assert set(page.staged) == {(ind, "2026-01"), (ind, "2026-03")}
    assert _text(page, "Ann", "2026-01") == "1,500"
    assert db.get_savings_balance(ind) == 3000       # staged, not written


def test_fill_forward_leaves_months_before_the_account_began(qt_app, db, monkeypatch):
    """The member's first deposit is February; January is not a missed month."""
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1500)
    page = _page(qt_app, db)

    page._fill_forward()

    assert set(page.staged) == {(ind, "2026-03")}
    assert _text(page, "Ann", "2026-01") == "—"


def test_the_confirmation_says_what_it_left_out(qt_app, db, monkeypatch):
    """A fill that silently skipped six months looks like one with none to skip."""
    asked = {}

    def fake_question(_parent, _title, text, *a, **k):
        asked["text"] = text
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1500)
    page = _page(qt_app, db)

    page._fill_forward()
    assert "before the member's account began" in asked["text"]
    assert page.staged == {}                          # cancelled


def test_fill_forward_can_be_declined(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel))
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1500)
    page = _page(qt_app, db)

    page._fill_forward()
    assert page.staged == {}


def test_fill_forward_with_nothing_to_do_explains_itself(qt_app, db, monkeypatch):
    said = {}
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda _p, _t, text, *a, **k: said.setdefault("text", text)))
    _member(db, "Ann")
    db.set_setting("default_savings_increment", 0)
    page = _page(qt_app, db)

    page._fill_forward()
    assert page.staged == {}
    assert "Nothing to fill" in said["text"]


def test_a_filled_grid_saves_and_the_totals_settle(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    for name in ("Ann", "Ben"):
        ind = _member(db, name)
        db.add_savings_transaction(ind, "2025-12-01", "Deposit", 1000)
    page = _page(qt_app, db)

    page._fill_forward()
    page._save()

    assert db.get_total_savings() == 2 * (1000 + 3000)
    assert _total(page, "2026-01") == "2,000"


# --------------------------------------------------------------------------- #
# Benevolent specifics
# --------------------------------------------------------------------------- #

def test_the_benevolent_grid_moves_the_schedule_on_save(qt_app, db):
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-01-01")
    page = _page(qt_app, db, fund=BENEVOLENT)

    _type(page, "Ann", "2026-01", "300")
    _type(page, "Ann", "2026-02", "300")
    page._save()

    assert db.get_benevolent_account(ind)["next_due_date"] == "2026-03-01"


def test_an_unenrolled_member_is_greyed_with_a_reason(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db, fund=BENEVOLENT)
    assert "Not enrolled" in page.table.item(_row_of(page, "Ann"), 0).toolTip()


# --------------------------------------------------------------------------- #
# The window around the pages
# --------------------------------------------------------------------------- #

def test_the_window_offers_every_ledger(qt_app, db):
    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Savings / Shares", "Christmas", "Benevolent", "Loans"]


def test_only_the_first_fund_loads_up_front(qt_app, db):
    """Three funds × every member is a lot of reading to do unasked."""
    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    assert window.pages[SAVINGS].loaded
    assert not window.pages[BENEVOLENT].loaded


def test_switching_tabs_loads_that_fund(qt_app, db):
    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    benevolent = list(window.pages).index(BENEVOLENT)
    window.tabs.setCurrentIndex(benevolent)
    assert window.pages[BENEVOLENT].loaded


def test_closing_with_unsaved_work_asks_first(qt_app, db, monkeypatch):
    asked = {}

    def fake_question(_parent, _title, text, *a, **k):
        asked["text"] = text
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    page = window.pages[SAVINGS]
    page.table.item(0, LEAD).setText("500")

    assert window.confirm_discard() is False
    assert "1 in Savings / Shares" in asked["text"]


def test_closing_with_nothing_staged_does_not_ask(qt_app, db):
    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    assert window.confirm_discard() is True


# --------------------------------------------------------------------------- #
# Theming
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("theme", ["Light", "Dark"])
def test_the_grid_names_every_colour_it_depends_on(qt_app, db, theme):
    """Nothing may fall back to the OS palette.

    The alternating row stripe did, and on a dark theme Qt painted every other
    row near-white under near-white text. Anything left to the palette is a
    colour that only looks right on whichever theme it was written under.
    """
    db.set_setting("app_theme", theme)
    _member(db, "Ann")
    page = _page(qt_app, db)

    sheet = page.table.styleSheet()
    for prop in ("background-color", "alternate-background-color", "color",
                 "gridline-color", "selection-background-color"):
        assert prop in sheet, f"{prop} not set on the grid table"

    # The page and the tab pane carry a background of their own: light text on
    # a widget that kept the default light background is what made the "From:"
    # label invisible.
    assert "#fundGridPage" in page.styleSheet()
    window = FundGridWindow(None, db, ThemeManager(db))
    assert "QTabWidget::pane" in window.styleSheet()


@pytest.mark.parametrize("theme", ["Light", "Dark"])
def test_grid_text_and_stripe_are_not_the_same_colour(qt_app, db, theme):
    """The specific failure, stated as an assertion."""
    db.set_setting("app_theme", theme)
    manager = ThemeManager(db)
    stripe = manager.get_color("bg_primary")
    base = manager.get_color("bg_secondary")
    text = manager.get_color("text_primary")
    assert text.lower() not in (stripe.lower(), base.lower())


def test_dialog_tables_get_a_themed_stripe_too(qt_app, db):
    """The shared dialog stylesheet had the same gap for any table using it."""
    db.set_setting("app_theme", "Dark")
    sheet = ThemeManager(db).dialog_stylesheet()
    assert "alternate-background-color" in sheet


# --------------------------------------------------------------------------- #
# The Loans tab
# --------------------------------------------------------------------------- #
# The page is shared with the funds but the backend is not, and the parts that
# differ are exactly the parts a fund-only test never reaches: the staging key
# is a loan rather than a member, the columns are different, and the export
# reads its headers from the backend.

def _loan_page(qt_app, db, months=MONTHS):
    from src.engine import LoanEngine
    from src.services.loan_grid import LoanBackend

    page = FundGridPage(_Host(), db, ThemeManager(db), LoanBackend(),
                        engine=LoanEngine(db))
    for combo, key in ((page.from_month, months[0]), (page.to_month, months[-1])):
        combo.blockSignals(True)
        combo.setCurrentIndex(combo.findData(key))
        combo.blockSignals(False)
    page.reload()
    return page


def _with_loan(db, name="Ann", ref="L-001", catch_up_to=None):
    from src.engine import LoanEngine

    engine = LoanEngine(db)
    ind = db.add_individual(name, "0700", f"{name}@x")
    db.add_loan_record(ind, ref, 100000, 115000, 100000, 5750, 750,
                       "2026-01-01", "2026-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2026-01-01", "Loan Issued", ref, 100000, 0, 100000, "x")
    if catch_up_to:
        engine.loan_service.catch_up_loan(ind, ref, target_date=catch_up_to)
    return ind


def test_the_loans_tab_draws_a_row_per_loan(qt_app, db):
    ind = _with_loan(db, catch_up_to="2026-02-01")
    db.add_loan_record(ind, "L-002", 50000, 57500, 50000, 2875, 375,
                       "2026-01-01", "2026-02-01", unearned_interest=7500)
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-002", 50000, 0, 50000, "x")
    page = _loan_page(qt_app, db)

    assert [r.ref for r in page.rows] == ["L-001", "L-002"]
    headers = [page.table.horizontalHeaderItem(c).text()
               for c in range(page.table.columnCount())]
    assert headers[:3] == ["Member", "Loan", "Due"]
    assert headers[-2:] == ["Paid in period", "Outstanding"]


def test_typing_into_the_due_month_stages_a_deduction(qt_app, db):
    from src.services.loan_grid import DEDUCT

    _with_loan(db, catch_up_to="2026-02-01")
    page = _loan_page(qt_app, db)
    loan_id = page.rows[0].loan_id

    page.table.item(0, LEAD + page.months.index("2026-03")).setText("5750")
    assert list(page.staged) == [(loan_id, "2026-03")]
    assert page.staged[(loan_id, "2026-03")].kind == DEDUCT


def test_staged_edits_are_keyed_by_loan_not_member(qt_app, db):
    """Two loans on one member must not collapse onto the same staged cell."""
    ind = _with_loan(db, catch_up_to="2026-02-01")
    db.add_loan_record(ind, "L-002", 50000, 57500, 50000, 2875, 375,
                       "2026-01-01", "2026-02-01", unearned_interest=7500)
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-002", 50000, 0, 50000, "x")
    page = _loan_page(qt_app, db)

    column = LEAD + page.months.index("2026-02")
    page.table.item(1, column).setText("2875")           # L-002's first month
    page.table.item(0, LEAD + page.months.index("2026-03")).setText("5750")
    assert len(page.staged) == 2
    assert {k[0] for k in page.staged} == {r.loan_id for r in page.rows}


def test_an_out_of_order_month_is_refused_on_screen(qt_app, db):
    _with_loan(db, catch_up_to="2026-02-01")
    page = _loan_page(qt_app, db)

    page.table.item(0, LEAD + page.months.index("2026-03")).setText("5750")
    page.staged.clear()
    page.table.item(0, LEAD + page.months.index("2026-03")).setText("")
    page.table.item(0, LEAD + page.months.index("2026-01")).setText("5750")
    assert "passed over" in page.status.text() or "due for" in page.status.text()


def test_fill_forward_on_loans_does_not_crash_and_stages(qt_app, db, monkeypatch):
    """The reported crash: a loan Change had no key, so staging one blew up."""
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    _with_loan(db, catch_up_to="2026-01-31")
    page = _loan_page(qt_app, db)

    page._fill_forward()

    assert page.staged                       # months staged, nothing thrown
    assert all(isinstance(k[0], int) for k in page.staged)


def test_saving_the_loans_tab_deducts(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    ind = _with_loan(db, catch_up_to="2026-01-31")
    page = _loan_page(qt_app, db)

    page._fill_forward()
    page._save()

    assert page.staged == {}
    assert db.get_loan_by_ref(ind, "L-001")["balance"] < 100000
    assert "Saved" in page.status.text()


def test_a_refused_loan_cell_reports_against_its_loan(qt_app, db, monkeypatch):
    """The refused map is keyed by change.key too — the same crash, on save."""
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    ind = _with_loan(db, catch_up_to="2026-02-01")
    db.update_individual(ind, "Ann", "0700", "Ann@x", employment_status="Resigned")

    page = _loan_page(qt_app, db)
    page.include_inactive.setChecked(True)          # so the row is on screen
    loan_id = page.rows[0].loan_id

    page.table.item(0, LEAD + page.months.index("2026-03")).setText("5750")
    page._save()

    assert (loan_id, "2026-03") in page.refused
    assert "has resigned" in page.refused[(loan_id, "2026-03")]


def test_exporting_the_loans_tab_uses_its_own_columns(qt_app, db):
    """The export built a fixed fund-shaped record and would have crashed here."""
    _with_loan(db, catch_up_to="2026-03-01")
    page = _loan_page(qt_app, db)

    frame = page._dataframe()
    assert list(frame.columns)[:3] == ["Member", "Loan", "Due"]
    assert list(frame.columns)[-2:] == ["Paid in period", "Outstanding"]
    assert frame.iloc[0]["Loan"] == "L-001"


def test_undo_last_save_works_on_the_loans_tab(qt_app, db, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    ind = _with_loan(db, catch_up_to="2026-02-01")
    before = db.get_loan_by_ref(ind, "L-001")["balance"]
    page = _loan_page(qt_app, db)

    page.table.item(0, LEAD + page.months.index("2026-03")).setText("5750")
    page._save()
    assert db.get_loan_by_ref(ind, "L-001")["balance"] < before

    page._undo_last_save()
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == before


# --------------------------------------------------------------------------- #
# Operations and help
# --------------------------------------------------------------------------- #

def test_typing_an_operation_stages_the_whole_month(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-05", "Deposit", 2000)
    page = _page(qt_app, db)

    _type(page, "Ann", "2026-01", "+@w=500")
    item = page.table.item(_row_of(page, "Ann"), _column_of(page, "2026-01"))
    assert item.text() == "@D=2,000, @W=500"
    assert "Withdrawal 500" in item.toolTip()
    # The Period column nets the month the way it will be once saved.
    period = page.table.item(_row_of(page, "Ann"), page._period_column())
    assert period.text() == "1,500"

    page._save()
    assert [(r[0], r[1]) for r in db.conn.execute(
        "SELECT transaction_type, amount FROM savings ORDER BY id")] == [
        ("Deposit", 2000), ("Withdrawal", 500)]


def test_a_mistyped_operation_is_explained_not_staged(qt_app, db):
    _member(db, "Ann")
    page = _page(qt_app, db)
    _type(page, "Ann", "2026-01", "@x=5")
    assert not page.staged
    assert "not an operation" in page.status.text()


def test_the_help_lists_each_tabs_own_operations(qt_app, db):
    from src.views.fund_grid import GridHelpDialog

    _member(db, "Ann")
    window = FundGridWindow(None, db, ThemeManager(db))
    assert window.help_btn.text() == "?"
    wanted = {SAVINGS: ["@W=amount", "+@W=500"],
              "christmas_savings": ["unlock month"],
              BENEVOLENT: ["@P=amount", "payout"],
              "loans": ["@T=amount/months", "@B", "@S=months", "due next"]}
    for fund, words in wanted.items():
        html = GridHelpDialog.html(window.theme, window.pages[fund].backend)
        for word in words:
            assert word in html, (fund, word)


def test_the_loans_tab_reaches_next_month(qt_app, db):
    """An up-to-date loan is due next month — its only typeable cell."""
    from PyQt6.QtCore import QDate

    window = FundGridWindow(None, db, ThemeManager(db))
    next_month = QDate.currentDate().addMonths(1).toString("yyyy-MM")
    assert window.pages["loans"].to_month.currentData() == next_month
    assert window.pages[SAVINGS].to_month.currentData() == QDate.currentDate().toString("yyyy-MM")


# --------------------------------------------------------------------------- #
# Deleting months — docs/grid/deleting-months.md
# --------------------------------------------------------------------------- #

def _select_column(page, month):
    page.table.clearSelection()
    page.table.selectColumn(_column_of(page, month))


def test_a_header_selected_month_and_delete_opens_the_preview(qt_app, db, monkeypatch):
    for name in ("Ann", "Ben"):
        ind = _member(db, name)
        db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1000)
    page = _page(qt_app, db)
    shown = {}
    monkeypatch.setattr(page, "_confirm_delete",
                        lambda plan, label: shown.setdefault("plan", plan) is plan)

    _select_column(page, "2026-02")
    page._clear_selection()

    assert shown["plan"].tallies == {"deposits": (2, 2000)}
    assert {k[1] for k in page.staged} == {"2026-02"} and len(page.staged) == 2
    assert _total(page, "2026-02") == "—"


def test_cancelling_the_preview_stages_nothing(qt_app, db, monkeypatch):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1000)
    page = _page(qt_app, db)
    monkeypatch.setattr(page, "_confirm_delete", lambda plan, label: False)
    _select_column(page, "2026-02")
    page._clear_selection()
    assert page.staged == {}


def test_a_month_delete_touches_only_the_rows_shown(qt_app, db):
    for name in ("Ann", "Ben"):
        ind = _member(db, name)
        db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1000)
    page = _page(qt_app, db)
    page.search.setText("Ann")
    assert page._delete_months(["2026-02"], confirm=False)
    assert [k for k in page.staged] == [(page.rows[_row_of(page, "Ann")].key, "2026-02")]

    page._save()
    names = [r[0] for r in db.conn.execute(
        "SELECT i.name FROM savings s JOIN individuals i ON i.id = s.individual_id")]
    assert names == ["Ben"]


def test_deleting_a_month_replaces_what_was_staged_in_it(qt_app, db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 1000)
    _member(db, "Ben")
    page = _page(qt_app, db)
    _type(page, "Ben", "2026-02", "700")                 # staged into an empty cell
    page._delete_months(["2026-02"], confirm=False)
    assert all(c.kind == model.ChangeKind.DELETE for c in page.staged.values())
    assert len(page.staged) == 1


def test_a_loan_months_deletion_is_described_as_it_will_run(qt_app, db):
    _with_loan(db, catch_up_to="2026-03-01")
    page = _loan_page(qt_app, db)
    page._delete_months(["2026-02"], confirm=False)
    tip = page.table.item(0, LEAD + page.months.index("2026-02")).toolTip()
    assert "missed" in tip

    # With March deleted too, February is the last entry by the time it runs.
    page._delete_months(["2026-02", "2026-03"], confirm=False)
    tip = page.table.item(0, LEAD + page.months.index("2026-02")).toolTip()
    assert "Un-runs" in tip


def test_a_missed_loan_month_shows_red_and_says_so(qt_app, db):
    from src.engine import LoanEngine
    from src.services import loan_grid as lg

    ind = _with_loan(db, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, grid.rows[0], "2026-02", "")
    lg.apply_changes(db, LoanEngine(db), [change])

    page = _loan_page(qt_app, db)
    item = page.table.item(0, LEAD + page.months.index("2026-02"))
    assert item.text() == "—"
    assert "Missed" in item.toolTip()
    assert item.foreground().color().name() == QColor(
        page.theme.get_color("danger")).name()
    assert db.get_loan_by_ref(ind, "L-001")["next_due_date"] == "2026-04-01"


def test_only_the_loans_tab_offers_paid_loans(qt_app, db):
    window = FundGridWindow(None, db, ThemeManager(db))
    assert window.pages["loans"].include_paid is not None
    assert window.pages[SAVINGS].include_paid is None
    window.pages["loans"].include_paid.setChecked(True)
    assert window.pages["loans"].backend.include_paid is True


def test_the_help_explains_deleting_months(qt_app, db):
    from src.views.fund_grid import GridHelpDialog

    window = FundGridWindow(None, db, ThemeManager(db))
    funds = GridHelpDialog.html(window.theme, window.pages[SAVINGS].backend)
    loans = GridHelpDialog.html(window.theme, window.pages["loans"].backend)
    assert "Deleting months" in funds and "missed month" in funds
    assert "un-run" in loans and "red dash" in loans
