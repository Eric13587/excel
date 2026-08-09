"""The running-total column is a property of the series, not of the row.

`balance` on a fund row is the cumulative total *as at that row*. Anything that
changes the series — inserting out of order, editing an amount, moving a date,
deleting a row — invalidates every row from that point onward.

The API used to leave that to the caller ("caller recalculates afterwards"), so
the column was correct only where somebody had remembered. It was not: an edited
middle row kept its old total, and a row moved to an earlier date carried its
old total to the top of the table.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager

# (table, credit type, debit type)
FUNDS = [("savings", "Deposit", "Withdrawal"),
         ("christmas_savings", "Deposit", "Withdrawal"),
         ("benevolent_ledger", "Contribution", "Withdrawal")]


@pytest.fixture
def db():
    mgr = DatabaseManager(os.path.join(tempfile.mkdtemp(), "f.db"), auto_backup=False)
    yield mgr
    mgr.close()


def _add(db, table, ind, date, ttype, amount, notes=""):
    if table == "savings":
        return db.add_savings_transaction(ind, date, ttype, amount, notes)
    return db.fund_add_transaction(table, ind, date, ttype, amount, notes)


def _rows(db, table, ind):
    return (db.get_savings_transactions(ind) if table == "savings"
            else db.fund_transactions(table, ind))


def _assert_consistent(db, table, ind):
    """Every row's total equals the running sum of everything up to its date."""
    running = 0.0
    frame = _rows(db, table, ind)
    dates = list(frame["date"])
    assert dates == sorted(dates), f"{table} is not in date order"
    for _, row in frame.iterrows():
        amount = float(row["amount"])
        running += -amount if row["transaction_type"] == "Withdrawal" else amount
        assert abs(running - float(row["balance"])) < 0.005, (
            f"{table} row {row['date']} shows {row['balance']}, running sum is {running}")
    return running


@pytest.mark.parametrize("table,credit,debit", FUNDS)
def test_a_back_dated_deduction_reprices_every_row_below_it(db, table, credit, debit):
    """The case that prompted this: insert a deduction in the past and the
    totals from that date onward must all step down by it."""
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-02-01", "2026-05-01"):
        _add(db, table, ind, date, credit, 1000)
    before = [float(r["balance"]) for _, r in _rows(db, table, ind).iterrows()]
    assert before == [1000, 2000, 3000]

    _add(db, table, ind, "2026-03-01", debit, 1500, "back-dated deduction")

    totals = [float(r["balance"]) for _, r in _rows(db, table, ind).iterrows()]
    assert totals == [1000, 2000, 500, 1500]
    _assert_consistent(db, table, ind)


@pytest.mark.parametrize("table,credit,debit", FUNDS)
def test_several_back_dated_rows_interleave_correctly(db, table, credit, debit):
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-02-01", "2026-05-01"):
        _add(db, table, ind, date, credit, 1000)
    _add(db, table, ind, "2026-03-01", debit, 1500)
    _add(db, table, ind, "2026-04-01", credit, 800)

    assert [float(r["balance"]) for _, r in _rows(db, table, ind).iterrows()] == \
        [1000, 2000, 500, 1300, 2300]
    _assert_consistent(db, table, ind)


@pytest.mark.parametrize("table,credit,_debit", FUNDS)
def test_editing_an_amount_reprices_the_rows_below(db, table, credit, _debit):
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-02-01", "2026-03-01"):
        _add(db, table, ind, date, credit, 1000)

    middle = _rows(db, table, ind).iloc[1]
    if table == "savings":
        db.update_savings_transaction(int(middle["id"]), middle["date"], 5000, "raised")
    else:
        db.fund_update_transaction(table, int(middle["id"]), middle["date"], 5000, "raised")

    assert [float(r["balance"]) for _, r in _rows(db, table, ind).iterrows()] == \
        [1000, 6000, 7000]
    _assert_consistent(db, table, ind)


@pytest.mark.parametrize("table,credit,_debit", FUNDS)
def test_moving_a_row_backwards_reorders_and_reprices(db, table, credit, _debit):
    """It used to carry its old total to the top of the table."""
    ind = db.add_individual("M", "07", "m@x")
    for date, amount in (("2026-01-01", 1000), ("2026-02-01", 2000), ("2026-03-01", 4000)):
        _add(db, table, ind, date, credit, amount)

    last = _rows(db, table, ind).iloc[-1]
    if table == "savings":
        db.update_savings_transaction(int(last["id"]), "2025-06-01", last["amount"], "moved")
    else:
        db.fund_update_transaction(table, int(last["id"]), "2025-06-01", last["amount"], "moved")

    frame = _rows(db, table, ind)
    assert list(frame["date"]) == ["2025-06-01", "2026-01-01", "2026-02-01"]
    assert [float(r["balance"]) for _, r in frame.iterrows()] == [4000, 5000, 7000]
    _assert_consistent(db, table, ind)


@pytest.mark.parametrize("table,credit,_debit", FUNDS)
def test_deleting_a_row_reprices_the_rest(db, table, credit, _debit):
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-02-01", "2026-03-01"):
        _add(db, table, ind, date, credit, 1000)

    first = _rows(db, table, ind).iloc[0]
    if table == "savings":
        db.delete_savings_transaction(int(first["id"]))
    else:
        db.fund_delete_transaction(table, int(first["id"]))

    assert [float(r["balance"]) for _, r in _rows(db, table, ind).iterrows()] == [1000, 2000]
    _assert_consistent(db, table, ind)


@pytest.mark.parametrize("table,credit,_debit", FUNDS)
def test_the_last_row_agrees_with_the_reported_balance(db, table, credit, _debit):
    """The figure on the screen header and the bottom of the column are the
    same number arrived at two ways."""
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-04-01"):
        _add(db, table, ind, date, credit, 1200)
    _add(db, table, ind, "2026-02-01", "Withdrawal", 700)

    running = _assert_consistent(db, table, ind)
    reported = (db.get_savings_balance(ind) if table == "savings"
                else db.fund_balance(table, ind))
    assert abs(running - reported) < 0.005


def test_deleting_a_batch_reprices_every_member_it_touched(db):
    ind_a = db.add_individual("A", "07", "a@x")
    ind_b = db.add_individual("B", "07", "b@x")
    for ind in (ind_a, ind_b):
        db.fund_add_transaction("christmas_savings", ind, "2026-01-01", "Deposit", 500)
        db.fund_add_transaction("christmas_savings", ind, "2026-02-01", "Deposit", 500,
                                batch_id="BATCH-1")
        db.fund_add_transaction("christmas_savings", ind, "2026-03-01", "Deposit", 500)

    db.fund_delete_batch("christmas_savings", "BATCH-1")
    for ind in (ind_a, ind_b):
        assert [float(r["balance"]) for _, r
                in db.fund_transactions("christmas_savings", ind).iterrows()] == [500, 1000]
        _assert_consistent(db, "christmas_savings", ind)


def test_appending_in_order_needs_no_repricing(db):
    """The common case must not pay for the rare one."""
    ind = db.add_individual("M", "07", "m@x")
    for i in range(1, 6):
        db.add_savings_transaction(ind, f"2026-0{i}-01", "Deposit", 100, "")
    assert [float(r["balance"]) for _, r in db.get_savings_transactions(ind).iterrows()] == \
        [100, 200, 300, 400, 500]
