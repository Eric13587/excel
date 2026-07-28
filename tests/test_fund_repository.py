"""Phase 2d — FundRepository.

Pins the behaviour that is being *preserved*, not just the new API. Two known
defects survive this refactor deliberately (three divergent balance semantics,
and only savings firing the GL hook); the tests below make both explicit so
Phase 3 and Phase 5 have a baseline to change against.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.db.fund import BENEVOLENT, CHRISTMAS, SAVINGS, FundRepository


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "f.db"))
    yield db
    db.close()


def _member(db, name="Saver"):
    return db.add_individual(name, "0700", "s@x.com")


# --------------------------------------------------------------------------- #
# Table whitelisting
# --------------------------------------------------------------------------- #

def test_table_name_is_whitelisted(journal):
    for bad in ("individuals", "ledger", "savings; DROP TABLE individuals", ""):
        with pytest.raises(ValueError):
            journal.funds.latest_balance(bad, 1)


def test_all_three_subledgers_are_reachable(journal):
    ind = _member(journal)
    for table in (SAVINGS, CHRISTMAS, BENEVOLENT):
        journal.funds.add(table, ind, "2026-01-01", "Deposit", 100, 100)
    journal.conn.commit()
    for table in (SAVINGS, CHRISTMAS, BENEVOLENT):
        assert journal.funds.latest_balance(table, ind) == 100


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #

def test_latest_balance_reads_the_most_recent_row(journal):
    ind = _member(journal)
    journal.add_savings_transaction(ind, "2026-01-01", "Deposit", 1000)
    journal.add_savings_transaction(ind, "2026-02-01", "Deposit", 500)
    assert journal.get_savings_balance(ind) == 1500
    journal.add_savings_transaction(ind, "2026-03-01", "Withdrawal", 200)
    assert journal.get_savings_balance(ind) == 1300


def test_latest_balance_is_zero_for_a_member_with_no_history(journal):
    assert journal.get_savings_balance(_member(journal)) == 0.0


def test_fund_add_rounds_and_directs_by_type(journal):
    """Withdrawals reduce, everything else increases, and each step is rounded
    to 2dp — which is what keeps 0.1 + 0.2 from accumulating as
    0.30000000000000004. Note the rounding is `round()` on a binary float, so
    it is not decimal-exact (round(100.005, 2) is 100.0); that is the reason
    Phase 9 moves money to integer minor units."""
    ind = _member(journal)
    assert journal.fund_add_transaction(CHRISTMAS, ind, "2026-01-01", "Deposit", 0.1) == 0.1
    assert journal.fund_add_transaction(CHRISTMAS, ind, "2026-02-01", "Deposit", 0.2) == 0.3
    assert journal.fund_add_transaction(CHRISTMAS, ind, "2026-03-01", "Withdrawal", 0.05) == 0.25


# --------------------------------------------------------------------------- #
# The three recalculation semantics (finding D1) — preserved, not reconciled
# --------------------------------------------------------------------------- #

def _seed_with_interest(db, ind):
    """Deposit 1000, Interest 100, Withdrawal 200 — balances differ by method."""
    cur = db.conn.cursor()
    for date, ttype, amount in (("2026-01-01", "Deposit", 1000),
                                ("2026-02-01", "Interest", 100),
                                ("2026-03-01", "Withdrawal", 200)):
        cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                    "VALUES (?, ?, ?, ?, 0)", (ind, date, ttype, amount))
    db.conn.commit()


def test_recalc_by_date_treats_interest_as_an_increase(journal):
    ind = _member(journal)
    _seed_with_interest(journal, ind)
    journal.recalculate_savings_balance(ind)
    assert journal.get_savings_balance(ind) == 900        # 1000 + 100 - 200


def test_recalc_by_id_treats_interest_as_a_decrease(journal):
    """The sibling implementation disagrees. Preserved verbatim; Phase 3
    reconciles them (docs/db/inconsistent-sql.md D1)."""
    ind = _member(journal)
    _seed_with_interest(journal, ind)
    journal.recalculate_savings_balances(ind)
    assert journal.get_savings_balance(ind) == 700        # 1000 - 100 - 200


def test_recalc_fund_rounds_to_cents(journal):
    ind = _member(journal)
    for date, amount in (("2026-01-01", 0.1), ("2026-02-01", 0.2)):
        journal.funds.add(CHRISTMAS, ind, date, "Deposit", amount, 0)
    journal.conn.commit()
    journal.fund_recalculate(CHRISTMAS, ind)
    assert journal.funds.latest_balance(CHRISTMAS, ind) == 0.3   # not 0.30000000000000004


def test_recalc_by_date_accepts_a_caller_cursor(journal):
    """The import path passes its own cursor so the work joins its transaction."""
    ind = _member(journal)
    _seed_with_interest(journal, ind)
    cur = journal.conn.cursor()
    journal.recalculate_savings_balance(ind, cursor=cur)
    journal.conn.commit()
    assert journal.get_savings_balance(ind) == 900


# --------------------------------------------------------------------------- #
# Batch helpers
# --------------------------------------------------------------------------- #

def test_members_in_batch_and_delete_batch(journal):
    a, b = _member(journal, "A"), _member(journal, "B")
    for ind in (a, b):
        journal.add_savings_transaction(ind, "2026-01-01", "Deposit", 100, batch_id="BATCH-1")
    journal.add_savings_transaction(a, "2026-02-01", "Deposit", 50, batch_id="BATCH-2")

    assert sorted(journal.funds.members_in_batch(SAVINGS, "BATCH-1")) == sorted([a, b])
    journal.delete_savings_batch("BATCH-1")
    assert journal.funds.members_in_batch(SAVINGS, "BATCH-1") == []
    assert journal.funds.members_in_batch(SAVINGS, "BATCH-2") == [a]


def test_earliest_date_per_member_in_batch(journal):
    """Benevolent revert restores each member's next-due to the batch's first date."""
    a, b = _member(journal, "A"), _member(journal, "B")
    journal.fund_add_transaction(BENEVOLENT, a, "2026-03-01", "Contribution", 50, batch_id="B1")
    journal.fund_add_transaction(BENEVOLENT, a, "2026-04-01", "Contribution", 50, batch_id="B1")
    journal.fund_add_transaction(BENEVOLENT, b, "2026-02-01", "Contribution", 50, batch_id="B1")

    got = dict(journal.funds.earliest_date_per_member_in_batch(BENEVOLENT, "B1"))
    assert got == {a: "2026-03-01", b: "2026-02-01"}


def test_delete_all_for_member_leaves_others_alone(journal):
    a, b = _member(journal, "A"), _member(journal, "B")
    for ind in (a, b):
        journal.add_savings_transaction(ind, "2026-01-01", "Deposit", 100)
    journal.fund_delete_all(SAVINGS, a)
    assert journal.get_savings_balance(a) == 0.0
    assert journal.get_savings_balance(b) == 100


# --------------------------------------------------------------------------- #
# Row-level edits
# --------------------------------------------------------------------------- #

def test_get_update_and_delete_a_row(journal):
    ind = _member(journal)
    journal.fund_add_transaction(CHRISTMAS, ind, "2026-01-01", "Deposit", 100, notes="orig")
    row_id = journal.funds.transactions(CHRISTMAS, ind).iloc[0]["id"]

    row = journal.fund_get_transaction(CHRISTMAS, int(row_id))
    assert row["notes"] == "orig" and row["amount"] == 100

    journal.fund_update_transaction(CHRISTMAS, int(row_id), "2026-01-15", 250, "edited")
    row = journal.fund_get_transaction(CHRISTMAS, int(row_id))
    assert (row["date"], row["amount"], row["notes"]) == ("2026-01-15", 250, "edited")

    journal.fund_delete_transaction(CHRISTMAS, int(row_id))
    assert journal.fund_get_transaction(CHRISTMAS, int(row_id)) is None


# --------------------------------------------------------------------------- #
# Benevolent enrolment (1:0..1)
# --------------------------------------------------------------------------- #

def test_enrolment_upsert_is_idempotent(journal):
    ind = _member(journal)
    journal.upsert_benevolent_account(ind, 300, "2026-01-01", "2026-02-01")
    journal.upsert_benevolent_account(ind, 500, "2026-01-01", "2026-03-01")

    acc = journal.get_benevolent_account(ind)
    assert acc["monthly_amount"] == 500 and acc["next_due_date"] == "2026-03-01"
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM benevolent_accounts WHERE individual_id=?", (ind,)).fetchone()[0] == 1


def test_enrolment_absent_returns_none(journal):
    assert journal.get_benevolent_account(_member(journal)) is None


def test_set_next_due(journal):
    ind = _member(journal)
    journal.upsert_benevolent_account(ind, 300, "2026-01-01", "2026-02-01")
    journal.set_benevolent_next_due(ind, "2026-06-01")
    assert journal.get_benevolent_account(ind)["next_due_date"] == "2026-06-01"


# --------------------------------------------------------------------------- #
# Post-hook asymmetry (finding preserved for Phase 5)
# --------------------------------------------------------------------------- #

def test_only_savings_fires_the_gl_post_hook(journal):
    """Christmas and Benevolent writes reach the GL only on a full sync — which
    is how 12 Christmas deposits went unposted in production. Preserved here so
    Phase 5 has a baseline to change."""
    fired = []
    journal.savings_post_hook = fired.append
    ind = _member(journal)

    journal.add_savings_transaction(ind, "2026-01-01", "Deposit", 100)
    assert len(fired) == 1

    journal.fund_add_transaction(CHRISTMAS, ind, "2026-01-01", "Deposit", 100)
    journal.fund_add_transaction(BENEVOLENT, ind, "2026-01-01", "Contribution", 50)
    assert len(fired) == 1, "fund writes must not post yet (Phase 5 changes this)"


# --------------------------------------------------------------------------- #
# Standalone use
# --------------------------------------------------------------------------- #

def test_repository_works_against_a_bare_connection(journal):
    repo = FundRepository(journal.conn)
    ind = _member(journal)
    repo.add(SAVINGS, ind, "2026-01-01", "Deposit", 10, 10)
    journal.conn.commit()
    assert repo.latest_balance(SAVINGS, ind) == 10
