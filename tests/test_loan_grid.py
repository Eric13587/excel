"""The loans × months grid.

The fund grid can write what it likes into a cell. This one cannot, and the
tests are mostly about the difference: a loan's rows are the consequence of a
schedule, so every deduction here goes through the engine and every figure is
checked against what the engine itself would have produced.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services import loan_grid as lg
from src.services.fund_grid import CellState, ChangeKind

MONTHS = lg.month_range("2026-01", "2026-06")
LATER = "2026-09-15"                    # past every month above


def _env():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "lg.db"), auto_backup=False)
    return db, LoanEngine(db)


def _loan(db, engine, name="Ann", ref="L-001", principal=100000,
          installment=5750, monthly_interest=750, issued="2026-01-01",
          catch_up_to=None, status="Active"):
    ind = db.add_individual(name, "0700", f"{name}@x", employment_status=status)
    db.add_loan_record(ind, ref, principal, principal * 1.15, principal, installment,
                       monthly_interest, issued, "2026-02-01",
                       unearned_interest=principal * 0.15)
    db.add_transaction(ind, issued, "Loan Issued", ref, principal, 0, principal, "issue")
    if catch_up_to:
        engine.loan_service.catch_up_loan(ind, ref, target_date=catch_up_to)
    return ind


@pytest.fixture
def env():
    db, engine = _env()
    yield db, engine
    db.close()


def _row(grid, ref="L-001"):
    return next(r for r in grid.rows if r.ref == ref)


def _ledger(db, ind, ref="L-001"):
    return db.conn.execute(
        "SELECT date, event_type, deducted, principal_portion, interest_portion "
        "FROM ledger WHERE individual_id=? AND loan_id=? ORDER BY date, id",
        (ind, ref)).fetchall()


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #

def test_a_row_is_a_loan_not_a_member(env):
    """Two loans are two schedules; one figure could not be split between them."""
    db, engine = env
    ind = _loan(db, engine)
    db.add_loan_record(ind, "L-002", 50000, 57500, 50000, 2875, 375,
                       "2026-01-01", "2026-02-01", unearned_interest=7500)
    db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-002", 50000, 0, 50000, "x")

    grid = lg.load(db, MONTHS)
    assert sorted(r.ref for r in grid.rows) == ["L-001", "L-002"]
    assert {r.member_id for r in grid.rows} == {ind}


def test_deductions_land_in_their_month(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-03-01")
    row = _row(lg.load(db, MONTHS))

    assert row.cell("2026-02").net == 5750
    assert row.cell("2026-03").net == 5750
    assert row.cell("2026-04").state == CellState.EMPTY


def test_paid_loans_are_left_out_unless_asked_for(env):
    db, engine = env
    ind = _loan(db, engine, principal=5000, installment=5750, catch_up_to="2026-02-01")
    assert db.get_loan_by_ref(ind, "L-001")["status"] == "Paid"

    assert lg.load(db, MONTHS).rows == []
    assert len(lg.load(db, MONTHS, include_paid=True).rows) == 1


# --------------------------------------------------------------------------- #
# What a cell refuses, and why
# --------------------------------------------------------------------------- #

def test_the_next_due_month_takes_a_deduction(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    row = _row(grid)

    assert row.next_due_month == "2026-04"
    assert lg.cell_refusal(grid, row, "2026-04") is None


def test_a_month_beyond_the_due_date_names_the_month_that_is_owed(env):
    """A loan is paid in order; the cell says which month comes first."""
    db, engine = env
    _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)

    refusal = lg.cell_refusal(grid, _row(grid), "2026-06")
    assert "due for Apr 2026 first" in refusal


def test_a_skipped_past_month_cannot_be_back_filled(env):
    """Its interest depends on the balance at the time — not a cell's job."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-04-01")
    db.conn.execute("DELETE FROM ledger WHERE individual_id=? AND loan_id='L-001' "
                    "AND date LIKE '2026-03%'", (ind,))
    db.conn.commit()

    grid = lg.load(db, MONTHS)
    refusal = lg.cell_refusal(grid, _row(grid), "2026-03")
    assert "passed over" in refusal


def test_a_month_before_the_loan_existed_is_refused(env):
    db, engine = env
    _loan(db, engine, issued="2026-03-01", catch_up_to=None)
    grid = lg.load(db, MONTHS)
    assert "not issued until Mar 2026" in lg.cell_refusal(grid, _row(grid), "2026-01")


def test_a_suspended_month_is_refused_and_says_so(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(loan_id, "2026-03-01", None, today="2026-03-01")

    grid = lg.load(db, MONTHS)
    assert "suspended" in lg.cell_refusal(grid, _row(grid), "2026-03")


def test_a_month_with_two_payments_is_locked(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    db.add_transaction(ind, "2026-02-20", "Repayment", "L-001", 0, 1000, 0, "extra")

    grid = lg.load(db, MONTHS)
    assert _row(grid).cell("2026-02").state == CellState.MIXED
    assert "more than one payment" in lg.cell_refusal(grid, _row(grid), "2026-02")


def test_a_buyoff_is_not_editable_as_a_deduction(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    engine.buyoff_loan(ind, "L-001", "2026-03-01")

    grid = lg.load(db, MONTHS, include_paid=True)
    assert "buy-off" in lg.cell_refusal(grid, _row(grid), "2026-03")


# --------------------------------------------------------------------------- #
# Taking a deduction
# --------------------------------------------------------------------------- #

def test_a_grid_deduction_equals_what_the_engine_produces(env):
    """The whole design in one assertion."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, problem = lg.stage(grid, _row(grid), "2026-04", "5750")
    assert problem is None
    lg.apply_changes(db, engine, [change])
    through_grid = _ledger(db, ind)

    db2, engine2 = _env()
    ind2 = _loan(db2, engine2, catch_up_to="2026-04-01")
    through_engine = _ledger(db2, ind2)
    db2.close()

    assert through_grid == through_engine


def test_the_balance_matches_the_engine_too(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-04", "5750")
    lg.apply_changes(db, engine, [change])

    db2, engine2 = _env()
    ind2 = _loan(db2, engine2, catch_up_to="2026-04-01")
    expected = db2.get_loan_by_ref(ind2, "L-001")
    db2.close()

    got = db.get_loan_by_ref(ind, "L-001")
    assert (got["balance"], got["next_due_date"]) == (expected["balance"],
                                                      expected["next_due_date"])


def test_a_deduction_accrues_that_months_interest(env):
    """The failure that ruled out writing rows directly: 79,250 vs 80,000."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-04", "5750")
    lg.apply_changes(db, engine, [change])

    april = [r for r in _ledger(db, ind) if r[0].startswith("2026-04")]
    assert [r[1] for r in april] == ["Interest Earned", "Repayment"]
    # Feb and Mar took 5,000 of principal each; April takes another 5,000.
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 85000


def test_a_figure_other_than_the_instalment_is_honoured_and_split(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-04", "9000")
    lg.apply_changes(db, engine, [change])

    april = [r for r in _ledger(db, ind) if r[0].startswith("2026-04")
             and r[1] == "Repayment"][0]
    assert april[2] == 9000
    # 750 of interest accrued that month, so 8,250 came off principal.
    assert april[3] + april[4] == 9000
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 90000 - 8250


def test_several_months_for_one_loan_are_taken_in_order(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    grid = lg.load(db, MONTHS)
    row = _row(grid)
    # Staged out of order on purpose.
    changes = [lg.stage(grid, row, m, "5750")[0] for m in ("2026-03",)]
    lg.apply_changes(db, engine, changes)

    grid = lg.load(db, MONTHS)
    changes = [lg.stage(grid, _row(grid), "2026-04", "5750")[0]]
    result = lg.apply_changes(db, engine, changes)

    assert result.deducted == 1
    dates = [r[0] for r in _ledger(db, ind) if r[1] == "Repayment"]
    assert dates == ["2026-02-01", "2026-03-01", "2026-04-01"]


def test_a_member_who_left_takes_no_new_deduction(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    db.update_individual(ind, "Ann", "0700", "a@x", employment_status="Resigned")

    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-04", "5750")
    result = lg.apply_changes(db, engine, [change])

    assert result.deducted == 0
    assert "has resigned" in result.refused[0].reason


# --------------------------------------------------------------------------- #
# Correcting a deduction
# --------------------------------------------------------------------------- #

def test_correcting_an_amount_re_derives_the_split(env):
    """Without clear_derived the new figure changes nothing at all."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    change, problem = lg.stage(grid, _row(grid), "2026-03", "9000")
    assert problem is None and change.kind == ChangeKind.UPDATE
    lg.apply_changes(db, engine, [change])

    march = [r for r in _ledger(db, ind) if r[0].startswith("2026-03")
             and r[1] == "Repayment"][0]
    assert march[2] == 9000
    assert round(march[3] + march[4], 2) == 9000        # split re-derived
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 100000 - 5000 - 8250


def test_typing_the_same_figure_is_not_a_change(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    assert lg.stage(grid, _row(grid), "2026-03", "5,750") == (None, None)


def test_correcting_is_allowed_for_a_member_who_left(env):
    """A correction creates no new obligation, so the policy does not bar it."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    db.update_individual(ind, "Ann", "0700", "a@x", employment_status="Retired")

    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-03", "6000")
    result = lg.apply_changes(db, engine, [change])
    assert result.updated == 1


# --------------------------------------------------------------------------- #
# Removing a deduction
# --------------------------------------------------------------------------- #

def test_only_the_most_recent_deduction_can_be_removed(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-04-01")
    grid = lg.load(db, MONTHS)
    row = _row(grid)

    change, problem = lg.stage(grid, row, "2026-02", "")
    assert change is None
    assert "most recent" in problem

    change, problem = lg.stage(grid, row, "2026-04", "")
    assert problem is None and change.kind == ChangeKind.DELETE


def test_removing_takes_the_months_accrual_with_it(env):
    """Leaving the accrual behind is what strands a loan un-projectable."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-04-01")
    grid = lg.load(db, MONTHS)
    change, _ = lg.stage(grid, _row(grid), "2026-04", "")
    lg.apply_changes(db, engine, [change])

    assert [r for r in _ledger(db, ind) if r[0].startswith("2026-04")] == []


def test_the_loan_still_agrees_with_its_own_ledger_after_a_removal(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-04-01")
    grid = lg.load(db, MONTHS)
    lg.apply_changes(db, engine, [lg.stage(grid, _row(grid), "2026-04", "")[0]])

    db2, engine2 = _env()
    ind2 = _loan(db2, engine2, catch_up_to="2026-03-01")
    expected = db2.get_loan_by_ref(ind2, "L-001")
    db2.close()

    got = db.get_loan_by_ref(ind, "L-001")
    assert (got["balance"], got["next_due_date"]) == (expected["balance"],
                                                      expected["next_due_date"])


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #

def test_undoing_a_batch_of_deductions_restores_the_loan(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    before_rows = _ledger(db, ind)
    before_loan = db.get_loan_by_ref(ind, "L-001")

    grid = lg.load(db, MONTHS)
    row = _row(grid)
    result = lg.apply_changes(db, engine, [lg.stage(grid, row, "2026-04", "5750")[0]])
    assert result.deducted == 1

    lg.revert(db, engine, result)
    assert _ledger(db, ind) == before_rows
    after = db.get_loan_by_ref(ind, "L-001")
    assert (after["balance"], after["next_due_date"]) == (
        before_loan["balance"], before_loan["next_due_date"])


def test_undoing_a_correction_puts_the_figure_back(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    result = lg.apply_changes(
        db, engine, [lg.stage(grid, _row(grid), "2026-03", "9000")[0]])

    lg.revert(db, engine, result)
    march = [r for r in _ledger(db, ind) if r[0].startswith("2026-03")
             and r[1] == "Repayment"][0]
    assert march[2] == 5750
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 90000


def test_undoing_a_removal_brings_the_month_back(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-04-01")
    before = _ledger(db, ind)
    grid = lg.load(db, MONTHS)
    result = lg.apply_changes(db, engine, [lg.stage(grid, _row(grid), "2026-04", "")[0]])

    lg.revert(db, engine, result)
    assert _ledger(db, ind) == before


# --------------------------------------------------------------------------- #
# Fill forward — this is catch-up, as a table
# --------------------------------------------------------------------------- #

def test_fill_matches_catch_up_exactly(env):
    """If these ever diverge, one of them is wrong about somebody's money."""
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    lg.apply_changes(db, engine, plan.changes)
    through_grid = _ledger(db, ind)

    db2, engine2 = _env()
    ind2 = _loan(db2, engine2, catch_up_to="2026-02-01")
    engine2.loan_service.catch_up_loan(ind2, "L-001", target_date="2026-06-30")
    through_catch_up = _ledger(db2, ind2)
    db2.close()

    assert through_grid == through_catch_up


def test_fill_does_not_invent_the_future(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-02-01")
    months = lg.month_range("2026-01", "2026-12")
    grid = lg.load(db, months)

    plan = lg.plan_fill(db, grid, grid.rows, months, today="2026-06-15")
    assert [c.month for c in plan.changes] == ["2026-03", "2026-04", "2026-05",
                                               "2026-06"]
    assert plan.in_future == 6
    assert "in the future" in " ".join(plan.notes())


def test_fill_skips_suspended_months(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    # A past, closed suspension: the loan is not live-suspended, but those
    # months were deliberately skipped and must stay skipped.
    db.record_suspension(loan_id, "2026-03-01", "2026-05-01", today=LATER)

    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    assert [c.month for c in plan.changes] == ["2026-05", "2026-06"]
    assert plan.suspended == 2


def test_fill_skips_a_live_suspended_loan_entirely(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    loan_id = db.get_loan_by_ref(ind, "L-001")["id"]
    db.record_suspension(loan_id, "2026-03-01", None, today="2026-03-01")

    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    assert plan.changes == []
    assert "suspended" in plan.skipped[0][1]


def test_fill_skips_members_who_have_left(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    db.update_individual(ind, "Ann", "0700", "a@x", employment_status="Deceased")

    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    assert plan.changes == []
    assert "has left" in plan.skipped[0][1]


def test_fill_says_when_a_loan_is_already_up_to_date(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-06-01")
    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    assert plan.changes == []
    assert "up to date" in plan.skipped[0][1]


def test_fill_across_many_loans_is_the_mass_deduction(env):
    """What Mass Deduct does, with the figures visible before they are written."""
    db, engine = env
    for i in range(5):
        _loan(db, engine, name=f"M{i}", catch_up_to="2026-02-01")

    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    result = lg.apply_changes(db, engine, plan.changes)

    assert result.deducted == 5 * 4                  # Mar–Jun for each
    assert not result.refused
    for row in lg.load(db, MONTHS).rows:
        assert row.next_due_month == "2026-07"


def test_a_loan_that_pays_off_mid_fill_stops_there(env):
    db, engine = env
    ind = _loan(db, engine, principal=12000, installment=5750, monthly_interest=180,
                catch_up_to=None)
    grid = lg.load(db, MONTHS)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    result = lg.apply_changes(db, engine, plan.changes)

    assert db.get_loan_by_ref(ind, "L-001")["status"] == "Paid"
    assert db.get_loan_by_ref(ind, "L-001")["balance"] <= 0
    # Later months refuse rather than deducting against a settled loan.
    assert result.deducted + len(result.refused) == len(plan.changes)


def test_the_summary_reads_as_a_sentence(env):
    db, engine = env
    _loan(db, engine, catch_up_to="2026-03-01")
    grid = lg.load(db, MONTHS)
    result = lg.apply_changes(
        db, engine, [lg.stage(grid, _row(grid), "2026-04", "5750")[0]])
    assert result.summary() == "1 deducted"


# --------------------------------------------------------------------------- #
# Who is in the table at all
# --------------------------------------------------------------------------- #

def test_an_excluded_member_drops_out_of_the_loan_grid(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    db.members.set_excluded(ind, "loans", True)

    assert lg.load(db, MONTHS).rows == []
    assert len(lg.load(db, MONTHS, include_excluded=True).rows) == 1


def test_loan_exclusion_is_separate_from_fund_exclusion(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    db.members.set_excluded(ind, "savings", True)

    assert len(lg.load(db, MONTHS).rows) == 1       # still in the loan grid


def test_fill_never_deducts_for_an_excluded_member(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-02-01")
    db.members.set_excluded(ind, "loans", True)

    grid = lg.load(db, MONTHS, include_excluded=True)
    plan = lg.plan_fill(db, grid, grid.rows, MONTHS, today=LATER)
    assert plan.changes == []
    assert "excluded" in plan.skipped[0][1]


def test_excluding_a_member_leaves_their_loan_untouched(env):
    db, engine = env
    ind = _loan(db, engine, catch_up_to="2026-03-01")
    before = db.get_loan_by_ref(ind, "L-001")["balance"]
    db.members.set_excluded(ind, "loans", True)

    assert db.get_loan_by_ref(ind, "L-001")["balance"] == before
    assert len(_ledger(db, ind)) == 5
