"""The members × months model behind the fund grid.

Everything here is about the model, not the screen: what a cell means, what a
typed number turns into, what gets refused and why, and what a batch does to
the ledger. The screen is tested separately in test_fund_grid_view.py.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.services import fund_grid as fg
from src.services.benevolent_service import BenevolentService

SAVINGS = "savings"
CHRISTMAS = "christmas_savings"
BENEVOLENT = "benevolent_ledger"

MONTHS = ["2026-01", "2026-02", "2026-03"]


@pytest.fixture
def db():
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "g.db"), auto_backup=False)
    yield d
    d.close()


def _member(db, name, status="Active"):
    return db.add_individual(name, "0700", f"{name}@x", employment_status=status)


def _row(grid, name):
    return next(m for m in grid.members if m.name == name)


# --------------------------------------------------------------------------- #
# Months
# --------------------------------------------------------------------------- #

def test_a_month_range_is_inclusive_at_both_ends():
    assert fg.month_range("2026-01", "2026-03") == ["2026-01", "2026-02", "2026-03"]
    assert fg.month_range("2026-01-17", "2026-01-02") == ["2026-01"]


def test_a_backwards_range_is_empty_not_an_error():
    assert fg.month_range("2026-03", "2026-01") == []


def test_month_bounds_cover_the_whole_last_month():
    assert fg.month_bounds(["2026-01", "2026-02"]) == ("2026-01-01", "2026-02-28")
    assert fg.month_bounds(["2024-02"]) == ("2024-02-01", "2024-02-29")   # leap year


def test_an_empty_range_has_no_bounds():
    assert fg.month_bounds([]) == (None, None)


# --------------------------------------------------------------------------- #
# What a cell is
# --------------------------------------------------------------------------- #

def test_an_untouched_month_is_empty(db):
    _member(db, "Ann")
    grid = fg.load(db, SAVINGS, MONTHS)
    assert _row(grid, "Ann").cell("2026-01").state == fg.CellState.EMPTY


def test_one_transaction_makes_an_editable_cell(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)

    cell = _row(fg.load(db, SAVINGS, MONTHS), "Ann").cell("2026-01")
    assert cell.state == fg.CellState.SINGLE
    assert cell.editable
    assert cell.net == 2000


def test_a_withdrawal_reads_as_a_negative_cell(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-02-10", "Withdrawal", 500)

    row = _row(fg.load(db, SAVINGS, MONTHS), "Ann")
    assert row.cell("2026-02").net == -500


def test_several_transactions_in_a_month_lock_the_cell(db):
    """One number cannot stand for two facts, so the grid refuses to pretend."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-20", "Deposit", 500)

    cell = _row(fg.load(db, SAVINGS, MONTHS), "Ann").cell("2026-01")
    assert cell.state == fg.CellState.MIXED
    assert not cell.editable
    assert cell.net == 2500                      # still shown, just not editable


def test_the_columns_and_the_balance_agree(db):
    """Opening balance + everything shown == the member's real fund balance."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-06-01", "Deposit", 9000)   # before range
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 2000)

    row = _row(fg.load(db, SAVINGS, MONTHS), "Ann")
    assert row.opening_balance == 9000
    assert row.period_total(MONTHS) == 4000
    assert row.opening_balance + row.period_total(MONTHS) == row.total_balance


def test_inactive_members_can_be_left_out(db):
    _member(db, "Ann")
    _member(db, "Gone", status="Resigned")

    assert len(fg.load(db, SAVINGS, MONTHS, include_inactive=True).members) == 2
    kept = fg.load(db, SAVINGS, MONTHS, include_inactive=False).members
    assert [m.name for m in kept] == ["Ann"]


def test_an_unknown_table_is_refused(db):
    with pytest.raises(ValueError):
        fg.load(db, "ledger", MONTHS)


# --------------------------------------------------------------------------- #
# Typing into a cell
# --------------------------------------------------------------------------- #

def test_typing_into_an_empty_cell_inserts(db):
    ind = _member(db, "Ann")
    grid = fg.load(db, SAVINGS, MONTHS)
    change, problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "2000")

    assert problem is None
    assert change.kind == fg.ChangeKind.INSERT
    assert change.amount == 2000
    assert change.transaction_type == "Deposit"
    assert change.member_id == ind


def test_typing_over_a_figure_updates_it(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)

    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "2500")
    assert change.kind == fg.ChangeKind.UPDATE
    assert change.row_id is not None


def test_typing_the_same_figure_is_not_a_change(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)

    change, problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "2,000")
    assert change is None and problem is None


def test_clearing_a_cell_deletes_the_row(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)

    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "")
    assert change.kind == fg.ChangeKind.DELETE


def test_clearing_an_already_empty_cell_does_nothing(db):
    _member(db, "Ann")
    grid = fg.load(db, SAVINGS, MONTHS)
    assert fg.stage(grid, _row(grid, "Ann"), "2026-01", "") == (None, None)


def test_a_negative_number_means_a_withdrawal(db):
    _member(db, "Ann")
    grid = fg.load(db, SAVINGS, MONTHS)

    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "-500")
    assert change.transaction_type == "Withdrawal"
    assert change.magnitude == 500


def test_nonsense_is_reported_not_swallowed(db):
    _member(db, "Ann")
    grid = fg.load(db, SAVINGS, MONTHS)

    change, problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "two thousand")
    assert change is None
    assert "not a number" in problem


def test_a_mixed_cell_takes_a_list_of_entries(db):
    """Several rows used to lock the month. A list of entries can stand for
    them, so typing one figure makes the month hold exactly that."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-20", "Deposit", 500)
    grid = fg.load(db, SAVINGS, MONTHS)

    change, problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "3000")
    assert problem is None and change.kind == fg.ChangeKind.SET
    fg.apply_changes(db, SAVINGS, [change])
    assert db.conn.execute(
        "SELECT transaction_type, amount FROM savings WHERE individual_id=?",
        (ind,)).fetchall() == [("Deposit", 3000)]


def test_a_month_with_a_row_no_operation_describes_stays_locked(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-20", "Interest", 50)
    grid = fg.load(db, SAVINGS, MONTHS)

    change, problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "3000")
    assert change is None
    assert "no grid operation" in problem


def test_benevolent_credits_are_contributions_not_deposits(db):
    _member(db, "Ann")
    grid = fg.load(db, BENEVOLENT, MONTHS)
    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "300")
    assert change.transaction_type == "Contribution"


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def _stage_all(db, fund, typed, months=MONTHS):
    """typed: {member name: {month: text}} -> the changes it produces."""
    grid = fg.load(db, fund, months)
    changes = []
    for name, cells in typed.items():
        row = _row(grid, name)
        for month, text in cells.items():
            change, problem = fg.stage(grid, row, month, text)
            assert problem is None, problem
            if change:
                changes.append(change)
    return grid, changes


def test_a_batch_writes_every_cell(db):
    ind = _member(db, "Ann")
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {m: "2000" for m in MONTHS}})

    result = fg.apply_changes(db, SAVINGS, changes)
    assert result.inserted == 3
    assert not result.refused
    assert db.get_savings_balance(ind) == 6000


def test_the_running_balance_is_rebuilt_after_a_batch(db):
    ind = _member(db, "Ann")
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {m: "1000" for m in MONTHS}})
    fg.apply_changes(db, SAVINGS, changes)

    balances = [r[0] for r in db.conn.execute(
        "SELECT balance FROM savings WHERE individual_id=? ORDER BY date", (ind,))]
    assert balances == [1000, 2000, 3000]


def test_an_insert_lands_on_the_members_usual_day(db):
    """A grid entry should sit where that member's other entries sit."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-12-15", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": "2000"}})
    fg.apply_changes(db, SAVINGS, changes)

    dates = [r[0] for r in db.conn.execute(
        "SELECT date FROM savings WHERE individual_id=? ORDER BY date", (ind,))]
    assert dates == ["2025-12-15", "2026-01-15"]


def test_a_day_that_does_not_exist_in_the_month_is_clamped(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-31", "Deposit", 2000)
    assert fg.default_day(db, SAVINGS, ind, "2026-02") == "2026-02-28"


def test_with_no_history_an_insert_lands_on_the_first(db):
    ind = _member(db, "Ann")
    assert fg.default_day(db, SAVINGS, ind, "2026-03") == "2026-03-01"


def test_an_update_keeps_the_original_date(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-09", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": "3000"}})
    fg.apply_changes(db, SAVINGS, changes)

    row = db.conn.execute(
        "SELECT date, amount FROM savings WHERE individual_id=?", (ind,)).fetchone()
    assert row == ("2026-01-09", 3000)


def test_a_negative_figure_adds_a_withdrawal_beside_the_deposit(db):
    """A deposit happens every month; a withdrawal is as well, not instead."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": "-500"}})
    fg.apply_changes(db, SAVINGS, changes)

    rows = db.conn.execute(
        "SELECT transaction_type, amount FROM savings WHERE individual_id=? "
        "ORDER BY id", (ind,)).fetchall()
    assert rows == [("Deposit", 2000), ("Withdrawal", 500)]
    assert db.get_savings_balance(ind) == 1500


def test_flipping_a_deposit_to_a_withdrawal_is_said_outright(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-12-01", "Deposit", 5000)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": "@D=0, @W=2000"}})
    fg.apply_changes(db, SAVINGS, changes)

    rows = db.conn.execute(
        "SELECT transaction_type, amount FROM savings WHERE individual_id=? "
        "AND date LIKE '2026-01%'", (ind,)).fetchall()
    assert rows == [("Withdrawal", 2000)]
    assert db.get_savings_balance(ind) == 3000


def test_a_withdrawal_bigger_than_the_savings_is_refused(db):
    """The Withdraw dialog's rule, held by the grid too (docs §3.3)."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-02": "-2500"}})
    result = fg.apply_changes(db, SAVINGS, changes)

    assert result.refused and "overdrawn by 500" in result.refused[0].reason
    assert db.get_savings_balance(ind) == 2000


def test_a_deletion_removes_the_row(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": ""}})
    fg.apply_changes(db, SAVINGS, changes)

    assert db.get_savings_balance(ind) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 0


def test_nothing_staged_writes_nothing(db):
    _member(db, "Ann")
    result = fg.apply_changes(db, SAVINGS, [])
    assert result.summary() == "nothing changed"


# --------------------------------------------------------------------------- #
# What gets refused
# --------------------------------------------------------------------------- #

def test_a_member_who_left_cannot_receive_a_contribution(db):
    _member(db, "Ann")
    _member(db, "Gone", status="Resigned")
    grid = fg.load(db, SAVINGS, MONTHS)
    changes = [fg.stage(grid, _row(grid, n), "2026-01", "2000")[0]
               for n in ("Ann", "Gone")]

    result = fg.apply_changes(db, SAVINGS, changes)
    assert result.inserted == 1
    assert len(result.refused) == 1
    assert "has resigned" in result.refused[0].reason


def test_one_refusal_does_not_cost_the_others(db):
    """The whole point of reporting per cell rather than failing the batch."""
    for i in range(5):
        _member(db, f"Ann{i}")
    _member(db, "Gone", status="Deceased")
    grid = fg.load(db, SAVINGS, MONTHS)
    changes = [fg.stage(grid, m, "2026-01", "1000")[0] for m in grid.members]

    result = fg.apply_changes(db, SAVINGS, changes)
    assert result.inserted == 5
    assert len(result.refused) == 1


def test_a_member_who_left_may_still_withdraw(db):
    """Leaving does not trap your money — the policy says so and the grid obeys."""
    ind = _member(db, "Gone")                       # saved while still working…
    db.add_savings_transaction(ind, "2025-01-01", "Deposit", 5000)
    db.update_individual(ind, "Gone", "0700", "Gone@x", employment_status="Retired")
    grid = fg.load(db, SAVINGS, MONTHS)

    change, _problem = fg.stage(grid, _row(grid, "Gone"), "2026-01", "-1000")
    result = fg.apply_changes(db, SAVINGS, [change])
    assert result.inserted == 1
    assert db.get_savings_balance(ind) == 4000


def test_christmas_withdrawals_are_locked_outside_the_unlock_month(db):
    ind = _member(db, "Ann")
    db.fund_add_transaction(CHRISTMAS, ind, "2025-06-01", "Deposit", 5000)
    grid = fg.load(db, CHRISTMAS, MONTHS)

    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "-1000")
    result = fg.apply_changes(db, CHRISTMAS, [change])
    assert len(result.refused) == 1
    assert "December" in result.refused[0].reason


def test_a_christmas_withdrawal_in_the_unlock_month_goes_through(db):
    ind = _member(db, "Ann")
    db.fund_add_transaction(CHRISTMAS, ind, "2025-06-01", "Deposit", 5000)
    months = ["2026-12"]
    grid = fg.load(db, CHRISTMAS, months)

    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-12", "-1000")
    result = fg.apply_changes(db, CHRISTMAS, [change])
    assert result.inserted == 1
    assert db.fund_balance(CHRISTMAS, ind) == 4000


def test_an_edit_made_elsewhere_is_refused_not_overwritten(db):
    """The grid may have sat open for an hour. Somebody else's work wins."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)
    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "3000")

    # Meanwhile, on the member's own screen:
    row_id = db.conn.execute("SELECT id FROM savings").fetchone()[0]
    db.update_savings_transaction(row_id, "2026-01-01", 2500, "corrected")

    result = fg.apply_changes(db, SAVINGS, [change])
    assert len(result.refused) == 1
    assert "changed elsewhere" in result.refused[0].reason
    assert db.get_savings_balance(ind) == 2500      # their figure, not the grid's


def test_a_deleted_row_does_not_crash_the_batch(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)
    change, _problem = fg.stage(grid, _row(grid, "Ann"), "2026-01", "3000")

    db.conn.execute("DELETE FROM savings")
    db.conn.commit()

    result = fg.apply_changes(db, SAVINGS, [change])
    assert len(result.refused) == 1


# --------------------------------------------------------------------------- #
# The benevolent schedule
# --------------------------------------------------------------------------- #

def test_filling_benevolent_months_moves_the_schedule_on(db):
    """Otherwise the next catch-up re-posts every month the grid just filled."""
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-01-01")

    _grid, changes = _stage_all(db, BENEVOLENT, {"Ann": {m: "300" for m in MONTHS}})
    fg.apply_changes(db, BENEVOLENT, changes)

    assert db.get_benevolent_account(ind)["next_due_date"] == "2026-04-01"


def test_catch_up_after_a_grid_fill_does_not_double_post(db):
    ind = _member(db, "Ann")
    service = BenevolentService(db)
    service.enroll(ind, 300, "2026-01-01")

    _grid, changes = _stage_all(db, BENEVOLENT, {"Ann": {m: "300" for m in MONTHS}})
    fg.apply_changes(db, BENEVOLENT, changes)

    service.catch_up(ind, target_date="2026-03-31")
    assert db.fund_balance(BENEVOLENT, ind) == 900        # three months, once each


def test_removing_every_contribution_resets_the_schedule(db):
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-01-01")
    _grid, changes = _stage_all(db, BENEVOLENT, {"Ann": {"2026-01": "300"}})
    fg.apply_changes(db, BENEVOLENT, changes)

    _grid, undo_changes = _stage_all(db, BENEVOLENT, {"Ann": {"2026-01": ""}})
    fg.apply_changes(db, BENEVOLENT, undo_changes)

    assert db.get_benevolent_account(ind)["next_due_date"] == "2026-01-01"


def test_a_member_with_no_enrolment_leaves_the_schedule_alone(db):
    ind = _member(db, "Ann")
    _grid, changes = _stage_all(db, BENEVOLENT, {"Ann": {"2026-01": "300"}})
    fg.apply_changes(db, BENEVOLENT, changes)

    assert db.get_benevolent_account(ind) is None
    assert db.fund_balance(BENEVOLENT, ind) == 300


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #

def test_reverting_a_batch_puts_everything_back(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    before = db.get_savings_balance(ind)

    _grid, changes = _stage_all(db, SAVINGS, {
        "Ann": {"2026-01": "5000", "2026-02": "2000", "2026-03": "2000"}})
    result = fg.apply_changes(db, SAVINGS, changes)
    assert db.get_savings_balance(ind) == 9000

    fg.revert(db, SAVINGS, result)
    assert db.get_savings_balance(ind) == before
    rows = db.conn.execute(
        "SELECT date, transaction_type, amount FROM savings").fetchall()
    assert rows == [("2026-01-01", "Deposit", 2000)]


def test_reverting_a_deletion_brings_the_row_back(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000, "original note")
    _grid, changes = _stage_all(db, SAVINGS, {"Ann": {"2026-01": ""}})
    result = fg.apply_changes(db, SAVINGS, changes)

    fg.revert(db, SAVINGS, result)
    row = db.conn.execute(
        "SELECT date, transaction_type, amount, notes FROM savings").fetchone()
    assert row == ("2026-01-01", "Deposit", 2000, "original note")


def test_reverting_restores_the_benevolent_schedule(db):
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-01-01")
    _grid, changes = _stage_all(db, BENEVOLENT, {"Ann": {m: "300" for m in MONTHS}})
    result = fg.apply_changes(db, BENEVOLENT, changes)

    fg.revert(db, BENEVOLENT, result)
    assert db.get_benevolent_account(ind)["next_due_date"] == "2026-01-01"


# --------------------------------------------------------------------------- #
# Fill forward
# --------------------------------------------------------------------------- #

#: Well after MONTHS, so the future boundary is out of the way unless tested.
LATER = "2026-09-15"


def _fill(db, fund=SAVINGS, months=MONTHS, today=LATER):
    grid = fg.load(db, fund, months)
    return grid, fg.plan_fill(db, grid, grid.members, months, today=today)


def test_fill_proposes_the_members_usual_amount(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-11-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2025-12-01", "Deposit", 2000)

    _grid, plan = _fill(db)
    assert not plan.skipped
    assert [c.amount for c in plan.changes] == [2000, 2000, 2000]


def test_fill_only_touches_empty_months(db):
    """Overwriting a recorded figure with an assumed one is not on offer."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(db.get_individuals()[0][0], "2025-10-01", "Deposit", 750)
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 750)

    _grid, plan = _fill(db)
    assert [c.month for c in plan.changes] == ["2026-01", "2026-03"]


def test_fill_uses_the_enrolled_amount_for_benevolent(db):
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 250, "2026-01-01")

    _grid, plan = _fill(db, BENEVOLENT)
    assert {c.amount for c in plan.changes} == {250}


def test_fill_skips_members_it_cannot_infer_an_amount_for(db):
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2025-10-01", "Withdrawal", 10)
    db.set_setting("default_savings_increment", 0)

    _grid, plan = _fill(db)
    assert plan.changes == []
    assert "history" in plan.skipped[0][1]


def test_fill_skips_people_who_have_left(db):
    _member(db, "Gone", status="Retired")
    _grid, plan = _fill(db)
    assert plan.changes == []
    assert "has left" in plan.skipped[0][1]


def test_fill_skips_members_who_are_not_enrolled(db):
    _member(db, "Ann")
    _grid, plan = _fill(db, BENEVOLENT)
    assert plan.changes == []
    assert "not enrolled" in plan.skipped[0][1]


def test_a_filled_grid_saves_cleanly(db):
    for name in ("Ann", "Ben", "Cee"):
        ind = _member(db, name)
        db.add_savings_transaction(ind, "2025-12-01", "Deposit", 1500)

    _grid, plan = _fill(db)
    result = fg.apply_changes(db, SAVINGS, plan.changes)

    assert result.inserted == 9
    assert not result.refused
    assert db.get_total_savings() == 3 * (1500 + 4500)


# --------------------------------------------------------------------------- #
# Where a fill must stop
# --------------------------------------------------------------------------- #
# An empty cell means one of three different things and only one of them is a
# missed contribution. These pin the other two.

def test_fill_does_not_reach_back_before_the_account_began(db):
    """A member enrolled in April has no March contribution to miss."""
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-03-01")

    _grid, plan = _fill(db, BENEVOLENT)
    assert [c.month for c in plan.changes] == ["2026-03"]
    assert plan.before_start == 2
    assert "before the member's account began" in " ".join(plan.notes())


def test_fill_does_not_reach_back_before_the_first_deposit(db):
    """The same rule where there is no enrolment: savings and Christmas."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 2000)

    _grid, plan = _fill(db)
    assert [c.month for c in plan.changes] == ["2026-03"]
    assert plan.before_start == 1


def test_fill_does_not_invent_the_future(db):
    """September has not happened; nobody has paid it."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)

    months = fg.month_range("2026-01", "2026-12")
    grid = fg.load(db, SAVINGS, months)
    plan = fg.plan_fill(db, grid, grid.members, months, today="2026-08-19")

    assert [c.month for c in plan.changes] == [
        "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]
    assert plan.in_future == 4
    assert "in the future" in " ".join(plan.notes())


def test_the_current_month_is_fillable(db):
    """It is due; catch-up includes it, so the grid does too."""
    ind = _member(db, "Ann")
    db.add_savings_transaction(ind, "2026-06-01", "Deposit", 2000)

    months = fg.month_range("2026-06", "2026-08")
    grid = fg.load(db, SAVINGS, months)
    plan = fg.plan_fill(db, grid, grid.members, months, today="2026-08-19")
    assert [c.month for c in plan.changes] == ["2026-07", "2026-08"]


def test_both_boundaries_at_once(db):
    """The case as reported: enrolled part-way through, range running past today."""
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-04-01")
    db.fund_add_transaction(BENEVOLENT, ind, "2026-04-01", "Contribution", 300)

    months = fg.month_range("2026-01", "2026-12")
    grid = fg.load(db, BENEVOLENT, months)
    plan = fg.plan_fill(db, grid, grid.members, months, today="2026-08-19")

    assert [c.month for c in plan.changes] == ["2026-05", "2026-06", "2026-07", "2026-08"]
    assert (plan.before_start, plan.in_future) == (3, 4)


def test_a_member_with_no_account_at_all_is_skipped_not_guessed(db):
    """catch_up_savings refuses a member with no history; so does the grid."""
    _member(db, "Ann")
    _grid, plan = _fill(db)
    assert plan.changes == []
    assert "when Ann's" in plan.skipped[0][1]


def test_an_enrolment_dated_before_the_first_contribution_wins(db):
    """The enrolment states when the account began; a movement only evidences it."""
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-01-01")
    db.fund_add_transaction(BENEVOLENT, ind, "2026-03-01", "Contribution", 300)

    grid = fg.load(db, BENEVOLENT, MONTHS)
    assert grid.members[0].account_start == "2026-01"
    plan = fg.plan_fill(db, grid, grid.members, MONTHS, today=LATER)
    assert [c.month for c in plan.changes] == ["2026-01", "2026-02"]


def test_a_movement_before_the_enrolment_date_still_counts(db):
    """Money moved, so the account existed, whatever the enrolment says."""
    ind = _member(db, "Ann")
    db.fund_add_transaction(BENEVOLENT, ind, "2026-01-01", "Contribution", 300)
    BenevolentService(db).enroll(ind, 300, "2026-03-01")

    assert fg.load(db, BENEVOLENT, MONTHS).members[0].account_start == "2026-01"


def test_a_fill_that_has_nothing_left_to_do_says_so(db):
    ind = _member(db, "Ann")
    for month in MONTHS:
        db.add_savings_transaction(ind, f"{month}-01", "Deposit", 2000)

    _grid, plan = _fill(db)
    assert plan.changes == []
    assert "No missed months" in plan.skipped[0][1]


def test_filling_the_past_would_have_dragged_the_schedule_back(db):
    """Why the account-start rule matters beyond tidiness.

    A benevolent contribution written before the enrolment began still moves
    ``next_due_date`` — so an over-eager fill would have rewritten the member's
    schedule around months that should never have existed.
    """
    ind = _member(db, "Ann")
    BenevolentService(db).enroll(ind, 300, "2026-03-01")

    _grid, plan = _fill(db, BENEVOLENT)
    fg.apply_changes(db, BENEVOLENT, plan.changes)

    assert db.get_benevolent_account(ind)["next_due_date"] == "2026-04-01"
    assert db.fund_balance(BENEVOLENT, ind) == 300      # one month, not three


# --------------------------------------------------------------------------- #
# Telling the view what to grey out
# --------------------------------------------------------------------------- #

def test_blocked_members_are_named_in_advance(db):
    ann = _member(db, "Ann")
    gone = _member(db, "Gone", status="Terminated")
    blocked = fg.blocked_members(db, SAVINGS, [ann, gone])
    assert ann not in blocked
    assert "terminated" in blocked[gone]


def test_a_batch_summary_reads_as_a_sentence(db):
    _member(db, "Ann")
    _member(db, "Gone", status="Resigned")
    grid = fg.load(db, SAVINGS, MONTHS)
    changes = [fg.stage(grid, m, "2026-01", "500")[0] for m in grid.members]

    assert fg.apply_changes(db, SAVINGS, changes).summary() == "1 added — 1 refused"


# --------------------------------------------------------------------------- #
# Who is in the table at all
# --------------------------------------------------------------------------- #
# Not every member takes part in every ledger. Excluding is a display decision:
# it deletes nothing, hides no history, and is reversible.

def test_everyone_is_included_until_somebody_says_otherwise(db):
    _member(db, "Ann")
    assert [m.name for m in fg.load(db, SAVINGS, MONTHS).members] == ["Ann"]
    assert db.members.excluded_from(SAVINGS) == {}


def test_an_excluded_member_drops_out_of_the_grid(db):
    ann = _member(db, "Ann")
    _member(db, "Ben")
    db.members.set_excluded(ann, SAVINGS, True)

    assert [m.name for m in fg.load(db, SAVINGS, MONTHS).members] == ["Ben"]


def test_exclusion_is_per_ledger(db):
    """Not saving is not the same as not being on the benevolent scheme."""
    ann = _member(db, "Ann")
    db.members.set_excluded(ann, SAVINGS, True)

    assert [m.name for m in fg.load(db, SAVINGS, MONTHS).members] == []
    assert [m.name for m in fg.load(db, CHRISTMAS, MONTHS).members] == ["Ann"]


def test_an_excluded_member_can_be_shown_and_is_flagged(db):
    ann = _member(db, "Ann")
    db.members.set_excluded(ann, SAVINGS, True)

    row = fg.load(db, SAVINGS, MONTHS, include_excluded=True).members[0]
    assert row.excluded is True


def test_excluding_deletes_nothing(db):
    ann = _member(db, "Ann")
    db.add_savings_transaction(ann, "2026-01-01", "Deposit", 2000)
    db.members.set_excluded(ann, SAVINGS, True)

    assert db.get_savings_balance(ann) == 2000
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 1


def test_including_again_puts_the_member_back(db):
    ann = _member(db, "Ann")
    db.members.set_excluded(ann, SAVINGS, True)
    db.members.set_excluded(ann, SAVINGS, False)

    assert [m.name for m in fg.load(db, SAVINGS, MONTHS).members] == ["Ann"]
    assert db.members.excluded_from(SAVINGS) == {}


def test_excluding_twice_is_harmless(db):
    ann = _member(db, "Ann")
    db.members.set_excluded(ann, SAVINGS, True, note="does not save")
    db.members.set_excluded(ann, SAVINGS, True, note="confirmed")
    assert db.members.excluded_from(SAVINGS) == {ann: "confirmed"}


def test_fill_never_proposes_for_an_excluded_member(db):
    """The point of excluding: stop offering them a deposit every month."""
    ann = _member(db, "Ann")
    db.add_savings_transaction(ann, "2025-10-01", "Deposit", 2000)
    db.members.set_excluded(ann, SAVINGS, True)

    grid = fg.load(db, SAVINGS, MONTHS, include_excluded=True)
    plan = fg.plan_fill(db, grid, grid.members, MONTHS, today=LATER)
    assert plan.changes == []
    assert "excluded" in plan.skipped[0][1]
