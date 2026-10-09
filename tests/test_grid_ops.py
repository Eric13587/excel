"""Operations in grid cells: ``@W=500``, ``2,000, @W=500``, ``@T=20,000/12``.

The parser is tested on its own, then each grid end to end — typed text to
ledger rows, and back again through Undo Last Save — because the risk is not
in reading the syntax but in what a list of entries does to rows that exist.
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
from src.services.grid_ops import Op, OpError, OpSet

SAVINGS, CHRISTMAS, BENEVOLENT = "savings", "christmas_savings", "benevolent_ledger"
MONTHS = ["2026-01", "2026-02", "2026-03"]
SAVINGS_OPS = fg.operations(SAVINGS)


# --------------------------------------------------------------------------- #
# The syntax
# --------------------------------------------------------------------------- #

def _read(text, ops=SAVINGS_OPS):
    entries, append = ops.parse(text)
    return [(e.op.code, e.amount, e.arg) for e in entries], append


@pytest.mark.parametrize("text", ["@W=500", "@w=500", "@W 500", "@w500",
                                  "@withdraw=500", "@Withdrawal = 500", "@w: 500"])
def test_an_operation_is_any_unambiguous_start_of_its_name(text):
    assert _read(text) == ([("W", 500, None)], False)


def test_a_bare_figure_is_the_default_and_keeps_its_thousands():
    assert _read("2,000") == ([("D", 2000, None)], False)
    assert _read("@I=2,000") == ([("D", 2000, None)], False)     # "increment"


def test_a_comma_splits_entries_only_before_an_operation():
    assert _read("2,000, @W=500") == ([("D", 2000, None), ("W", 500, None)], False)
    assert _read("2,000 @W=1,500") == ([("D", 2000, None), ("W", 1500, None)], False)
    assert _read("@D=2,000; @W=500")[0] == [("D", 2000, None), ("W", 500, None)]


def test_a_comma_and_a_space_always_separate():
    """Never a silent merge: "@W=500, 2000" once read as 5,002,000."""
    assert _read("@W=500, 2000")[0] == [("W", 500, None), ("D", 2000, None)]
    assert _read("2000, 500")[0] == [("D", 2000, None), ("D", 500, None)]


@pytest.mark.parametrize("text", ["1,00", "500,2000", "@W=5,00"])
def test_misplaced_thousands_commas_are_refused_not_guessed(text):
    with pytest.raises(OpError, match="wrong places"):
        SAVINGS_OPS.parse(text)


def test_a_negative_figure_is_still_a_withdrawal():
    assert _read("-500") == ([("W", 500, None)], False)


def test_a_leading_plus_adds_rather_than_replaces():
    assert _read("+@W=500") == ([("W", 500, None)], True)
    assert _read("+500") == ([("D", 500, None)], False)          # a plain number


@pytest.mark.parametrize("text", ["", "-", "—"])
def test_nothing_is_no_entries(text):
    assert _read(text) == ([], False)


def test_a_zero_is_kept_so_it_can_remove_its_kind():
    assert _read("0") == ([("D", 0, None)], False)
    assert _read("@W=0") == ([("W", 0, None)], False)


@pytest.mark.parametrize("text, words", [
    ("@X=5", "not an operation"), ("abc", "not a number"),
    ("@W", "needs a figure"), ("@W=-5", "positive figure")])
def test_nonsense_is_refused_in_a_sentence(text, words):
    with pytest.raises(OpError, match=words):
        SAVINGS_OPS.parse(text)


def test_an_ambiguous_start_says_what_it_could_mean():
    deposit = Op("A", "Deposit", ("deposit",), "")
    ops = OpSet((deposit, Op("B", "Dividend", ("dividend",), "")), deposit)
    with pytest.raises(OpError, match="could mean"):
        ops.parse("@d=5")


def test_loan_operations_carry_their_own_figures():
    assert _read("@T=20,000/12", lg.OPS)[0] == [("T", 20000, 12)]
    assert _read("@topup 20000 / 12m", lg.OPS)[0] == [("T", 20000, 12)]
    assert _read("@B", lg.OPS)[0] == [("B", 0.0, None)]
    assert _read("@S=3", lg.OPS)[0] == [("S", 3, None)]
    with pytest.raises(OpError, match="months too"):
        lg.OPS.parse("@T=20000")
    with pytest.raises(OpError, match="takes no figure"):
        lg.OPS.parse("@B=5000")
    with pytest.raises(OpError, match="more than zero"):
        lg.OPS.parse("-500")


@pytest.mark.parametrize("text", ["2,000", "@D=2,000, @W=500", "@W=500",
                                  "@D=1,234.50, @D=10"])
def test_formatting_reads_back_as_the_same_entries(text):
    entries, _ = SAVINGS_OPS.parse(text)
    assert SAVINGS_OPS.format(entries) == text


def test_a_lone_default_is_written_bare_and_anything_else_prefixed():
    entries, _ = SAVINGS_OPS.parse("2000, @w=500")
    assert SAVINGS_OPS.format(entries) == "@D=2,000, @W=500"
    assert lg.OPS.format(lg.OPS.parse("@t=20000/12")[0]) == "@T=20,000/12"


# --------------------------------------------------------------------------- #
# Funds: a month as a list
# --------------------------------------------------------------------------- #

@pytest.fixture
def db():
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "o.db"), auto_backup=False)
    yield d
    d.close()


def _member(db, name="Ann"):
    return db.add_individual(name, "0700", f"{name}@x")


def _row(grid, name="Ann"):
    return next(m for m in grid.members if m.name == name)


def _rows(db, ind, table=SAVINGS):
    return db.conn.execute(
        f"SELECT id, transaction_type, amount, notes FROM {table} "
        f"WHERE individual_id=? ORDER BY date, id", (ind,)).fetchall()


def _type_and_save(db, fund, text, month="2026-01", name="Ann"):
    grid = fg.load(db, fund, MONTHS)
    change, problem = fg.stage(grid, _row(grid, name), month, text)
    assert problem is None, problem
    return fg.apply_changes(db, fund, [change]) if change else None


def test_a_withdrawal_can_join_the_months_deposit_without_touching_it(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    deposit_id = _rows(db, ind)[0][0]

    result = _type_and_save(db, SAVINGS, "2,000, @W=500")
    assert not result.refused
    rows = _rows(db, ind)
    assert [(r[1], r[2]) for r in rows] == [("Deposit", 2000), ("Withdrawal", 500)]
    assert rows[0][0] == deposit_id                   # the deposit was left alone
    assert db.funds.balance(SAVINGS, ind) == 1500


def test_plus_adds_to_the_month(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    _type_and_save(db, SAVINGS, "+@W=500")
    assert [(r[1], r[2]) for r in _rows(db, ind)] == [("Deposit", 2000), ("Withdrawal", 500)]


def test_a_bare_figure_sets_the_deposit_and_keeps_the_withdrawal(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    _type_and_save(db, SAVINGS, "2,500")
    assert [(r[1], r[2]) for r in _rows(db, ind)] == [("Deposit", 2500), ("Withdrawal", 500)]


def test_a_deposit_typed_into_a_withdrawal_month_joins_it(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    _type_and_save(db, SAVINGS, "2,000")
    assert sorted((r[1], r[2]) for r in _rows(db, ind)) == [
        ("Deposit", 2000), ("Withdrawal", 500)]


def test_a_withdrawal_typed_alone_keeps_the_deposit(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    _type_and_save(db, SAVINGS, "@W=300")
    assert [(r[1], r[2]) for r in _rows(db, ind)] == [("Deposit", 2000), ("Withdrawal", 300)]


def test_clearing_the_cell_clears_the_whole_month(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    _type_and_save(db, SAVINGS, "")
    assert _rows(db, ind) == []


def test_a_zero_entry_removes_just_that_row(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    _type_and_save(db, SAVINGS, "@D=2,000, @W=0")
    assert [(r[1], r[2]) for r in _rows(db, ind)] == [("Deposit", 2000)]


def test_typing_back_what_is_there_changes_nothing(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    grid = fg.load(db, SAVINGS, MONTHS)
    assert fg.stage(grid, _row(grid), "2026-01", "@W=500, 2000") == (None, None)


def test_a_set_keeps_matching_amounts_and_corrects_the_rest(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-10", "Deposit", 700)
    first, second = (r[0] for r in _rows(db, ind))
    _type_and_save(db, SAVINGS, "@D=900, @D=2,000")
    rows = _rows(db, ind)
    assert {(r[0], r[2]) for r in rows} == {(first, 2000), (second, 900)}


def test_undo_last_save_restores_a_set_exactly(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000, "payroll")
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500, "school fees")
    before = [(r[1], r[2], r[3]) for r in _rows(db, ind)]

    result = _type_and_save(db, SAVINGS, "@D=2,500, @D=100")
    fg.revert(db, SAVINGS, result)
    assert [(r[1], r[2], r[3]) for r in _rows(db, ind)] == before
    assert db.funds.balance(SAVINGS, ind) == 1500


def test_a_benevolent_payout_is_a_withdrawal_named_as_a_payout(db):
    from src.services.benevolent_service import BenevolentService

    ind = _member(db)
    BenevolentService(db).enroll(ind, 300, "2026-01-01")
    _type_and_save(db, BENEVOLENT, "300, @P=10,000")
    rows = _rows(db, ind, BENEVOLENT)
    assert [(r[1], r[2]) for r in rows] == [("Contribution", 300), ("Withdrawal", 10000)]
    assert rows[1][3] == "Benevolent Payout (grid entry)"


def test_one_refused_entry_holds_back_the_whole_month(db):
    """A Christmas withdrawal outside the unlock month refuses the cell — the
    deposit typed beside it is not written on its own."""
    ind = _member(db)
    db.settings.set("christmas_unlock_month", 12)
    db.fund_add_transaction(CHRISTMAS, ind, "2026-01-01", "Deposit", 1000, "")
    grid = fg.load(db, CHRISTMAS, MONTHS)
    change, _ = fg.stage(grid, _row(grid), "2026-01", "1,000, @D=500, @W=200")
    result = fg.apply_changes(db, CHRISTMAS, [change])
    assert result.refused and "only allowed in December" in result.refused[0].reason
    assert [(r[1], r[2]) for r in _rows(db, ind, CHRISTMAS)] == [("Deposit", 1000)]


def test_a_change_made_elsewhere_refuses_a_staged_list(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    grid = fg.load(db, SAVINGS, MONTHS)
    change, _ = fg.stage(grid, _row(grid), "2026-01", "2,000, @W=500")
    db.add_savings_transaction(ind, "2026-01-20", "Deposit", 1)
    result = fg.apply_changes(db, SAVINGS, [change])
    assert result.refused and "changed elsewhere" in result.refused[0].reason


# --------------------------------------------------------------------------- #
# Loans: one operation in the due month
# --------------------------------------------------------------------------- #

LOAN_MONTHS = lg.month_range("2026-01", "2026-06")


@pytest.fixture
def env():
    """Ann's 100,000 loan, deducted for February and March; April is due."""
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "lo.db"), auto_backup=False)
    engine = LoanEngine(d)
    ind = d.add_individual("Ann", "0700", "a@x")
    d.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                      "2026-01-01", "2026-02-01", unearned_interest=15000,
                      duration_months=20)
    d.add_transaction(ind, "2026-01-01", "Loan Issued", "L-001", 100000, 0, 100000,
                      "issue", principal_balance=100000)
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2026-03-01")
    yield d, engine, ind
    d.close()


def _stage_loan(db, text, month="2026-04"):
    grid = lg.load(db, LOAN_MONTHS)
    return lg.stage(grid, next(r for r in grid.rows if r.ref == "L-001"), month, text)


def _loan(db, ind):
    return db.get_loan_by_ref(ind, "L-001")


def test_a_top_up_runs_the_top_up_and_undoes_cleanly(env):
    db, engine, ind = env
    before = _loan(db, ind)
    change, problem = _stage_loan(db, "@T=20,000/12")
    assert problem is None and change.kind == lg.TOP_UP

    result = lg.apply_changes(db, engine, [change])
    assert not result.refused, result.refused
    after = _loan(db, ind)
    assert after["balance"] == pytest.approx(before["balance"] + 20000)
    assert after["duration_months"] == 12
    top_up = db.conn.execute("SELECT date, added FROM ledger WHERE event_type='Loan Top-Up'"
                             ).fetchone()
    assert top_up == ("2026-04-01", 20000)
    assert "1 topped up" in result.summary()

    lg.revert(db, engine, result)
    restored = _loan(db, ind)
    assert restored["balance"] == pytest.approx(before["balance"])
    assert restored["duration_months"] == 20
    assert db.conn.execute("SELECT COUNT(*) FROM ledger WHERE event_type='Loan Top-Up'"
                           ).fetchone()[0] == 0


def test_a_buyoff_settles_the_loan_and_undoes_cleanly(env):
    db, engine, ind = env
    change, problem = _stage_loan(db, "@B")
    assert problem is None
    result = lg.apply_changes(db, engine, [change])
    assert not result.refused, result.refused
    assert _loan(db, ind)["status"] == "Paid"
    assert db.conn.execute("SELECT date FROM ledger WHERE event_type='Loan Buyoff'"
                           ).fetchone()[0] == "2026-04-01"

    lg.revert(db, engine, result)
    assert _loan(db, ind)["status"] == "Active"
    assert db.conn.execute("SELECT COUNT(*) FROM ledger WHERE event_type='Loan Buyoff'"
                           ).fetchone()[0] == 0


def test_a_suspension_locks_its_months_and_undoes_cleanly(env):
    db, engine, ind = env
    change, problem = _stage_loan(db, "@S=2")
    assert problem is None and change.months == 2
    result = lg.apply_changes(db, engine, [change])
    assert not result.refused, result.refused

    spans = db.get_loan_suspensions_for_loan(_loan(db, ind)["id"])
    assert [(s["start_date"], s["suspend_until"]) for s in spans] == [
        ("2026-04-01", "2026-06-01")]
    assert "suspended" in _stage_loan(db, "5750", "2026-04")[1]

    lg.revert(db, engine, result)
    assert db.get_loan_suspensions_for_loan(_loan(db, ind)["id"]) == []


def test_an_operation_only_goes_in_an_empty_due_month(env):
    db, _engine, _ind = env
    _change, problem = _stage_loan(db, "@B", month="2026-03")
    assert "already has its deduction" in problem
    _change, problem = _stage_loan(db, "@B", month="2026-05")
    assert "due for Apr 2026 first" in problem


def test_a_loan_month_takes_one_thing(env):
    db, _engine, _ind = env
    _change, problem = _stage_loan(db, "5750, @S=1")
    assert "one thing" in problem


def test_a_deduction_staged_behind_a_buyoff_is_refused_not_misplaced(env):
    """Each change is checked against the loan as the batch has left it."""
    db, engine, ind = env
    buyoff, _ = _stage_loan(db, "@B")
    loan_id = _loan(db, ind)["id"]
    later = lg.Change(loan_id, ind, "L-001", "2026-05", lg.DEDUCT, amount=5750)
    result = lg.apply_changes(db, engine, [buyoff, later])
    assert [a.change.kind for a in result.applied] == [lg.BUYOFF]
    assert "paid now" in result.refused[0].reason


def test_a_bare_zero_on_a_loan_is_still_refused(env):
    db, _engine, _ind = env
    assert "more than zero" in _stage_loan(db, "0")[1]


def test_pressing_enter_on_a_month_with_a_zero_row_changes_nothing(db):
    """The journal holds zero-amount rows; reading one back must not delete it."""
    ind = _member(db)
    with db.transaction():          # as an import writes it; posting refuses zero
        db.funds.add(SAVINGS, ind, "2026-01-01", "Deposit", 0, 0.0, "imported")
    db.add_savings_transaction(ind, "2026-01-15", "Withdrawal", 500)
    grid = fg.load(db, SAVINGS, MONTHS)
    shown = fg.FundBackend(SAVINGS).edit_text(_row(grid), "2026-01")
    assert shown == "@D=0, @W=500"
    assert fg.stage(grid, _row(grid), "2026-01", shown) == (None, None)


# --------------------------------------------------------------------------- #
# Fill Forward: a deposit every month
# --------------------------------------------------------------------------- #

def _fill(db, fund=SAVINGS, today="2026-03-20"):
    grid = fg.load(db, fund, MONTHS)
    return grid, fg.plan_fill(db, grid, grid.members, MONTHS, today=today)


def test_fill_gives_a_withdrawal_month_its_missing_deposit(db):
    """The reported bug: a withdrawal in January made Fill start in February."""
    ind = _member(db)
    db.add_savings_transaction(ind, "2025-12-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-20", "Withdrawal", 500)

    _grid, plan = _fill(db)
    assert [c.month for c in plan.changes] == ["2026-01", "2026-02", "2026-03"]
    january = plan.changes[0]
    assert january.kind == fg.ChangeKind.SET and january.amount == 1500

    fg.apply_changes(db, SAVINGS, plan.changes)
    january_rows = db.conn.execute(
        "SELECT transaction_type, amount FROM savings WHERE individual_id=? "
        "AND date LIKE '2026-01%' ORDER BY date", (ind,)).fetchall()
    assert january_rows == [("Deposit", 2000), ("Withdrawal", 500)]


def test_fill_leaves_a_month_that_has_its_deposit(db):
    ind = _member(db)
    db.add_savings_transaction(ind, "2025-12-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 2000)
    db.add_savings_transaction(ind, "2026-01-20", "Withdrawal", 500)
    _grid, plan = _fill(db)
    assert [c.month for c in plan.changes] == ["2026-02", "2026-03"]


def test_fill_gives_a_christmas_withdrawal_month_its_deposit(db):
    ind = _member(db)
    db.fund_add_transaction(CHRISTMAS, ind, "2025-12-01", "Deposit", 1000, "")
    db.fund_add_transaction(CHRISTMAS, ind, "2026-01-05", "Withdrawal", 3000, "")
    _grid, plan = _fill(db, CHRISTMAS)
    assert plan.changes[0].month == "2026-01"
    result = fg.apply_changes(db, CHRISTMAS, plan.changes)
    assert not result.refused, [r.reason for r in result.refused]


# --------------------------------------------------------------------------- #
# Loans: suspensions are judged on the due date, and end where they say
# --------------------------------------------------------------------------- #

@pytest.fixture
def due_28th():
    """A loan due on the 28th — where testing the 1st of the month goes wrong."""
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "s.db"), auto_backup=False)
    engine = LoanEngine(d)
    ind = d.add_individual("Ann", "0700", "a@x")
    d.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                      "2026-01-28", "2026-02-28", unearned_interest=15000,
                      duration_months=20)
    d.add_transaction(ind, "2026-01-28", "Loan Issued", "L-001", 100000, 0, 100000,
                      "issue", principal_balance=100000)
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2026-03-28")
    yield d, engine, ind, d.get_loan_by_ref(ind, "L-001")["id"]
    d.close()


def _deduction_dates(db, ind, since="2026-04-01"):
    return [r[0] for r in db.conn.execute(
        "SELECT date FROM ledger WHERE individual_id=? AND event_type='Repayment' "
        "AND date>=? ORDER BY date", (ind, since))]


def test_a_live_suspension_skips_its_months_and_resumes_on_its_end_date(due_28th):
    """28 Apr to 28 Jun: April and May are skipped, June is paid."""
    db, engine, ind, loan_id = due_28th
    db.record_suspension(loan_id, "2026-04-28", "2026-06-28", today="2026-04-01")

    grid = lg.load(db, LOAN_MONTHS)
    row = grid.rows[0]
    assert "resume from 2026-06-28" in lg.cell_refusal(grid, row, "2026-04")
    assert lg.cell_refusal(grid, row, "2026-05") is not None
    assert lg.cell_refusal(grid, row, "2026-06") is None
    assert row.next_due_month == "2026-06"

    plan = lg.plan_fill(db, grid, grid.rows, LOAN_MONTHS, today="2026-06-30")
    assert [c.month for c in plan.changes] == ["2026-06"]
    assert plan.suspended == 2

    result = lg.apply_changes(db, engine, plan.changes)
    assert not result.refused, result.refused[0].reason
    assert _deduction_dates(db, ind) == ["2026-06-28"]
    assert not db.get_loan_by_ref(ind, "L-001")["is_suspended"]


def test_catch_up_resumes_on_the_suspensions_end_date(due_28th):
    """It skipped the end date too: a 2-month suspension cost 3 months."""
    db, engine, ind, loan_id = due_28th
    db.record_suspension(loan_id, "2026-04-28", "2026-06-28", today="2026-04-01")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2026-07-31")
    assert _deduction_dates(db, ind) == ["2026-06-28", "2026-07-28"]


def test_a_suspension_set_from_the_grid_skips_the_due_month(due_28th):
    db, engine, ind, _loan_id = due_28th
    grid = lg.load(db, LOAN_MONTHS)
    change, problem = lg.stage(grid, grid.rows[0], "2026-04", "@S=2")
    assert problem is None
    lg.apply_changes(db, engine, [change])

    grid = lg.load(db, LOAN_MONTHS)
    row = grid.rows[0]
    states = {m: lg.cell_refusal(grid, row, m) is None for m in ("2026-04", "2026-05", "2026-06")}
    assert states == {"2026-04": False, "2026-05": False, "2026-06": True}


def test_a_suspension_resumed_the_day_it_began_skips_nothing(due_28th):
    """The production case behind today's log warnings on 145/L-001."""
    from src.services.loan_events import replay_matches_ledger
    from src.services.loan_projection import project_loan

    db, engine, ind, loan_id = due_28th
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2026-06-28")
    with db.transaction():
        db.loans.add_suspension(loan_id, ind, "L-001", "2026-04-28", "2026-06-28",
                                resumed_date="2026-04-28", status="resumed")
    assert replay_matches_ledger(db, ind, "L-001") == (True, "matches")
    assert project_loan(db, ind, "L-001") == 0      # projects, rather than refusing


def test_the_final_deduction_takes_only_what_is_owed(env):
    """Fill stages the full instalment; the last month owes less."""
    db, engine, ind = env
    with db.transaction():
        db.conn.execute("UPDATE loans SET installment=200000 WHERE individual_id=?", (ind,))
    loan = db.get_loan_by_ref(ind, "L-001")
    owed = loan["balance"] + loan["interest_balance"] + min(750, loan["unearned_interest"])
    change, _ = _stage_loan(db, "200,000")
    result = lg.apply_changes(db, engine, [change])
    assert not result.refused
    paid = db.conn.execute("SELECT deducted FROM ledger WHERE event_type='Repayment' "
                           "ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert paid == pytest.approx(owed)
    assert db.get_loan_by_ref(ind, "L-001")["status"] == "Paid"
