"""Phase 7a — the fiscal-period register (migration 008)."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.exceptions import PeriodNotClosableError, TransactionError
from src.services.gl_service import GLService
from src.services.period_service import FiscalPeriodService


@pytest.fixture
def svc():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "fp.db"))
    db.settings.set("fy_start_month", "January")
    db.conn.commit()
    yield FiscalPeriodService(db), db
    db.close()


def _q1(svc):
    return svc.create("FY 2026 Q1", "2026-01-01", "2026-03-31")


# --------------------------------------------------------------------------- #
# The calendar
# --------------------------------------------------------------------------- #

def test_fy_bounds_follow_the_setting(svc):
    s, db = svc
    assert s.fy_bounds("2026-07-28") == ("2026-01-01", "2026-12-31")

    db.settings.set("fy_start_month", "November")
    db.conn.commit()
    assert s.fy_bounds("2026-07-28") == ("2025-11-01", "2026-10-31")
    # A date before the FY start belongs to the year that began earlier.
    assert s.fy_bounds("2026-11-05") == ("2026-11-01", "2027-10-31")


def test_suggested_quarters_tile_the_year_without_gaps(svc):
    s, _ = svc
    quarters = s.suggest_periods("2026-06-01")
    assert [q["start_date"] for q in quarters] == [
        "2026-01-01", "2026-04-01", "2026-07-01", "2026-10-01"]
    assert quarters[-1]["end_date"] == "2026-12-31"
    for earlier, later in zip(quarters, quarters[1:]):
        assert earlier["end_date"] < later["start_date"]


def test_suggestions_exclude_periods_that_already_exist(svc):
    s, _ = svc
    _q1(s)
    assert [q["label"] for q in s.suggest_periods("2026-06-01")] == [
        "FY 2026 Q2", "FY 2026 Q3", "FY 2026 Q4"]


def test_changing_the_setting_does_not_redraw_a_stored_period(svc):
    """Periods are declared, not derived — the whole reason the table exists."""
    s, db = svc
    pid = _q1(s)
    db.settings.set("fy_start_month", "November")
    db.conn.commit()
    stored = db.periods.get(pid)
    assert (stored["start_date"], stored["end_date"]) == ("2026-01-01", "2026-03-31")


# --------------------------------------------------------------------------- #
# The register's own integrity
# --------------------------------------------------------------------------- #

def test_periods_may_not_overlap(svc):
    s, _ = svc
    _q1(s)
    with pytest.raises(TransactionError, match="overlap"):
        s.create("Overlapping", "2026-03-01", "2026-05-31")


def test_adjacent_periods_are_fine(svc):
    s, _ = svc
    _q1(s)
    s.create("FY 2026 Q2", "2026-04-01", "2026-06-30")
    assert len(s.periods()) == 2


def test_a_period_cannot_end_before_it_starts(svc):
    s, _ = svc
    with pytest.raises(TransactionError, match="CHECK"):
        s.create("Backwards", "2026-06-30", "2026-01-01")


def test_a_label_is_unique(svc):
    s, _ = svc
    _q1(s)
    with pytest.raises(TransactionError, match="UNIQUE"):
        s.create("FY 2026 Q1", "2027-01-01", "2027-03-31")


def test_covering_finds_the_period_a_date_falls_in(svc):
    s, db = svc
    _q1(s)
    assert db.periods.covering("2026-02-14")["label"] == "FY 2026 Q1"
    assert db.periods.covering("2026-03-31")["label"] == "FY 2026 Q1", "inclusive end"
    assert db.periods.covering("2026-04-01") is None


# --------------------------------------------------------------------------- #
# Closing
# --------------------------------------------------------------------------- #

def _seed_balanced_books(db):
    ind = db.add_individual("M", "0", "m@x")
    db.add_loan_record(ind, "L1", 10000, 11500, 11500, 1000, 0, "2026-01-01", "2026-02-01")
    db.add_transaction(ind, "2026-01-05", "Loan Issued", "L1", 10000, 0, 11500, "issue")
    db.add_savings_transaction(ind, "2026-01-10", "Deposit", 2000, "dep")
    GLService(db).sync()
    return ind


def test_a_ready_period_closes(svc):
    s, db = svc
    _seed_balanced_books(db)
    pid = _q1(s)

    assert s.closing_blockers(pid) == []
    closed = s.close(pid, closed_by="tester")
    assert closed["status"] == "closed"
    assert closed["closed_at"] and closed["closed_by"] == "tester"
    assert s.is_closed("2026-02-14") and not s.is_closed("2026-05-01")
    assert s.settled_through() == "2026-03-31"


def test_closing_is_refused_while_changes_are_unposted(svc):
    """Unposted subledger changes mean the figures are still moving."""
    s, db = svc
    ind = _seed_balanced_books(db)
    pid = _q1(s)
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 500, "late")
    db.conn.execute("INSERT INTO gl_outbox (source_table, row_id) VALUES ('savings', 999)")
    db.conn.commit()

    blockers = s.closing_blockers(pid)
    assert any("not been posted" in b for b in blockers)
    with pytest.raises(PeriodNotClosableError):
        s.close(pid)
    assert db.periods.get(pid)["status"] == "open"


def test_closing_is_refused_while_an_earlier_period_is_open(svc):
    s, db = svc
    _seed_balanced_books(db)
    s.create("FY 2025 Q4", "2025-10-01", "2025-12-31")
    q1 = _q1(s)

    assert any("still open" in b for b in s.closing_blockers(q1))
    with pytest.raises(PeriodNotClosableError):
        s.close(q1)


def test_closing_is_refused_while_the_books_do_not_balance(svc):
    s, db = svc
    _seed_balanced_books(db)
    pid = _q1(s)
    # A one-sided journal line: debits no longer equal credits.
    entry = db.conn.execute(
        "INSERT INTO journal_entries (entry_date, source, status) "
        "VALUES ('2026-02-01', 'manual', 'posted')").lastrowid
    db.conn.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                    "VALUES (?, '1000', 500, 0)", (entry,))
    db.conn.commit()

    assert any("does not balance" in b for b in s.closing_blockers(pid))
    with pytest.raises(PeriodNotClosableError):
        s.close(pid)


def test_force_closing_records_what_was_wrong(svc):
    """Never silent: the accepted breaks are written into the period."""
    s, db = svc
    _seed_balanced_books(db)
    s.create("FY 2025 Q4", "2025-10-01", "2025-12-31")
    q1 = _q1(s)

    closed = s.close(q1, force=True)
    assert closed["status"] == "closed"
    assert "Closed with known issues" in closed["note"]
    assert "still open" in closed["note"]


def test_a_closed_period_cannot_be_redrawn(svc):
    s, db = svc
    _seed_balanced_books(db)
    pid = _q1(s)
    s.close(pid)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE fiscal_periods SET end_date='2026-06-30' WHERE id=?", (pid,))
    db.conn.rollback()


def test_reopening_requires_a_reason_and_leaves_a_trail(svc):
    s, db = svc
    _seed_balanced_books(db)
    pid = _q1(s)
    closed = s.close(pid, closed_by="tester")
    closed_at = closed["closed_at"]

    with pytest.raises(ValueError):
        s.reopen(pid, reason="   ")

    reopened = s.reopen(pid, reason="Interest accrual restated", reopened_by="auditor")
    assert reopened["status"] == "open" and reopened["closed_at"] is None
    assert "Interest accrual restated" in reopened["note"]
    assert closed_at in reopened["note"], "the original closing is still on the record"
    assert s.settled_through() is None


def test_status_of_a_date_outside_every_period(svc):
    s, _ = svc
    assert s.status_of("2030-01-01") is None
    assert not s.is_closed("2030-01-01")


def test_the_register_starts_empty_so_nothing_changes(svc):
    """Migration 008 declares no periods, so a journal that applies it behaves
    exactly as it did before."""
    s, db = svc
    assert s.periods() == []
    assert db.periods.counts() == {"open": 0, "closed": 0}
    assert s.settled_through() is None
