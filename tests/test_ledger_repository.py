"""Phase 2c-ii — LedgerRepository.

The ledger is the journal's source of truth, so these lean hard on preserved
semantics: the edit-lock scopes, the "amount not flag" quirk, the bulk-insert
id range the GL hook depends on, and the two-table blind spot in the
earliest-date query.
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "lg.db"))
    yield db
    db.close()


def _member(db, name="Borrower"):
    return db.add_individual(name, "0700", "b@x.com")


def _row(db, member_id, date, event_type="Repayment", loan_ref="L-001",
         added=0, deducted=0, balance=0, notes="", **kw):
    return db.add_transaction(member_id, date, event_type, loan_ref, added, deducted,
                              balance, notes, **kw)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def test_for_member_orders_by_date_then_id(journal):
    ind = _member(journal)
    _row(journal, ind, "2026-03-01", notes="third")
    _row(journal, ind, "2026-01-01", notes="first")
    _row(journal, ind, "2026-01-01", notes="second")     # same date, later id

    notes = list(journal.get_ledger(ind)["notes"])
    assert notes == ["first", "second", "third"]


def test_for_member_date_filters(journal):
    ind = _member(journal)
    for date in ("2026-01-15", "2026-02-15", "2026-03-15"):
        _row(journal, ind, date)
    assert len(journal.get_ledger(ind, "2026-02-01", "2026-02-28")) == 1
    assert len(journal.get_ledger(ind, start_date="2026-02-01")) == 2
    assert len(journal.get_ledger(ind, end_date="2026-02-28")) == 2


def test_for_member_isolates_members(journal):
    a, b = _member(journal, "A"), _member(journal, "B")
    _row(journal, a, "2026-01-01")
    assert len(journal.get_ledger(a)) == 1
    assert len(journal.get_ledger(b)) == 0


def test_event_probes(journal):
    ind = _member(journal)
    _row(journal, ind, "2026-01-01", "Loan Issued", added=1000, notes="Principal: 1000")
    _row(journal, ind, "2026-02-01", "Repayment", deducted=100)
    _row(journal, ind, "2026-03-01", "Repayment", deducted=100)
    _row(journal, ind, "2026-04-01", "Loan Top-Up", added=500)

    led = journal.ledger
    assert led.count_events(ind, "L-001", ("Repayment",)) == 2
    assert led.count_events(ind, "L-001", ("Loan Top-Up", "Loan Buyoff")) == 1
    assert led.count_events(ind, "L-001", ("Repayment",), "2026-03-01", "2026-03-31") == 1
    assert led.max_date(ind, "L-001", "Repayment") == "2026-03-01"
    assert led.min_date(ind, "L-001", "Repayment") == "2026-02-01"
    assert led.first_note(ind, "L-001", "Loan Issued") == "Principal: 1000"
    assert led.first_note(ind, "L-001", "Loan Buyoff") is None


# --------------------------------------------------------------------------- #
# Earliest-date coverage (finding D7)
# --------------------------------------------------------------------------- #

def test_earliest_date_spans_ledger_and_savings(journal):
    ind = _member(journal)
    _row(journal, ind, "2026-05-01")
    journal.add_savings_transaction(ind, "2026-02-01", "Deposit", 100)
    assert journal.get_earliest_record_date(ind) == "2026-02-01"
    assert journal.ledger.earliest_date_overall() == "2026-02-01"


def test_earliest_date_ignores_the_fund_tables(journal):
    """Documented blind spot: a member whose only history is Christmas or
    benevolent contributions has no earliest date, so dated reports start after
    their data. Pinned so Phase 2f's fix is a deliberate change."""
    ind = _member(journal)
    journal.fund_add_transaction("christmas_savings", ind, "2020-01-01", "Deposit", 100)
    journal.fund_add_transaction("benevolent_ledger", ind, "2020-01-01", "Contribution", 50)
    assert journal.get_earliest_record_date(ind) is None


def test_earliest_date_for_no_members(journal):
    assert journal.ledger.earliest_date_for_members([]) is None


def test_get_recent_quarters_reads_the_earliest_date(journal):
    """Regression guard: this dashboard-facing method had no coverage, which is
    how a broken reference to the removed cursor row survived the suite."""
    from src.reports import ReportGenerator
    ind = _member(journal)
    _row(journal, ind, "2024-03-01")
    journal.set_setting("fy_start_month", "January")

    quarters = ReportGenerator(journal).get_recent_quarters(datetime(2025, 6, 1))
    assert quarters and all(hasattr(q, "year") for q in quarters)
    assert quarters[0].year <= 2024          # reaches back to the earliest row
    assert quarters[0] < quarters[-1]


def test_bulk_insert_of_nothing_is_a_noop(journal):
    journal.bulk_insert_transactions([])


def test_update_core_writes_optional_columns_only_when_supplied(journal):
    """The *repository* leaves unsupplied columns alone. DatabaseManager sits
    above it and deliberately does not — see the next test."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", deducted=100,
                  principal_portion=60, interest_portion=40, interest_amount=7)

    journal.ledger.update_core(row_id, "2026-01-15", 0, 120, "edited")
    journal.conn.commit()
    row = journal.get_transaction(row_id)
    assert (row["date"], row["deducted"], row["notes"]) == ("2026-01-15", 120, "edited")
    assert (row["principal_portion"], row["interest_portion"]) == (60, 40)   # untouched
    assert row["interest_amount"] == 7                                       # untouched


def test_editing_an_amount_forgets_the_split_it_belonged_to(journal):
    """A split is derived, so an amount edit must not keep the one computed for
    the previous amount. Every recalculator used to replay that stale split,
    which is how a repayment edited from 5,750 to 9,000 kept reducing principal
    by 5,000."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", deducted=100,
                  principal_portion=60, interest_portion=40)

    journal.update_transaction(row_id, "2026-01-15", 0, 120, "edited")
    row = journal.get_transaction(row_id)
    assert row["deducted"] == 120
    assert (row["principal_portion"], row["interest_portion"]) == (0, 0)


def test_an_explicit_split_is_an_assertion_and_is_kept(journal):
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", deducted=100,
                  principal_portion=60, interest_portion=40)

    journal.update_transaction(row_id, "2026-01-15", 0, 120, "edited",
                               principal_portion=90, interest_portion=30)
    row = journal.get_transaction(row_id)
    assert (row["principal_portion"], row["interest_portion"]) == (90, 30)


def test_update_core_writes_the_portions_as_a_pair(journal):
    """Both or neither — a single portion is ignored, which is the existing
    contract and a trap worth pinning."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", principal_portion=60, interest_portion=40)

    journal.update_transaction(row_id, "2026-01-01", 0, 100, "n", principal_portion=99)
    assert journal.get_transaction(row_id)["principal_portion"] == 60

    journal.update_transaction(row_id, "2026-01-01", 0, 100, "n",
                               principal_portion=70, interest_portion=30)
    row = journal.get_transaction(row_id)
    assert (row["principal_portion"], row["interest_portion"]) == (70, 30)


def test_mark_edited_stores_the_amount_not_the_flag(journal):
    """The 'hysteresis fix': is_edited carries the user's intended figure so a
    recalculation cannot round it away. create_tables normalises it back to 1
    on the next open. Preserved verbatim."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", deducted=100)

    journal.update_transaction(row_id, "2026-01-01", 0, 5857, "n", mark_edited=True)
    assert journal.get_transaction(row_id)["is_edited"] == 5857

    # An amount of 1 or less collapses to the plain flag.
    journal.update_transaction(row_id, "2026-01-01", 0, 1, "n", mark_edited=True)
    assert journal.get_transaction(row_id)["is_edited"] == 1


def test_balance_setters(journal):
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01")

    journal.update_balance(row_id, 500)
    assert journal.get_transaction(row_id)["balance"] == 500

    journal.update_ledger_balances(row_id, 400, 300, 100, 450)
    row = journal.get_transaction(row_id)
    assert (row["balance"], row["principal_balance"], row["interest_balance"],
            row["gross_balance"]) == (400, 300, 100, 450)

    # A row moves debt one way, so `added` and `deducted` are exercised on
    # separate rows — migration 004 rejects a two-sided row outright.
    with journal.transaction():
        journal.ledger.set_portions(row_id, 70, 30)
        journal.ledger.set_deducted(row_id, 99)
        journal.ledger.set_date(row_id, "2026-06-06")
    row = journal.get_transaction(row_id)
    assert (row["principal_portion"], row["interest_portion"]) == (70, 30)
    assert row["deducted"] == 99 and row["date"] == "2026-06-06"

    accrual = _row(journal, ind, "2026-02-01", "Interest Earned", added=5)
    with journal.transaction():
        journal.ledger.set_accrual(accrual, 12, 12)
    row = journal.get_transaction(accrual)
    assert (row["added"], row["interest_amount"]) == (12, 12)


def test_delete_and_restore_round_trip(journal):
    """Undo re-inserts the row with its original id so GL source_refs still
    resolve."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", "Repayment", deducted=100,
                  balance=900, notes="orig", principal_portion=60, interest_portion=40)
    before = journal.get_transaction(row_id)

    journal.delete_transaction(row_id)
    assert journal.get_transaction(row_id) is None

    with journal.transaction():
        journal.ledger.restore((
            before["id"], before["individual_id"], before["date"], before["event_type"],
            before["added"], before["deducted"], before["balance"], before["loan_id"],
            before["notes"], before["interest_amount"], before["principal_balance"],
            before["interest_balance"], before["principal_portion"],
            before["interest_portion"], before["is_edited"]))

    after = journal.get_transaction(row_id)
    assert after["id"] == row_id and after["notes"] == "orig"
    assert (after["principal_portion"], after["interest_portion"]) == (60, 40)


def test_delete_events_removes_only_the_kinds_named(journal):
    ind = _member(journal)
    _row(journal, ind, "2026-01-01", "Loan Issued", added=1000)
    _row(journal, ind, "2026-02-01", "Repayment", deducted=100)
    _row(journal, ind, "2026-02-01", "Interest Earned", added=12)

    with journal.transaction():
        journal.ledger.delete_events(ind, "L-001", ("Repayment", "Interest Earned"))
    assert [r for r in journal.get_ledger(ind)["event_type"]] == ["Loan Issued"]


# --------------------------------------------------------------------------- #
# The interest edit-lock — three scopes, one statement (finding D4)
# --------------------------------------------------------------------------- #

def _three_accruals(db, member_id):
    """Three locked accruals on distinct dates, for the edit-lock scope tests.

    The lock is set directly rather than through update_core, which would also
    rewrite the date and flatten the three rows onto one day.
    """
    ids = []
    for date in ("2026-01-01", "2026-02-01", "2026-03-01"):
        ids.append(_row(db, member_id, date, "Interest Earned", added=12))
    db.conn.executemany("UPDATE ledger SET is_edited=1 WHERE id=?", [(i,) for i in ids])
    db.conn.commit()
    return ids


def test_unlock_clears_only_rows_after_the_date(journal):
    ind = _member(journal)
    ids = _three_accruals(journal, ind)
    journal.unlock_future_interest(ind, "L-001", "2026-01-31")

    locks = [journal.get_transaction(i)["is_edited"] for i in ids]
    assert locks == [1, 0, 0]          # January kept, February onward cleared


def test_unbounded_clear_discards_every_lock(journal):
    """What the heal tool does — safe only on loans with no legitimate interest
    edits, which is why it checks that first."""
    ind = _member(journal)
    ids = _three_accruals(journal, ind)
    with journal.transaction():
        journal.ledger.set_interest_lock(ind, "L-001", locked=False)
    assert [journal.get_transaction(i)["is_edited"] for i in ids] == [0, 0, 0]


def test_set_accrual_from_date_is_inclusive(journal):
    """A rate change moves concurrent and future accruals and locks them; the
    same-day row must be caught, hence >= rather than >."""
    ind = _member(journal)
    ids = _three_accruals(journal, ind)
    with journal.transaction():
        journal.ledger.set_interest_lock(ind, "L-001", locked=False)
        journal.ledger.set_accrual_from_date(ind, "L-001", "2026-02-01", 99)

    rows = [journal.get_transaction(i) for i in ids]
    assert [r["added"] for r in rows] == [12, 99, 99]
    assert [r["is_edited"] for r in rows] == [0, 1, 1]


def test_interest_lock_is_scoped_to_one_member(journal):
    a, b = _member(journal, "A"), _member(journal, "B")
    ids_a = _three_accruals(journal, a)
    ids_b = _three_accruals(journal, b)

    with journal.transaction():
        journal.ledger.set_interest_lock(a, "L-001", locked=False)

    assert [journal.get_transaction(i)["is_edited"] for i in ids_a] == [0, 0, 0]
    assert [journal.get_transaction(i)["is_edited"] for i in ids_b] == [1, 1, 1]


def test_count_edited_treats_any_nonzero_as_locked(journal):
    """Legacy rows store an amount rather than 1, so the heal probe must test
    for nonzero, not equality with 1."""
    ind = _member(journal)
    row_id = _row(journal, ind, "2026-01-01", "Repayment", deducted=100)
    journal.update_transaction(row_id, "2026-01-01", 0, 5857, "n", mark_edited=True)

    assert journal.ledger.count_edited(ind, "L-001", "Repayment") == 1
    assert journal.ledger.count_edited(ind, "L-001", "Repayment", flag=1) == 0


# --------------------------------------------------------------------------- #
# Phase 3b — one position accessor
# --------------------------------------------------------------------------- #

def test_position_from_frame_reads_the_last_row(journal):
    ind = _member(journal)
    _row(journal, ind, "2026-01-01", "Loan Issued", added=1000, balance=1000,
         principal_balance=1000, interest_balance=0)
    _row(journal, ind, "2026-02-01", "Interest Earned", added=13, balance=1013,
         principal_balance=1000, interest_balance=13)

    from src.db import position_from_frame
    pos = position_from_frame(journal.get_ledger(ind))
    assert (pos.balance, pos.principal_balance, pos.interest_balance) == (1013, 1000, 13)


def test_position_of_an_empty_frame_is_all_zero(journal):
    from src.db import position_from_frame
    ind = _member(journal)
    pos = position_from_frame(journal.get_ledger(ind))
    assert (pos.balance, pos.principal_balance, pos.interest_balance) == (0.0, 0.0, 0.0)
    assert position_from_frame(None).balance == 0.0


def test_position_tolerates_a_frame_without_the_split_columns():
    """A frame read from an older journal may not carry the split columns; the
    guard the six call sites each carried is kept in one place."""
    import pandas as pd

    from src.db import position_from_frame
    df = pd.DataFrame([{"balance": 500.0}])
    pos = position_from_frame(df)
    assert (pos.balance, pos.principal_balance, pos.interest_balance) == (500.0, 0.0, 0.0)


def test_current_position_matches_the_frame_reader(journal):
    from src.db import position_from_frame
    ind = _member(journal)
    _row(journal, ind, "2026-01-01", "Loan Issued", added=1000, balance=1000,
         principal_balance=1000)
    _row(journal, ind, "2026-03-01", "Repayment", deducted=100, balance=900,
         principal_balance=900)
    # A back-dated row must not become "current" just because it was inserted last.
    _row(journal, ind, "2026-02-01", "Interest Earned", added=13, balance=1013,
         principal_balance=1000, interest_balance=13)

    from_db = journal.ledger.current_position(ind)
    from_frame = position_from_frame(journal.get_ledger(ind))
    assert from_db == from_frame
    assert from_db.balance == 900          # the 2026-03-01 row, not the one inserted last
