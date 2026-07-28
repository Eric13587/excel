"""Phase 7a — the fiscal-period register (migration 008)."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.exceptions import (PeriodClosedError, PeriodNotClosableError,
                            TransactionError)
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


# --------------------------------------------------------------------------- #
# Phase 7b — a closed period is a lock (migration 009)
# --------------------------------------------------------------------------- #

@pytest.fixture
def locked(svc):
    """Books with Q1 2026 closed and Q2 left open."""
    s, db = svc
    ind = _seed_balanced_books(db)
    pid = s.create("FY 2026 Q1", "2026-01-01", "2026-03-31")
    s.close(pid)
    return s, db, ind


def test_nothing_is_locked_until_something_is_closed(svc):
    """How 009 ships: the triggers are inert on a journal with no closed
    periods, which is every journal until a user closes one."""
    s, db = svc
    ind = _seed_balanced_books(db)
    s.create("FY 2026 Q1", "2026-01-01", "2026-03-31")   # declared, still open
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 100, "")
    db.conn.execute("UPDATE ledger SET added=9999 WHERE event_type='Loan Issued'")
    db.conn.commit()


def test_activity_cannot_be_added_inside_a_closed_period(locked):
    s, db, ind = locked
    with pytest.raises(PeriodClosedError):
        db.add_savings_transaction(ind, "2026-02-01", "Deposit", 100, "late")


def test_an_amount_inside_a_closed_period_cannot_be_edited(locked):
    s, db, ind = locked
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE ledger SET added=9999 WHERE date='2026-01-05'")
    db.conn.rollback()
    assert db.conn.execute(
        "SELECT added FROM ledger WHERE date='2026-01-05'").fetchone()[0] == 10000.0


def test_a_row_inside_a_closed_period_cannot_be_deleted(locked):
    s, db, ind = locked
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("DELETE FROM ledger WHERE date='2026-01-05'")
    db.conn.rollback()


def test_a_row_cannot_be_moved_out_of_a_closed_period(locked):
    """Moving a row out changes that period's figures just as surely as moving
    one in, so both sides of the date change are checked."""
    s, db, ind = locked
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE ledger SET date='2026-06-01' WHERE date='2026-01-05'")
    db.conn.rollback()


def test_a_row_cannot_be_moved_into_a_closed_period(locked):
    s, db, ind = locked
    db.add_savings_transaction(ind, "2026-06-01", "Deposit", 50, "open period")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE savings SET date='2026-02-01' WHERE date='2026-06-01'")
    db.conn.rollback()


def test_recalculating_balances_is_still_allowed(locked):
    """The lock is on money, not on bookkeeping — the recalculators rewrite
    `balance` across every row and must keep working."""
    s, db, ind = locked
    db.conn.execute("UPDATE ledger SET balance = balance + 1")
    db.conn.execute("UPDATE savings SET balance = balance + 1")
    db.conn.commit()


def test_an_open_period_is_untouched(locked):
    s, db, ind = locked
    db.add_savings_transaction(ind, "2026-06-01", "Deposit", 100, "fine")
    assert db.conn.execute(
        "SELECT COUNT(*) FROM savings WHERE date='2026-06-01'").fetchone()[0] == 1


def test_a_journal_cannot_be_hand_posted_into_a_closed_period(locked):
    """post_journal does not go through DatabaseManager.transaction, so it
    translates the lock itself — a caller sees one exception type either way."""
    s, db, ind = locked
    gl = GLService(db)
    with pytest.raises(PeriodClosedError):
        gl.post_journal("2026-02-01", [
            {'account': '1000', 'debit': 10}, {'account': '4000', 'credit': 10}],
            source="manual")
    db.conn.rollback()


def test_a_full_rebuild_still_works_with_a_closed_period(locked):
    """The exemption that makes the lock survivable: sync(full=True) re-posts
    every projected entry, including ones inside closed periods. Without it the
    recovery path would be the first casualty of the safety feature."""
    s, db, ind = locked
    gl = GLService(db)
    before = {r['code']: (r['debit'], r['credit']) for r in gl.get_trial_balance()[0]}

    gl.sync(full=True)

    rows, balanced = gl.get_trial_balance()
    assert balanced
    assert {r['code']: (r['debit'], r['credit']) for r in rows} == before


def test_reopening_lifts_the_lock(locked):
    s, db, ind = locked
    pid = db.periods.by_label("FY 2026 Q1")["id"]
    with pytest.raises(PeriodClosedError):
        db.add_savings_transaction(ind, "2026-02-01", "Deposit", 100, "")

    s.reopen(pid, reason="restating January accrual", reopened_by="auditor")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 100, "now fine")
    assert db.conn.execute(
        "SELECT COUNT(*) FROM savings WHERE date='2026-02-01'").fetchone()[0] == 1


def test_the_error_says_what_to_do(locked):
    s, db, ind = locked
    with pytest.raises(PeriodClosedError) as excinfo:
        db.add_savings_transaction(ind, "2026-02-01", "Deposit", 100, "")
    assert "open period" in str(excinfo.value)


def test_assert_open_is_the_same_rule_before_the_write(locked):
    """The pre-emptive check, for callers that would rather ask than fail."""
    s, db, ind = locked
    s.assert_open("2026-06-01")                       # open — no raise
    with pytest.raises(PeriodClosedError):
        s.assert_open("2026-02-01", what="This repayment")


# --------------------------------------------------------------------------- #
# Phase 8 — period-end snapshots (migration 010)
# --------------------------------------------------------------------------- #

def _tb(gl, as_of=None):
    rows, balanced = gl.get_trial_balance(as_of=as_of)
    assert balanced
    return {r['code']: (r['debit'], r['credit']) for r in rows}


def test_closing_a_period_records_its_figures(svc):
    s, db = svc
    _seed_balanced_books(db)
    gl = GLService(db)
    expected = {code: round(d - c, 2)
                for code, d, c in db.journal.cumulative_totals("2026-03-31")}

    pid = _q1(s)
    s.close(pid)

    assert db.snapshots.count() == len(expected)
    assert db.snapshots.dates() == ["2026-03-31"]
    stored = {code: round(d - c, 2)
              for code, (d, c) in db.snapshots.balances_at("2026-03-31").items()}
    assert stored == expected
    assert gl.snapshot_discrepancies() == []


def test_an_open_period_is_never_snapshotted(svc):
    """Its figures can still move, so anything stored would need invalidating —
    which is the problem this design avoids rather than solves."""
    s, db = svc
    _seed_balanced_books(db)
    s.create("FY 2026 Q1", "2026-01-01", "2026-03-31")
    assert db.snapshots.count() == 0


def test_reading_through_a_snapshot_matches_recomputation(svc):
    """The equivalence the whole read path rests on."""
    s, db = svc
    ind = _seed_balanced_books(db)
    gl = GLService(db)

    before_q1 = _tb(gl, "2026-03-31")
    before_later = _tb(gl, "2026-12-31")
    before_balance = gl.get_account_balance("1100", as_of="2026-12-31")

    pid = _q1(s)
    s.close(pid)                       # snapshot now exists and will be used

    assert _tb(gl, "2026-03-31") == before_q1, "at the snapshot date itself"
    assert _tb(gl, "2026-12-31") == before_later, "snapshot + movement since"
    assert gl.get_account_balance("1100", as_of="2026-12-31") == before_balance

    # Activity after the closed period must still show up.
    db.add_savings_transaction(ind, "2026-06-01", "Deposit", 750, "later")
    gl.sync()
    assert _tb(gl, "2026-12-31") != before_later
    assert _tb(gl, "2026-03-31") == before_q1, "the settled period is unmoved"
    assert ind


def test_a_date_before_any_snapshot_falls_back(svc):
    s, db = svc
    _seed_balanced_books(db)
    gl = GLService(db)
    expected = _tb(gl, "2026-01-31")
    s.close(_q1(s))
    assert _tb(gl, "2026-01-31") == expected


def test_reopening_discards_the_snapshot(svc):
    s, db = svc
    _seed_balanced_books(db)
    gl = GLService(db)
    pid = _q1(s)
    s.close(pid)
    assert db.snapshots.count() > 0

    s.reopen(pid, reason="restating", reopened_by="auditor")
    assert db.snapshots.count() == 0
    # Reads still work, straight from journal_lines.
    assert _tb(gl, "2026-03-31")


def test_reopening_discards_later_snapshots_too(svc):
    """The totals are cumulative, so a change inside the reopened period moves
    every figure that includes it."""
    s, db = svc
    _seed_balanced_books(db)
    q1 = _q1(s)
    q2 = s.create("FY 2026 Q2", "2026-04-01", "2026-06-30")
    s.close(q1)
    s.close(q2)
    assert db.snapshots.dates() == ["2026-03-31", "2026-06-30"]

    s.reopen(q1, reason="restating January", reopened_by="auditor")
    assert db.snapshots.dates() == [], "Q2's totals included Q1"


def test_rebuild_snapshots_restores_every_closed_period(svc):
    s, db = svc
    _seed_balanced_books(db)
    gl = GLService(db)
    q1 = _q1(s)
    q2 = s.create("FY 2026 Q2", "2026-04-01", "2026-06-30")
    s.close(q1)
    s.close(q2)
    s.reopen(q1, reason="x", reopened_by="a")
    s.close(q1)                       # only Q1's snapshot comes back
    assert db.snapshots.dates() == ["2026-03-31"]

    assert gl.rebuild_snapshots() == 2
    assert db.snapshots.dates() == ["2026-03-31", "2026-06-30"]
    assert gl.snapshot_discrepancies() == []


def test_a_tampered_snapshot_is_detected(svc):
    """The guarantee behind reading stored figures instead of computing them."""
    s, db = svc
    _seed_balanced_books(db)
    gl = GLService(db)
    s.close(_q1(s))

    db.conn.execute("UPDATE account_snapshots SET closing_balance = closing_balance + 100, "
                    "debit_total = debit_total + 100 WHERE account_code='1100'")
    db.conn.commit()

    bad = gl.snapshot_discrepancies()
    assert len(bad) == 1
    as_of, code, stored, recomputed = bad[0]
    assert (as_of, code) == ("2026-03-31", "1100")
    assert round(stored - recomputed, 2) == 100.0


def test_snapshots_go_when_their_period_does(svc):
    s, db = svc
    _seed_balanced_books(db)
    pid = _q1(s)
    s.close(pid)
    assert db.snapshots.count() > 0

    db.conn.execute("DELETE FROM fiscal_periods WHERE id=?", (pid,))
    db.conn.commit()
    assert db.snapshots.count() == 0, "ON DELETE CASCADE from fiscal_periods"
