"""Deleting months in the Ledger Grid — docs/grid/deleting-months.md.

Each section names the part of the specification it holds the code to. The
worked examples in §6 are reproduced to the shilling.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services import fund_grid as fg
from src.services import loan_grid as lg
from src.services.loan_events import replay_matches_ledger
from src.services.loan_projection import project_loan

SAVINGS, CHRISTMAS, BENEVOLENT = "savings", "christmas_savings", "benevolent_ledger"
MONTHS = lg.month_range("2026-01", "2026-08")


@pytest.fixture
def env():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "dm.db"), auto_backup=False)
    db.settings.set("deduct_same_month", False)
    yield db, LoanEngine(db)
    db.close()


def _loan(db, engine, name="Ann", principal=100000, months=20, through="2026-06-01"):
    """§6's loan: 100,000 at 15% over 20 months — 5,750 a month, 750 of it
    interest — issued in January and deducted February to ``through``."""
    ind = db.add_individual(name, "0700", f"{name}@x")
    engine.loan_service.add_loan_event(ind, principal, months, "2026-01-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date=through)
    return ind


def _grid(db, paid=False):
    return lg.load(db, MONTHS, include_paid=paid)


def _row(grid, ind, ref="L-001"):
    return next(r for r in grid.rows if r.member_id == ind and r.ref == ref)


def _delete(db, engine, ind, months, paid=False):
    grid = _grid(db, paid)
    plan = lg.plan_delete_months(db, grid, [_row(grid, ind)], months)
    return plan, lg.apply_changes(db, engine, plan.changes)


def _loan_state(db, ind, ref="L-001"):
    return db.get_loan_by_ref(ind, ref)


def _payments(db, ind, ref="L-001"):
    return db.conn.execute(
        "SELECT date, deducted, interest_portion, principal_portion FROM ledger "
        "WHERE individual_id=? AND loan_id=? AND event_type='Repayment' ORDER BY date",
        (ind, ref)).fetchall()


def _charges(db, ind, ref="L-001"):
    return [r[0] for r in db.conn.execute(
        "SELECT date FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Interest Earned' ORDER BY date", (ind, ref))]


def _conserved(db, ind, ref="L-001", written=15000):
    """§4.5: interest charged + unearned is what the loan was written for."""
    charged = db.conn.execute(
        "SELECT COALESCE(SUM(added), 0) FROM ledger WHERE individual_id=? AND loan_id=? "
        "AND event_type='Interest Earned'", (ind, ref)).fetchone()[0]
    return round(charged + _loan_state(db, ind, ref)["unearned_interest"], 2) == written


def _everything(db):
    """Every ledger and loan column, for proving an undo exact.

    Except ``loans.updated_at``: it records *when* a row was last written,
    which an undo is a write like any other.
    """
    columns = [r[1] for r in db.conn.execute("PRAGMA table_info(loans)")
               if r[1] != "updated_at"]
    return (db.conn.execute("SELECT * FROM ledger ORDER BY id").fetchall(),
            db.conn.execute(f"SELECT {', '.join(columns)} FROM loans ORDER BY id").fetchall())


def test_the_fixture_is_the_worked_example(env):
    db, engine = env
    ind = _loan(db, engine)
    assert [p[0] for p in _payments(db, ind)] == [
        "2026-02-01", "2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01"]
    loan = _loan_state(db, ind)
    assert (loan["balance"], loan["unearned_interest"]) == (75000, 11250)


# --------------------------------------------------------------------------- #
# §4.2 un-run — the deduction is the loan's last entry
# --------------------------------------------------------------------------- #

def test_deleting_the_last_month_un_runs_it(env):
    """§6, first table."""
    db, engine = env
    ind = _loan(db, engine)
    plan, result = _delete(db, engine, ind, ["2026-06"])

    assert [c.mode for c in plan.changes] == [lg.UNRUN]
    assert not result.refused
    loan = _loan_state(db, ind)
    assert loan["balance"] == 80000
    assert loan["unearned_interest"] == 12000
    assert loan["next_due_date"] == "2026-06-01"
    assert "2026-06-01" not in _charges(db, ind)
    assert _conserved(db, ind)


def test_an_un_run_month_is_taken_again_by_the_next_deduction(env):
    db, engine = env
    ind = _loan(db, engine)
    before = _payments(db, ind)
    _delete(db, engine, ind, ["2026-06"])
    engine.loan_service.deduct_single_loan(ind, "L-001")
    assert _payments(db, ind) == before
    assert _conserved(db, ind)


def test_deleting_the_last_three_months_un_runs_all_three(env):
    """§4.4: each becomes the last entry in turn."""
    db, engine = env
    ind = _loan(db, engine)
    plan, result = _delete(db, engine, ind, ["2026-04", "2026-05", "2026-06"])

    assert {c.mode for c in plan.changes} == {lg.UNRUN}
    loan = _loan_state(db, ind)
    assert (loan["balance"], loan["next_due_date"]) == (90000, "2026-04-01")
    assert loan["unearned_interest"] == 13500
    assert _conserved(db, ind)


# --------------------------------------------------------------------------- #
# §4.2 missed — later deductions exist
# --------------------------------------------------------------------------- #

def test_deleting_an_earlier_month_records_it_missed(env):
    """§6, second table, to the shilling."""
    db, engine = env
    ind = _loan(db, engine)
    plan, result = _delete(db, engine, ind, ["2026-03"])

    assert [c.mode for c in plan.changes] == [lg.MISSED]
    assert not result.refused
    assert "2026-03-01" in _charges(db, ind)                 # the charge stays
    assert _payments(db, ind) == [
        ("2026-02-01", 5750, 750, 5000),
        ("2026-04-01", 5750, 1500, 4250),                    # settles March first
        ("2026-05-01", 5750, 750, 5000),
        ("2026-06-01", 5750, 750, 5000)]
    loan = _loan_state(db, ind)
    assert loan["balance"] == 80750
    assert loan["next_due_date"] == "2026-07-01"             # the due date stays
    assert loan["unearned_interest"] == 11250                # nothing was un-charged
    assert _conserved(db, ind)


def test_a_missed_month_leaves_a_loan_that_still_reconciles(env):
    db, engine = env
    ind = _loan(db, engine)
    _delete(db, engine, ind, ["2026-03"])
    assert replay_matches_ledger(db, ind, "L-001") == (True, "matches")
    assert project_loan(db, ind, "L-001") == 0               # already derived


def test_a_missed_loan_is_collected_after_its_last_scheduled_month(env):
    """The shortfall is carried, not forgiven: the loan runs one instalment longer."""
    db, engine = env
    ind = _loan(db, engine)
    twin = _loan(db, engine, name="Ben")                     # the same loan, untouched
    _delete(db, engine, ind, ["2026-03"])
    for member in (ind, twin):
        engine.loan_service.catch_up_loan(member, "L-001", target_date="2028-12-01")

    payments = _payments(db, ind)
    assert _loan_state(db, ind)["status"] == "Paid"
    assert round(sum(p[3] for p in payments), 2) == 100000   # all principal
    assert round(sum(p[2] for p in payments), 2) == 15000    # same total interest
    assert len(payments) == len(_payments(db, twin)) == 20   # the same 20 payments…
    assert _payments(db, twin)[-1][0] == "2027-09-01"
    assert payments[-1][0] == "2027-10-01"                   # …ending a month later


def test_un_run_and_missed_in_one_batch(env):
    """§4.4: deleting March and June where June is last."""
    db, engine = env
    ind = _loan(db, engine)
    plan, _result = _delete(db, engine, ind, ["2026-03", "2026-06"])
    assert {c.month: c.mode for c in plan.changes} == {
        "2026-06": lg.UNRUN, "2026-03": lg.MISSED}
    loan = _loan_state(db, ind)
    assert loan["next_due_date"] == "2026-06-01"
    assert loan["balance"] == 85750                          # 80,000 + 5,750 missed
    assert _conserved(db, ind)


def test_deleting_a_paid_loans_deduction_reopens_it(env):
    """§4.7."""
    db, engine = env
    ind = _loan(db, engine, principal=10000, months=2)       # 5,750 x 2 clears it
    assert _loan_state(db, ind)["status"] == "Paid"
    assert _row(_grid(db, paid=True), ind)                   # shown when asked
    assert not any(r.member_id == ind for r in _grid(db).rows)

    _plan, result = _delete(db, engine, ind, ["2026-02"], paid=True)
    assert not result.refused
    loan = _loan_state(db, ind)
    assert loan["status"] == "Active" and loan["balance"] > 0


# --------------------------------------------------------------------------- #
# §4.1 / §4.3 what is never deleted, and refusals
# --------------------------------------------------------------------------- #

def test_a_top_up_in_the_month_is_not_deleted(env):
    db, engine = env
    ind = _loan(db, engine, through="2026-04-01")
    engine.loan_service.top_up_loan(ind, "L-001", 20000, 20, "2026-05-01")
    engine.loan_service.deduct_single_loan(ind, "L-001")
    plan, _result = _delete(db, engine, ind, ["2026-05"])

    assert any("not deleted" in line for line in plan.untouched)
    kinds = [r[0] for r in db.conn.execute(
        "SELECT event_type FROM ledger WHERE date LIKE '2026-05%' ORDER BY id")]
    assert kinds == ["Loan Top-Up"]


def test_the_last_deduction_before_a_top_up_is_refused(env):
    db, engine = env
    ind = _loan(db, engine, through="2026-04-01")
    engine.loan_service.top_up_loan(ind, "L-001", 20000, 20, "2026-05-01")
    grid = _grid(db)
    change, problem = lg.stage(grid, _row(grid, ind), "2026-04", "")
    assert change is None and "top-up on 2026-05-01" in problem


def test_a_deduction_before_a_top_up_with_later_deductions_is_missed(env):
    db, engine = env
    ind = _loan(db, engine, through="2026-04-01")
    engine.loan_service.top_up_loan(ind, "L-001", 20000, 20, "2026-05-01")
    engine.loan_service.deduct_single_loan(ind, "L-001")
    plan, result = _delete(db, engine, ind, ["2026-04"])
    assert [c.mode for c in plan.changes] == [lg.MISSED] and not result.refused
    assert replay_matches_ledger(db, ind, "L-001") == (True, "matches")


def test_a_loans_deletions_are_all_or_nothing(env):
    """Deleting May and April: May un-runs, which leaves April as the last
    deduction before the top-up — refused, so May stays too."""
    db, engine = env
    ind = _loan(db, engine, through="2026-04-01")
    engine.loan_service.top_up_loan(ind, "L-001", 20000, 20, "2026-05-01")
    engine.loan_service.deduct_single_loan(ind, "L-001")
    before = _payments(db, ind)

    plan, _result = _delete(db, engine, ind, ["2026-04", "2026-05"])
    assert not plan.changes and "top-up" in plan.refused[0][1]

    # Staged by hand anyway, the save refuses the loan as a whole.
    grid = _grid(db)
    row = _row(grid, ind)
    changes = [lg.Change(row.loan_id, ind, "L-001", m, lg.ChangeKind.DELETE,
                         row_id=row.cell(m).row_id) for m in ("2026-04", "2026-05")]
    result = lg.apply_changes(db, engine, changes)
    assert len(result.refused) == 2 and not result.applied
    assert _payments(db, ind) == before


def test_a_month_behind_a_buy_off_is_refused(env):
    db, engine = env
    ind = _loan(db, engine)
    engine.loan_service.buyoff_loan(ind, "L-001", "2026-07-01")
    grid = _grid(db, paid=True)
    row = _row(grid, ind)
    assert "bought off on 2026-07-01" in lg.stage(grid, row, "2026-03", "")[1]
    assert "buy-off" in lg.cell_refusal(grid, row, "2026-07")
    plan = lg.plan_delete_months(db, grid, [row], ["2026-03", "2026-07"])
    assert not plan.changes and plan.refused


# --------------------------------------------------------------------------- #
# §4.5 interest is conserved — the bug this specification found
# --------------------------------------------------------------------------- #

def test_interest_is_conserved_through_a_grid_deduction_and_its_undo(env):
    db, engine = env
    ind = _loan(db, engine, through="2026-03-01")
    grid = _grid(db)
    change, _ = lg.stage(grid, _row(grid, ind), "2026-04", "5750")
    result = lg.apply_changes(db, engine, [change])
    lg.revert(db, engine, result)
    assert _conserved(db, ind)
    assert _loan_state(db, ind)["unearned_interest"] == 13500


def test_interest_is_conserved_through_a_deletion_and_its_undo(env):
    db, engine = env
    ind = _loan(db, engine)
    _plan, result = _delete(db, engine, ind, ["2026-05", "2026-06"])
    assert _conserved(db, ind)
    lg.revert(db, engine, result)
    assert _conserved(db, ind)


# --------------------------------------------------------------------------- #
# §4.6 putting a missed month back
# --------------------------------------------------------------------------- #

def test_a_missed_month_can_take_its_deduction_again(env):
    db, engine = env
    ind = _loan(db, engine)
    original = _payments(db, ind)
    _delete(db, engine, ind, ["2026-03"])

    grid = _grid(db)
    row = _row(grid, ind)
    assert row.missed("2026-03") and lg.cell_refusal(grid, row, "2026-03") is None
    assert "Missed" in lg.LoanBackend().cell_note(row, "2026-03")
    change, problem = lg.stage(grid, row, "2026-03", "5750")
    assert problem is None and change.kind == lg.LATE

    result = lg.apply_changes(db, engine, [change])
    assert "1 missed month(s) recorded" in result.summary()
    assert _payments(db, ind) == original
    assert _loan_state(db, ind)["balance"] == 75000


def test_an_operation_cannot_go_in_a_missed_month(env):
    db, engine = env
    ind = _loan(db, engine)
    _delete(db, engine, ind, ["2026-03"])
    grid = _grid(db)
    assert "goes in the month" in lg.stage(grid, _row(grid, ind), "2026-03", "@B")[1]


def test_a_month_never_charged_still_cannot_be_typed_into(env):
    db, engine = env
    ind = _loan(db, engine)
    db.conn.execute("DELETE FROM ledger WHERE individual_id=? AND date LIKE '2026-03%'",
                    (ind,))
    grid = _grid(db)
    assert "passed over" in lg.cell_refusal(grid, _row(grid, ind), "2026-03")


# --------------------------------------------------------------------------- #
# §5 undo is exact
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("months", [["2026-06"], ["2026-03"], ["2026-03", "2026-06"],
                                    ["2026-02", "2026-03", "2026-04", "2026-05",
                                     "2026-06"]])
def test_undo_restores_every_column_of_every_row(env, months):
    db, engine = env
    ind = _loan(db, engine)
    _loan(db, engine, name="Ben")
    before = _everything(db)
    _plan, result = _delete(db, engine, ind, months)
    assert _everything(db) != before
    lg.revert(db, engine, result)
    assert _everything(db) == before


def test_undoing_a_correction_restores_later_splits_exactly(env):
    db, engine = env
    ind = _loan(db, engine)
    before = _everything(db)
    grid = _grid(db)
    change, _ = lg.stage(grid, _row(grid, ind), "2026-03", "500")   # less than interest
    result = lg.apply_changes(db, engine, [change])
    assert _payments(db, ind)[2][2] == 1000          # April settles March's 250 first
    lg.revert(db, engine, result)
    assert _everything(db) == before


# --------------------------------------------------------------------------- #
# §2 the rows on screen, and the preview
# --------------------------------------------------------------------------- #

def test_only_the_rows_given_are_touched_and_the_rest_are_counted(env):
    db, engine = env
    ann = _loan(db, engine)
    ben = _loan(db, engine, name="Ben")
    plan, _result = _delete(db, engine, ann, ["2026-06"])
    assert {c.member_id for c in plan.changes} == {ann}
    assert any("not on screen" in line for line in plan.untouched)
    assert len(_payments(db, ben)) == 5


def test_the_preview_says_what_each_kind_of_deletion_does(env):
    db, engine = env
    ind = _loan(db, engine)
    grid = _grid(db)
    plan = lg.plan_delete_months(db, grid, [_row(grid, ind)], ["2026-03", "2026-06"])
    assert plan.tallies == {"un-run": (1, 5750), "missed": (1, 5750)}
    text = " ".join(plan.consequences)
    assert "Un-run (1)" in text and "Missed (1)" in text and "suspend" in text


# --------------------------------------------------------------------------- #
# §3 funds
# --------------------------------------------------------------------------- #

def _saver(db, name="Ann", withdraw=None, fund=SAVINGS):
    ind = db.add_individual(name, "0700", f"{name}@x")
    for month in ("01", "02", "03", "04", "05", "06"):
        db.fund_add_transaction(fund, ind, f"2026-{month}-01",
                                "Contribution" if fund == BENEVOLENT else "Deposit",
                                2000, "")
    if withdraw:
        db.fund_add_transaction(fund, ind, "2026-06-15", "Withdrawal", withdraw, "")
    return ind


def _fund_delete(db, fund, members, months):
    grid = fg.load(db, fund, MONTHS)
    rows = [m for m in grid.members if m.member_id in members]
    plan = fg.plan_delete_months(db, grid, rows, months)
    return plan, fg.apply_changes(db, fund, plan.changes)


def test_a_fund_month_is_deleted_whole_and_balances_follow(env):
    db, _engine = env
    ind = _saver(db, withdraw=500)
    plan, result = _fund_delete(db, SAVINGS, {ind}, ["2026-06"])
    assert plan.tallies == {"deposits": (1, 2000), "withdrawals": (1, 500)}
    assert not result.refused
    assert db.funds.balance(SAVINGS, ind) == 10000
    last = db.conn.execute("SELECT balance FROM savings WHERE individual_id=? "
                           "ORDER BY date DESC, id DESC LIMIT 1", (ind,)).fetchone()[0]
    assert last == 10000


def test_a_deleted_fund_month_becomes_a_missed_month(env):
    db, _engine = env
    ind = _saver(db)
    _fund_delete(db, SAVINGS, {ind}, ["2026-03"])
    grid = fg.load(db, SAVINGS, MONTHS)
    plan = fg.plan_fill(db, grid, grid.members, MONTHS, today="2026-06-30")
    assert [c.month for c in plan.changes] == ["2026-03"]


def test_deleting_under_a_withdrawal_that_stays_covered_goes_ahead(env):
    """§6: March goes; the June withdrawal is taken from 10,000 instead of 12,000."""
    db, _engine = env
    ind = _saver(db, withdraw=10000)
    plan, result = _fund_delete(db, SAVINGS, {ind}, ["2026-03"])
    assert plan.changes and not plan.refused and not result.refused
    assert db.funds.balance(SAVINGS, ind) == 0


def test_deleting_under_a_withdrawal_it_would_overdraw_is_refused(env):
    """§6: without January to March the June withdrawal would come from 6,000."""
    db, _engine = env
    ind = _saver(db, withdraw=10000)
    other = _saver(db, name="Ben")
    plan, result = _fund_delete(db, SAVINGS, {ind, other},
                                ["2026-01", "2026-02", "2026-03"])
    assert [r.name for r, _why in plan.refused] == ["Ann"]
    assert "overdrawn by 4,000" in plan.refused[0][1]
    assert {c.member_id for c in plan.changes} == {other}     # Ben goes ahead
    assert db.funds.balance(SAVINGS, ind) == 2000             # 12,000 - 10,000


def test_the_save_refuses_an_overdraw_even_if_the_preview_was_skipped(env):
    db, _engine = env
    ind = _saver(db, withdraw=10000)
    grid = fg.load(db, SAVINGS, MONTHS)
    row = next(m for m in grid.members if m.member_id == ind)
    changes = [fg.stage(grid, row, m, "")[0] for m in ("2026-01", "2026-02", "2026-03")]
    result = fg.apply_changes(db, SAVINGS, changes)
    assert len(result.refused) == 3 and not result.applied
    assert "overdrawn by 4,000" in result.refused[0].reason
    assert db.funds.balance(SAVINGS, ind) == 2000


def test_benevolent_payouts_may_exceed_contributions(env):
    """§3.3: a payout is a claim on a pooled fund."""
    db, _engine = env
    ind = _saver(db, withdraw=10000, fund=BENEVOLENT)
    plan, result = _fund_delete(db, BENEVOLENT, {ind}, ["2026-01", "2026-02", "2026-03"])
    assert not plan.refused and not result.refused
    assert db.funds.balance(BENEVOLENT, ind) == -4000


def test_deleting_the_latest_contributions_moves_the_schedule_back(env):
    from src.services.benevolent_service import BenevolentService

    db, _engine = env
    ind = db.add_individual("Ann", "0700", "a@x")
    service = BenevolentService(db)
    service.enroll(ind, 300, "2026-01-01")
    for _ in range(6):
        service.deduct_single(ind)
    assert db.funds.get_enrolment(ind)["next_due_date"] == "2026-07-01"
    _fund_delete(db, BENEVOLENT, {ind}, ["2026-05", "2026-06"])
    assert db.funds.get_enrolment(ind)["next_due_date"] == "2026-05-01"
    _fund_delete(db, BENEVOLENT, {ind}, ["2026-02"])
    assert db.funds.get_enrolment(ind)["next_due_date"] == "2026-05-01"


def test_a_christmas_withdrawal_can_be_deleted_outside_the_unlock_month(env):
    db, _engine = env
    db.settings.set("christmas_unlock_month", 12)
    ind = _saver(db, withdraw=500, fund=CHRISTMAS)
    _plan, result = _fund_delete(db, CHRISTMAS, {ind}, ["2026-06"])
    assert not result.refused


def test_a_month_holding_an_interest_posting_is_left_whole(env):
    db, _engine = env
    ind = _saver(db)
    with db.transaction():
        db.funds.add(SAVINGS, ind, "2026-03-20", "Interest", 40, 0.0, "")
    plan, _result = _fund_delete(db, SAVINGS, {ind}, ["2026-03"])
    assert not plan.changes and "no grid operation" in plan.refused[0][1]
    assert len(db.funds.history(SAVINGS, ind)) == 7


def test_undoing_a_fund_month_deletion_puts_it_back(env):
    db, _engine = env
    ind = _saver(db, withdraw=500)
    before = [r[1:] for r in db.funds.history(SAVINGS, ind)]
    _plan, result = _fund_delete(db, SAVINGS, {ind}, ["2026-05", "2026-06"])
    fg.revert(db, SAVINGS, result)
    assert [r[1:] for r in db.funds.history(SAVINGS, ind)] == before
    assert db.funds.balance(SAVINGS, ind) == 11500


def test_undo_keeps_same_day_rows_in_their_order(env):
    """Rehearsal finding: re-added under new ids, a deposit and a withdrawal on
    the same day traded places, and the balance between them changed."""
    db, _engine = env
    ind = _saver(db)
    db.fund_add_transaction(SAVINGS, ind, "2026-06-01", "Withdrawal", 3000, "")
    before = db.conn.execute("SELECT * FROM savings ORDER BY date, id").fetchall()
    _plan, result = _fund_delete(db, SAVINGS, {ind}, ["2026-06"])
    fg.revert(db, SAVINGS, result)
    assert db.conn.execute("SELECT * FROM savings ORDER BY date, id").fetchall() == before

