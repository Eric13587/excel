"""Rules that run before an operation.

`is_retired` and `employment_status` existed and were read only by reports, so
nothing stopped a catch-up from generating a year of contributions for somebody
who left in March. These pin what is now refused — and, just as importantly,
what is still allowed.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services.policy import (ACTIVE, DECEASED, INACTIVE_STATUSES, RESIGNED,
                                 RETIRED, Operation, OperationNotAllowed, Policy,
                                 default_policy)


@pytest.fixture
def db():
    mgr = DatabaseManager(os.path.join(tempfile.mkdtemp(), "p.db"), auto_backup=False)
    yield mgr
    mgr.close()


def _member(db, status=ACTIVE, name="Member"):
    ind = db.add_individual(name, "07", "m@x")
    if status != ACTIVE:
        db.set_membership_status(ind, status, "2025-06-30")
    return ind


# --------------------------------------------------------------------------- #
# The rule that matters
# --------------------------------------------------------------------------- #

def test_an_active_member_may_accrue(db):
    ind = _member(db)
    assert default_policy.allows(db, Operation("savings.catch_up", member_id=ind))


@pytest.mark.parametrize("status", INACTIVE_STATUSES)
def test_a_member_who_has_left_may_not_accrue(db, status):
    ind = _member(db, status)
    refusals = default_policy.check(db, Operation("savings.catch_up", member_id=ind))
    assert refusals and refusals[0].rule == "member_must_be_active"


def test_the_refusal_says_who_what_and_when(db):
    """It is read by a clerk, not a developer."""
    ind = _member(db, RETIRED, name="Jane Wanjiru")
    reason = default_policy.check(db, Operation("savings.catch_up", member_id=ind))[0].reason
    assert "Jane Wanjiru" in reason
    assert "has retired" in reason and "2025-06-30" in reason
    assert "still allowed" in reason, "it must say what they CAN do"


def test_each_status_reads_naturally(db):
    for status, phrase in ((RETIRED, "has retired"), (RESIGNED, "has resigned"),
                           (DECEASED, "is deceased")):
        ind = _member(db, status, name=f"M{status}")
        reason = default_policy.check(db, Operation("loan.catch_up", member_id=ind))[0].reason
        assert phrase in reason


# --------------------------------------------------------------------------- #
# What a departure must NOT prevent
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind", ["loan.repayment", "savings.withdrawal",
                                  "benevolent.payout", "ledger.edit"])
def test_a_member_who_left_can_still_settle_up(db, kind):
    """Leaving does not erase what you owe or what you are owed. Blocking
    everything would trap a retired member's own savings."""
    ind = _member(db, RETIRED)
    assert default_policy.allows(db, Operation(kind, member_id=ind))


def test_a_missing_member_is_refused_by_name(db):
    refusals = default_policy.check(db, Operation("savings.catch_up", member_id=9999))
    assert refusals[0].rule == "member_must_exist"


# --------------------------------------------------------------------------- #
# Deciding vs doing
# --------------------------------------------------------------------------- #

def test_partition_splits_a_batch_without_running_it(db):
    """A mass operation greys out who it cannot serve rather than failing
    partway through."""
    active = [_member(db, name=f"A{i}") for i in range(3)]
    gone = [_member(db, RETIRED, name=f"R{i}") for i in range(2)]

    allowed, blocked = default_policy.partition(
        db, "savings.catch_up", active + gone)
    assert sorted(allowed) == sorted(active)
    assert sorted(m for m, _ in blocked) == sorted(gone)
    assert all("has retired" in r.reason for _, r in blocked)


def test_check_never_writes(db):
    ind = _member(db, RETIRED)
    before = db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0]
    default_policy.check(db, Operation("savings.catch_up", member_id=ind))
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == before


def test_a_broken_rule_does_not_block_the_operation(db):
    """A rule that throws is a bug in the rule, not grounds to stop work."""
    def explodes(_db, _op):
        raise RuntimeError("bad rule")

    policy = Policy(rules=(explodes,))
    assert policy.allows(db, Operation("savings.catch_up", member_id=_member(db)))


# --------------------------------------------------------------------------- #
# Enforcement at the point of writing
# --------------------------------------------------------------------------- #

def test_savings_catch_up_is_refused_for_a_retired_member(db):
    ind = _member(db)                       # history first — see the note below
    db.add_savings_transaction(ind, "2025-01-01", "Deposit", 1000, "")
    db.set_membership_status(ind, RETIRED, "2025-06-30")
    engine = LoanEngine(db)
    before = db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0]

    with pytest.raises(OperationNotAllowed):
        engine.savings_service.catch_up_savings(ind)
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == before


def test_christmas_and_benevolent_catch_up_are_refused_too(db):
    ind = _member(db, RESIGNED)
    engine = LoanEngine(db)
    with pytest.raises(OperationNotAllowed):
        engine.christmas_service.catch_up(ind)
    with pytest.raises(OperationNotAllowed):
        engine.benevolent_service.catch_up(ind)


def test_loan_catch_up_is_refused_for_a_departed_member(db):
    ind = _member(db, DECEASED)
    db.add_loan_record(ind, "L-001", 10000, 11500, 10000, 1000, 150,
                       "2025-01-01", "2025-02-01")
    engine = LoanEngine(db)
    with pytest.raises(OperationNotAllowed):
        engine.loan_service.catch_up_loan(ind, "L-001")


# --------------------------------------------------------------------------- #
# The status column itself
# --------------------------------------------------------------------------- #

def test_retiring_a_member_sets_both_columns(db):
    """employment_status is authoritative and is_retired mirrors it, so reports
    reading the old flag and policy reading the new word cannot disagree."""
    ind = _member(db)
    db.retire_individual(ind, "2025-06-30")
    status, flag = db.conn.execute(
        "SELECT employment_status, is_retired FROM individuals WHERE id=?", (ind,)).fetchone()
    assert (status, flag) == (RETIRED, 1)


def test_reinstating_clears_both(db):
    ind = _member(db)
    db.retire_individual(ind, "2025-06-30")
    db.reinstate_individual(ind)
    status, flag = db.conn.execute(
        "SELECT employment_status, is_retired FROM individuals WHERE id=?", (ind,)).fetchone()
    assert (status, flag) == (ACTIVE, 0)
    assert default_policy.allows(db, Operation("savings.catch_up", member_id=ind))


def test_an_unknown_status_is_refused(db):
    """A typo would read as 'not active' and silently stop contributions."""
    ind = _member(db)
    with pytest.raises(ValueError, match="not a membership status"):
        db.set_membership_status(ind, "Retiredd", "2025-06-30")

    # And the database refuses it too, for anything that bypasses the method.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE individuals SET employment_status='Retiredd', "
                        "is_retired=1, retired_date='2025-06-30' WHERE id=?", (ind,))
    db.conn.rollback()


# --------------------------------------------------------------------------- #
# Back-dated fund rows belong where they happened
# --------------------------------------------------------------------------- #

def test_a_back_dated_deposit_reads_in_date_order(db):
    """It used to appear at the bottom of the table, because savings was read
    in insertion order while the other two funds were read by date."""
    ind = _member(db)
    for date, amount in (("2026-03-01", 1000), ("2026-04-01", 1000)):
        db.add_savings_transaction(ind, date, "Deposit", amount, "")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 500, "back-dated")

    shown = db.get_savings_transactions(ind)
    assert list(shown["date"]) == sorted(shown["date"])
    assert shown.iloc[0]["notes"] == "back-dated"


def test_a_back_dated_deposit_fixes_the_running_balance(db):
    """The stored balance is the running total *as at insertion*, so a
    back-dated row left itself and every row after it wrong."""
    ind = _member(db)
    for date, amount in (("2026-03-01", 1000), ("2026-04-01", 1000)):
        db.add_savings_transaction(ind, date, "Deposit", amount, "")
    db.add_savings_transaction(ind, "2026-02-01", "Deposit", 500, "back-dated")

    balances = list(db.get_savings_transactions(ind)["balance"])
    assert balances == [500, 1500, 2500]
    assert balances[-1] == db.get_savings_balance(ind)


def test_the_other_funds_behave_the_same_way(db):
    ind = _member(db)
    db.fund_add_transaction("benevolent_ledger", ind, "2026-03-01", "Contribution", 300)
    db.fund_add_transaction("benevolent_ledger", ind, "2026-01-01", "Contribution", 200)

    rows = db.fund_transactions("benevolent_ledger", ind)
    assert list(rows["date"]) == ["2026-01-01", "2026-03-01"]
    assert list(rows["balance"]) == [200, 500]


def test_appending_in_order_still_costs_no_recalculation(db):
    """The common case must not pay for the rare one."""
    ind = _member(db)
    for i in range(1, 5):
        db.add_savings_transaction(ind, f"2026-0{i}-01", "Deposit", 100, "")
    assert list(db.get_savings_transactions(ind)["balance"]) == [100, 200, 300, 400]


def test_a_deposit_for_a_departed_member_is_refused_too(db):
    """Not just catch-up. A single deposit is money going in, so it is a new
    obligation; the member's own withdrawal is not."""
    from src.services.posting import PostingError
    ind = _member(db)
    db.add_savings_transaction(ind, "2025-01-01", "Deposit", 1000, "")
    db.set_membership_status(ind, RETIRED, "2025-06-30")

    with pytest.raises(PostingError, match="has retired"):
        db.add_savings_transaction(ind, "2025-08-01", "Deposit", 500, "")

    # …but they can still take their savings out.
    assert db.add_savings_transaction(ind, "2025-08-01", "Withdrawal", 400, "") == 600
