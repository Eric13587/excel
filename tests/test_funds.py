"""Tests for the Christmas and Benevolent funds."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.services.christmas_service import ChristmasService
from src.services.benevolent_service import BenevolentService
from src.exceptions import ChristmasLockedError


@pytest.fixture
def env():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "f.db"))
    ind = db.add_individual("Jane", "0", "j@x")
    return db, ind


# --------------------------------------------------------------------------- #
# Christmas fund
# --------------------------------------------------------------------------- #
def test_christmas_deposit_and_balance(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    svc.add_deposit(ind, 500, "2026-02-01")
    assert svc.get_balance(ind) == 1500.0


def test_christmas_withdrawal_locked_outside_unlock_month(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    # June -> locked
    with pytest.raises(ChristmasLockedError):
        svc.add_withdrawal(ind, 200, "2026-06-15")
    # December (default unlock month) -> allowed
    svc.add_withdrawal(ind, 200, "2026-12-15")
    assert svc.get_balance(ind) == 800.0


def test_christmas_withdrawal_override(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    svc.add_withdrawal(ind, 300, "2026-06-15", allow_override=True)  # admin override
    assert svc.get_balance(ind) == 700.0


def test_christmas_unlock_month_setting(env):
    db, ind = env
    db.set_setting("christmas_unlock_month", "8")  # August
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    with pytest.raises(ChristmasLockedError):
        svc.add_withdrawal(ind, 100, "2026-12-15")  # Dec now locked
    svc.add_withdrawal(ind, 100, "2026-08-15")       # August allowed
    assert svc.get_balance(ind) == 900.0


def test_christmas_catch_up(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 500, "2026-01-01")
    n = svc.catch_up(ind, 500, target_date="2026-04-01")  # Feb, Mar, Apr
    assert n == 3
    assert svc.get_balance(ind) == 2000.0


def test_christmas_recalculate(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    svc.add_withdrawal(ind, 400, "2026-12-15")
    # Corrupt a balance, then recalc
    db.conn.execute("UPDATE christmas_savings SET balance=99999 WHERE id=1")
    db.conn.commit()
    svc.recalculate(ind)
    assert svc.get_balance(ind) == 600.0


# --------------------------------------------------------------------------- #
# Benevolent fund
# --------------------------------------------------------------------------- #
def test_benevolent_enroll(env):
    db, ind = env
    svc = BenevolentService(db)
    svc.enroll(ind, 200, "2026-01-01")
    acc = svc.get_account(ind)
    assert acc['monthly_amount'] == 200 and acc['next_due_date'] == "2026-01-01"
    assert svc.is_enrolled(ind)


def test_benevolent_deduct_single_advances_schedule(env):
    db, ind = env
    svc = BenevolentService(db)
    svc.enroll(ind, 200, "2026-01-01")
    assert svc.deduct_single(ind) == 1
    assert svc.get_total(ind) == 200.0
    assert svc.get_account(ind)['next_due_date'] == "2026-02-01"


def test_benevolent_catch_up_accumulates(env):
    db, ind = env
    svc = BenevolentService(db)
    svc.enroll(ind, 200, "2026-01-01")
    n = svc.catch_up(ind, target_date="2026-05-01")  # Jan..May
    assert n == 5
    assert svc.get_total(ind) == 1000.0
    assert svc.get_account(ind)['next_due_date'] == "2026-06-01"


def test_benevolent_not_enrolled_noops(env):
    db, ind = env
    svc = BenevolentService(db)
    assert svc.deduct_single(ind) == 0
    assert svc.catch_up(ind, target_date="2026-12-01") == 0
    assert not svc.is_enrolled(ind)


def test_benevolent_recalculate(env):
    db, ind = env
    svc = BenevolentService(db)
    svc.enroll(ind, 150, "2026-01-01")
    svc.catch_up(ind, target_date="2026-03-01")  # 3 contributions -> 450
    db.conn.execute("UPDATE benevolent_ledger SET balance=0 WHERE id=2")
    db.conn.commit()
    svc.recalculate(ind)
    assert svc.get_total(ind) == 450.0


# --------------------------------------------------------------------------- #
# Per-row edit / delete (generic fund DB layer)
# --------------------------------------------------------------------------- #
def test_fund_update_and_recalculate(env):
    db, ind = env
    svc = ChristmasService(db)
    svc.add_deposit(ind, 1000, "2026-01-01")
    svc.add_deposit(ind, 500, "2026-02-01")
    tid = db.fund_transactions("christmas_savings", ind).iloc[0]['id']
    db.fund_update_transaction("christmas_savings", int(tid), "2026-01-01", 1200, "edited")
    db.fund_recalculate("christmas_savings", ind)
    assert svc.get_balance(ind) == 1700.0  # 1200 + 500
    assert db.fund_get_transaction("christmas_savings", int(tid))['notes'] == "edited"


def test_fund_delete_and_recalculate(env):
    db, ind = env
    svc = BenevolentService(db)
    svc.enroll(ind, 200, "2026-01-01")
    svc.catch_up(ind, target_date="2026-03-01")  # 3 contributions -> 600
    tid = db.fund_transactions("benevolent_ledger", ind).iloc[1]['id']
    db.fund_delete_transaction("benevolent_ledger", int(tid))
    db.fund_recalculate("benevolent_ledger", ind)
    assert svc.get_total(ind) == 400.0  # one of three removed


def test_fund_table_whitelist(env):
    db, _ = env
    with pytest.raises(ValueError):
        db.fund_balance("savings; DROP TABLE loans", 1)


# --------------------------------------------------------------------------- #
# Mass catch-up
# --------------------------------------------------------------------------- #
def test_mass_catch_up_christmas(env):
    db, ind1 = env
    ind2 = db.add_individual("Bob", "0", "b@x")
    c = ChristmasService(db)
    c.add_deposit(ind1, 500, "2026-01-01")  # ind1 has a starting deposit
    # ind2 has none -> should be skipped (no-op)
    processed, total, _b, errors = c.mass_catch_up([ind1, ind2], target_date="2026-04-01")
    assert processed == 1 and total == 3 and errors == []
    assert c.get_balance(ind2) == 0.0


def test_mass_catch_up_benevolent(env):
    db, ind1 = env
    ind2 = db.add_individual("Bob", "0", "b@x")
    b = BenevolentService(db)
    b.enroll(ind1, 100, "2026-01-01")  # only ind1 enrolled
    processed, total, _b, errors = b.mass_catch_up([ind1, ind2], target_date="2026-03-01")
    assert processed == 1 and total == 3
    assert b.get_total(ind2) == 0.0


# --------------------------------------------------------------------------- #
# Undo support for mass catch-up
# --------------------------------------------------------------------------- #
def test_undo_mass_christmas(env):
    from src.engine import LoanEngine
    db, ind = env
    eng = LoanEngine(db)
    eng.christmas_service.add_deposit(ind, 500, "2026-01-01")
    eng.mass_catch_up_christmas([ind], target_date="2026-04-01")  # +3 -> 2000
    assert eng.christmas_service.get_balance(ind) == 2000.0
    assert eng.can_undo()
    eng.undo()
    assert eng.christmas_service.get_balance(ind) == 500.0  # reverted


def test_undo_mass_benevolent_restores_next_due(env):
    from src.engine import LoanEngine
    db, ind = env
    eng = LoanEngine(db)
    eng.benevolent_service.enroll(ind, 100, "2026-01-01")
    eng.mass_catch_up_benevolent([ind], target_date="2026-03-01")  # Jan..Mar -> 300
    assert eng.benevolent_service.get_total(ind) == 300.0
    assert eng.benevolent_service.get_account(ind)['next_due_date'] == "2026-04-01"
    eng.undo()
    assert eng.benevolent_service.get_total(ind) == 0.0
    # schedule restored to where it was before the run
    assert eng.benevolent_service.get_account(ind)['next_due_date'] == "2026-01-01"


def test_funds_are_independent_of_savings(env):
    """Christmas/Benevolent must not touch the regular savings pot."""
    db, ind = env
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 5000, "real savings")
    ChristmasService(db).add_deposit(ind, 1000, "2026-01-01")
    BenevolentService(db).enroll(ind, 200, "2026-01-01")
    BenevolentService(db).deduct_single(ind)
    assert db.get_savings_balance(ind) == 5000.0  # unchanged
    assert ChristmasService(db).get_balance(ind) == 1000.0
    assert BenevolentService(db).get_total(ind) == 200.0
