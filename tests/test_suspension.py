"""Tests for recording loan suspensions in the past / as past events."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.statement_generator import StatementGenerator
from src.data_structures import StatementConfig

TODAY = "2026-06-26"


@pytest.fixture
def db_loan():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "s.db"))
    ind = db.add_individual("Jane", "0", "j@x")
    db.add_loan_record(ind, "L1", 10000, 11500, 11500, 1000, 0, "2025-12-01", "2026-07-01")
    db.add_transaction(ind, "2025-12-01", "Loan Issued", "L1", 10000, 0, 11500, "issue")
    pk = db.get_loan_by_ref(ind, "L1")["id"]
    return db, ind, pk


def test_record_historical_suspension_is_closed_and_not_live(db_loan):
    db, ind, pk = db_loan
    kind = db.record_suspension(pk, "2026-01-01", "2026-04-01", today=TODAY)
    assert kind == "historical"

    spans = db.get_loan_suspensions(ind)
    assert len(spans) == 1
    s = spans[0]
    assert s["status"] == "resumed"
    assert s["start_date"] == "2026-01-01"
    assert s["resumed_date"] == "2026-04-01"
    # The loan is NOT flagged as currently suspended.
    assert db.get_loan_by_ref(ind, "L1")["is_suspended"] == 0


def test_record_active_suspension_sets_live_flag(db_loan):
    db, ind, pk = db_loan
    kind = db.record_suspension(pk, TODAY, "2026-09-26", today=TODAY)
    assert kind == "active"

    loan = db.get_loan_by_ref(ind, "L1")
    assert loan["is_suspended"] == 1 and loan["suspend_until"] == "2026-09-26"
    spans = db.get_loan_suspensions(ind)
    assert spans[0]["resumed_date"] is None and spans[0]["start_date"] == TODAY


def test_backdated_span_ending_in_future_is_active(db_loan):
    # Start in the past but ending in the future -> ongoing suspension.
    db, ind, pk = db_loan
    kind = db.record_suspension(pk, "2026-05-01", "2026-08-01", today=TODAY)
    assert kind == "active"
    assert db.get_loan_by_ref(ind, "L1")["is_suspended"] == 1
    assert db.get_loan_suspensions(ind)[0]["start_date"] == "2026-05-01"


def test_count_deductions_in_period(db_loan):
    db, ind, pk = db_loan
    db.add_transaction(ind, "2026-02-15", "Repayment", "L1", 0, 1000, 10500, "feb",
                       principal_portion=850, interest_portion=150)
    db.add_transaction(ind, "2026-05-15", "Repayment", "L1", 0, 1000, 9500, "may",
                       principal_portion=860, interest_portion=140)
    assert db.count_deductions_in_period(ind, "L1", "2026-01-01", "2026-04-01") == 1
    assert db.count_deductions_in_period(ind, "L1", "2026-01-01", "2026-06-01") == 2
    assert db.count_deductions_in_period(ind, "L1", "2026-08-01", "2026-12-01") == 0


def test_historical_span_appears_as_statement_annotation(db_loan):
    db, ind, pk = db_loan
    db.record_suspension(pk, "2026-01-01", "2026-04-01", today=TODAY)

    gen = StatementGenerator(db)
    data = db.get_statement_data(ind, "2026-01-01", "2026-12-31")
    pres = gen._prepare_presentation(data, "2026-01-01", "2026-12-31", StatementConfig())
    annotations = [r for sec in pres.loan_sections for r in sec.rows if r.is_annotation]
    assert len(annotations) == 1
    # Resume month (April) excluded -> Jan, Feb, Mar.
    assert "Jan, Feb, Mar 2026" in annotations[0].annotation_text


def test_catchup_skips_past_suspension_months_and_lands_dates():
    """Back-fill across a past suspend->resume gap: suspended months are skipped
    and next_due lands past the gap (the user's scenario)."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "c.db"))
    eng = LoanEngine(db)
    ind = db.add_individual("Jane", "0", "j@x")
    # 12,000 over 12 months from 2025-12-01 -> first due 2026-01-01, installment 1150.
    eng.loan_service.add_loan_event(ind, 12000, 12, "2025-12-01")
    pk = db.get_loan_by_ref(ind, "L-001")["id"]

    # Record a past suspension covering Mar, Apr, May 2026 (resumed 2026-06-01).
    db.record_suspension(pk, "2026-03-01", "2026-06-01", today="2026-08-01")

    # Catch up to Aug 2026.
    eng.loan_service.catch_up_loan(ind, "L-001", target_date="2026-08-01")

    ledger = db.get_ledger(ind)
    rep_months = sorted({str(d)[:7] for d in ledger[ledger["event_type"] == "Repayment"]["date"]})
    # Suspended months are skipped...
    assert "2026-03" not in rep_months
    assert "2026-04" not in rep_months
    assert "2026-05" not in rep_months
    # ...while the surrounding months are deducted.
    assert {"2026-01", "2026-02", "2026-06", "2026-07", "2026-08"} <= set(rep_months)

    # The dates shifted past the gap: next due is the month after the last deduction.
    assert db.get_loan_by_ref(ind, "L-001")["next_due_date"] == "2026-09-01"


def test_rebuild_applies_past_suspension_over_existing_deductions():
    """The screenshot case: deduct first, then record a past suspension and
    rebuild — the suspended months' deductions are removed and the loan extends."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "rb.db"))
    eng = LoanEngine(db)
    ls = eng.loan_service
    ind = db.add_individual("Jane", "0", "j@x")
    ls.add_loan_event(ind, 12000, 12, "2025-06-01")          # first due 2025-07-01
    ls.catch_up_loan(ind, "L-001", target_date="2026-05-01")  # continuous deductions

    before = db.get_loan_by_ref(ind, "L-001")["balance"]
    pk = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(pk, "2025-10-01", "2026-01-01", today="2026-06-26")  # Oct,Nov,Dec
    ls.rebuild_loan_schedule(ind, "L-001")

    led = db.get_ledger(ind)
    months = sorted({str(d)[:7] for d in led[led["event_type"] == "Repayment"]["date"]})
    assert not any(m in months for m in ["2025-10", "2025-11", "2025-12"])
    assert {"2025-09", "2026-01", "2026-02"} <= set(months)
    after = db.get_loan_by_ref(ind, "L-001")["balance"]
    # Three skipped 1,000 principal payments leave the balance 3,000 higher.
    assert round(after - before, 2) == 3000.0


def test_rebuild_refuses_loans_with_topups():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "tu.db"))
    eng = LoanEngine(db)
    ind = db.add_individual("Jane", "0", "j@x")
    db.add_loan_record(ind, "L1", 10000, 11500, 11500, 1000, 0, "2025-12-01", "2026-01-01")
    db.add_transaction(ind, "2025-12-01", "Loan Issued", "L1", 10000, 0, 11500, "issue")
    db.add_transaction(ind, "2026-02-01", "Loan Top-Up", "L1", 5000, 0, 16500, "topup")
    with pytest.raises(ValueError):
        eng.loan_service.rebuild_loan_schedule(ind, "L1")


def test_loan_refs_are_isolated_per_member():
    """Two members both have L-001; operations on one must not touch the other.

    Loan refs are not unique across individuals, so queries must scope by
    individual_id (the bug the user spotted in the recalc count)."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "iso.db"))
    eng = LoanEngine(db)
    a = db.add_individual("A", "0", "a@x")
    b = db.add_individual("B", "0", "b@x")
    for ind in (a, b):
        eng.loan_service.add_loan_event(ind, 12000, 12, "2025-06-01")  # both get L-001
        eng.loan_service.catch_up_loan(ind, "L-001", target_date="2026-05-01")

    # Count is scoped to one member, not both members' L-001 combined.
    a_count = db.count_deductions_in_period(a, "L-001", "2025-07-01", "2026-06-01")
    both_if_buggy = db.count_deductions_in_period(a, "L-001", "2025-07-01", "2026-06-01")
    assert a_count == 11  # not 22

    # Rebuilding A's L-001 (after a suspension) must not change B's ledger.
    b_rows_before = len(db.get_ledger(b))
    pk = db.get_loan_by_ref(a, "L-001")["id"]
    db.record_suspension(pk, "2025-10-01", "2026-01-01", today="2026-06-26")
    eng.loan_service.rebuild_loan_schedule(a, "L-001")
    assert len(db.get_ledger(b)) == b_rows_before  # B untouched
    b_months = sorted({str(d)[:7] for d in db.get_ledger(b)
                       [db.get_ledger(b)["event_type"] == "Repayment"]["date"]})
    assert "2025-10" in b_months and "2025-11" in b_months  # B still has its Oct/Nov


def test_auto_deduct_range_skips_suspended_months():
    """The UI 'Catch Up' / 'Auto' buttons use engine.auto_deduct_range — it must
    skip recorded suspension months like the other two deduction paths."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "ar.db"))
    eng = LoanEngine(db)
    ind = db.add_individual("Jane", "0", "j@x")
    eng.loan_service.add_loan_event(ind, 12000, 12, "2025-06-01")  # next due 2025-07-01
    pk = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(pk, "2025-10-01", "2026-01-01", today="2026-08-01")  # Oct,Nov,Dec

    eng.auto_deduct_range(ind, "L-001", "2025-07-01", "2026-08-01")
    led = db.get_ledger(ind)
    months = sorted({str(d)[:7] for d in led[led["event_type"] == "Repayment"]["date"]})
    assert not any(m in months for m in ["2025-10", "2025-11", "2025-12"])
    assert {"2025-07", "2025-08", "2025-09", "2026-01"} <= set(months)


def test_deduct_single_skips_suspended_month():
    """The single Deduct button lands on the first non-suspended due date."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "d.db"))
    eng = LoanEngine(db)
    ls = eng.loan_service
    ind = db.add_individual("Jane", "0", "j@x")
    ls.add_loan_event(ind, 12000, 12, "2025-06-01")  # next due 2025-07-01
    ls.catch_up_loan(ind, "L-001", target_date="2025-09-01")  # Jul, Aug, Sep -> next due Oct
    # Suspend the next three due months (Oct, Nov, Dec).
    pk = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(pk, "2025-10-01", "2026-01-01", today="2026-06-26")

    ls.deduct_single_loan(ind, "L-001")  # should skip Oct/Nov/Dec, deduct Jan
    led = db.get_ledger(ind)
    months = sorted({str(d)[:7] for d in led[led["event_type"] == "Repayment"]["date"]})
    assert not any(m in months for m in ["2025-10", "2025-11", "2025-12"])
    assert "2026-01" in months


def test_recalc_does_not_readd_suspended_deductions():
    """A balance recalc replays existing rows only — it must not re-create the
    deductions a suspension removed."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "r.db"))
    eng = LoanEngine(db)
    ls = eng.loan_service
    ind = db.add_individual("Jane", "0", "j@x")
    ls.add_loan_event(ind, 12000, 12, "2025-06-01")
    ls.catch_up_loan(ind, "L-001", target_date="2026-05-01")
    pk = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(pk, "2025-10-01", "2026-01-01", today="2026-06-26")
    ls.rebuild_loan_schedule(ind, "L-001")

    before = sorted({str(d)[:7] for d in db.get_ledger(ind)
                     [db.get_ledger(ind)["event_type"] == "Repayment"]["date"]})
    # Any of the recalc paths that other operations trigger:
    eng.balance_recalculator.recalculate_balances(ind)
    eng.balance_recalculator.recalculate_loan_history(ind, "L-001")
    after = sorted({str(d)[:7] for d in db.get_ledger(ind)
                    [db.get_ledger(ind)["event_type"] == "Repayment"]["date"]})
    assert before == after  # suspended months stay gone
    assert not any(m in after for m in ["2025-10", "2025-11", "2025-12"])
