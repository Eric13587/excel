"""The one write path for the three funds.

`add_savings_transaction` and `fund_add_transaction` did the same job with
different rules, and the difference was a bug: savings added anything that was
not a 'Deposit' as a *reduction*, so an Interest credit subtracted, disagreeing
with FundRepository and with the other two funds.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.services.posting import (FUND_TYPES, Posting, PostingError, check,
                                  normalise, post, validate)

FUNDS = ["savings", "christmas_savings", "benevolent_ledger"]


@pytest.fixture
def db():
    mgr = DatabaseManager(os.path.join(tempfile.mkdtemp(), "p.db"), auto_backup=False)
    yield mgr
    mgr.close()


def _p(table="savings", member_id=1, date="2026-01-01", kind="Deposit", amount=100, **kw):
    return Posting(table=table, member_id=member_id, date=date, kind=kind,
                   amount=amount, **kw)


# --------------------------------------------------------------------------- #
# The bug this ends
# --------------------------------------------------------------------------- #

def test_interest_credits_savings_rather_than_debiting_it(db):
    """500 of interest on 1,000 is 1,500, not 500."""
    ind = db.add_individual("M", "07", "m@x")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000, "")
    returned = db.add_savings_transaction(ind, "2026-02-01", "Interest", 500, "")

    assert returned == 1500
    assert db.get_savings_balance(ind) == 1500
    assert float(db.get_savings_transactions(ind).iloc[-1]["balance"]) == 1500


def test_the_returned_balance_survives_a_recalculation(db):
    """The old savings path returned a figure that a later recalculation
    silently contradicted."""
    ind = db.add_individual("M", "07", "m@x")
    db.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000, "")
    returned = db.add_savings_transaction(ind, "2026-02-01", "Interest", 500, "")
    db.fund_recalculate("savings", ind)
    assert float(db.get_savings_transactions(ind).iloc[-1]["balance"]) == returned


@pytest.mark.parametrize("table", FUNDS)
def test_only_a_withdrawal_takes_money_out(db, table):
    ind = db.add_individual("M", "07", "m@x")
    credit = "Contribution" if table == "benevolent_ledger" else "Deposit"
    db.fund_add_transaction(table, ind, "2026-01-01", credit, 1000)
    db.fund_add_transaction(table, ind, "2026-02-01", "Interest", 200)
    db.fund_add_transaction(table, ind, "2026-03-01", "Withdrawal", 300)
    assert db.fund_balance(table, ind) == 900


# --------------------------------------------------------------------------- #
# normalise
# --------------------------------------------------------------------------- #

def test_dates_arrive_in_several_shapes():
    for given in ("2026-04-01", "2026/04/01", "20260401"):
        assert normalise(_p(date=given)).date == "2026-04-01"


def test_a_type_is_matched_regardless_of_case():
    assert normalise(_p(kind="deposit")).kind == "Deposit"
    assert normalise(_p(kind="  WITHDRAWAL ")).kind == "Withdrawal"


def test_money_is_rounded_to_cents_and_notes_trimmed():
    """Deliberately not a knife-edge value: 100.005 is not representable in
    binary float and rounds down, which is a property of floats rather than of
    this code. CLAUDE.md is explicit that rounding must not be "fixed" piecemeal."""
    tidy = normalise(_p(amount=100.4567, notes="  late  "))
    assert tidy.amount == 100.46 and tidy.notes == "late"


def test_normalise_never_raises_on_rubbish():
    tidy = normalise(_p(amount="abc", date=None, kind=None))
    assert validate(tidy), "it defers judgement to validate"


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #

def test_a_good_posting_has_no_problems():
    assert validate(normalise(_p())) == []


def test_an_unknown_type_is_named_with_the_alternatives():
    problems = validate(normalise(_p(kind="Bonus")))
    assert problems and "Deposit, Withdrawal, Interest" in problems[0]


def test_a_negative_amount_is_refused_with_the_reason():
    """The sign belongs to the type; a withdrawal of -500 would read as money
    going in."""
    problems = validate(normalise(_p(amount=-500)))
    assert any("greater than zero" in p and "Withdrawal" in p for p in problems)


def test_zero_is_refused():
    assert validate(normalise(_p(amount=0)))


def test_a_malformed_date_is_refused():
    assert any("is not a date" in p for p in validate(normalise(_p(date="not-a-date"))))


def test_an_unknown_fund_is_refused():
    assert any("is not a fund" in p for p in validate(normalise(_p(table="wallet"))))


def test_every_problem_is_reported_at_once():
    """A form should not make the user fix one thing to discover the next."""
    problems = validate(normalise(_p(kind="Bonus", amount=-1, date="nope")))
    assert len(problems) >= 3


# --------------------------------------------------------------------------- #
# check: deciding without writing
# --------------------------------------------------------------------------- #

def test_check_writes_nothing(db):
    ind = db.add_individual("M", "07", "m@x")
    before = db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0]
    check(db, _p(member_id=ind, kind="Bonus"))
    check(db, _p(member_id=ind))
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == before


def test_check_reports_a_departed_member(db):
    ind = db.add_individual("M", "07", "m@x")
    db.set_membership_status(ind, "Retired", "2025-06-30")
    _tidy, problems = check(db, _p(member_id=ind))
    assert any("has retired" in p for p in problems)


def test_check_lets_a_departed_member_withdraw(db):
    ind = db.add_individual("M", "07", "m@x")
    db.set_membership_status(ind, "Retired", "2025-06-30")
    _tidy, problems = check(db, _p(member_id=ind, kind="Withdrawal"))
    assert problems == []


def test_a_missing_member_is_reported(db):
    _tidy, problems = check(db, _p(member_id=9999, kind="Withdrawal"))
    assert any("no longer exists" in p for p in problems)


# --------------------------------------------------------------------------- #
# post
# --------------------------------------------------------------------------- #

def test_post_returns_the_new_balance(db):
    ind = db.add_individual("M", "07", "m@x")
    assert post(db, _p(member_id=ind, amount=250)) == 250
    assert post(db, _p(member_id=ind, date="2026-02-01", amount=250)) == 500


def test_post_refuses_with_a_sentence_not_a_constraint_error(db):
    """It used to surface as 'CHECK constraint failed: amount >= 0'."""
    ind = db.add_individual("M", "07", "m@x")
    with pytest.raises(PostingError) as excinfo:
        post(db, _p(member_id=ind, amount=-5))
    assert "CHECK" not in str(excinfo.value)
    assert "greater than zero" in str(excinfo.value)


def test_a_refused_posting_writes_nothing(db):
    ind = db.add_individual("M", "07", "m@x")
    with pytest.raises(PostingError):
        post(db, _p(member_id=ind, kind="Bonus"))
    assert db.conn.execute("SELECT COUNT(*) FROM savings").fetchone()[0] == 0


def test_a_back_dated_posting_still_reprices_the_rows_below(db):
    ind = db.add_individual("M", "07", "m@x")
    for date in ("2026-01-01", "2026-03-01"):
        post(db, _p(member_id=ind, date=date, amount=1000))
    post(db, _p(member_id=ind, date="2026-02-01", kind="Withdrawal", amount=400))

    frame = db.get_savings_transactions(ind)
    assert list(frame["date"]) == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert [float(b) for b in frame["balance"]] == [1000, 600, 1600]


def test_every_fund_declares_its_own_vocabulary():
    assert "Contribution" in FUND_TYPES["benevolent_ledger"]
    assert "Deposit" not in FUND_TYPES["benevolent_ledger"]
    assert "Deposit" in FUND_TYPES["savings"]
