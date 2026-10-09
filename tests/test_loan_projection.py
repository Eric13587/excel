"""Writing a loan's derived values back into its rows.

The four recalculators each answer part of "what may I regenerate?", and they
disagree. The derivation does not — provided it is not handed a stale split to
replay, which is what an edit leaves behind.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.engine import LoanEngine
from src.services.loan_projection import clear_derived, project_loan, project_member


@pytest.fixture
def loan():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "p.db"), auto_backup=False)
    engine = LoanEngine(db)
    ind = db.add_individual("M", "07", "m@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 5750, 750,
                       "2025-01-01", "2025-02-01", unearned_interest=15000)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-001", 100000, 0, 100000, "issue")
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-04-01")
    yield db, engine, ind
    db.close()


def _rows(db, ind):
    return db.conn.execute(
        "SELECT date, event_type, deducted, principal_portion, interest_portion, balance "
        "FROM ledger WHERE individual_id=? ORDER BY date, id", (ind,)).fetchall()


def test_projecting_a_healthy_loan_changes_nothing(loan):
    """The safety property: it must be idempotent on data that is already right."""
    db, _engine, ind = loan
    before = _rows(db, ind)
    assert project_loan(db, ind, "L-001") == 0
    assert _rows(db, ind) == before


def test_projecting_is_idempotent(loan):
    db, _engine, ind = loan
    project_loan(db, ind, "L-001")
    first = _rows(db, ind)
    assert project_loan(db, ind, "L-001") == 0
    assert _rows(db, ind) == first


def test_an_edited_amount_gets_the_right_split_once_the_stale_one_is_cleared(loan):
    """The headline case. A repayment edited from 5,750 to 9,000 kept reducing
    principal by 5,000, because every recalculator replayed the split that
    belonged to the old amount."""
    db, _engine, ind = loan
    row_id = db.conn.execute(
        "SELECT id FROM ledger WHERE event_type='Repayment' ORDER BY date LIMIT 1"
    ).fetchone()[0]
    db.update_transaction(row_id, "2025-02-01", 0, 9000, "edited", mark_edited=True)

    clear_derived(db, row_id)
    project_loan(db, ind, "L-001")

    edited = [r for r in _rows(db, ind) if r[0] == "2025-02-01" and r[1] == "Repayment"][0]
    assert edited[2] == 9000
    assert edited[3] == 8250, "principal takes what interest does not"
    assert edited[4] == 750
    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 81750


def test_a_back_dated_repayment_reprices_the_rows_below(loan):
    db, _engine, ind = loan
    db.add_transaction(ind, "2025-02-15", "Repayment", "L-001", 0, 20000, 0,
                       "lump", principal_portion=0, interest_portion=0)
    project_loan(db, ind, "L-001")

    assert db.get_loan_by_ref(ind, "L-001")["balance"] == 65000
    balances = [r[5] for r in _rows(db, ind) if r[1] == "Repayment"]
    assert balances == sorted(balances, reverse=True), "each row leaves less outstanding"


def test_the_running_balance_is_always_principal_plus_interest(loan):
    """The invariant the stored ledger holds on all 2,174 production rows."""
    db, _engine, ind = loan
    project_loan(db, ind, "L-001")
    # Only the rows the projector owns; the Loan Issued row is an assertion and
    # is left exactly as it was written.
    for balance, p, i in db.conn.execute(
            "SELECT balance, principal_balance, interest_balance FROM ledger "
            "WHERE individual_id=? AND event_type IN ('Interest Earned','Repayment')",
            (ind,)):
        assert abs((balance or 0) - ((p or 0) + (i or 0))) < 0.51


def test_a_row_count_mismatch_refuses_rather_than_writing_half_an_answer(loan):
    """If the reconstruction lost a row, leaving the loan alone is safer than
    projecting onto the wrong rows."""
    db, _engine, ind = loan
    db.conn.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, "
                    "added, deducted, balance) VALUES (?, '2025-03-05', "
                    "'Interest Earned', 'L-001', 999, 0, 0)", (ind,))
    db.conn.commit()
    before = _rows(db, ind)
    assert project_loan(db, ind, "L-001") == 0
    assert _rows(db, ind) == before


def test_projecting_a_member_covers_every_loan(loan):
    db, engine, ind = loan
    db.add_loan_record(ind, "L-002", 50000, 57500, 50000, 2875, 375,
                       "2025-01-01", "2025-02-01", unearned_interest=7500)
    db.add_transaction(ind, "2025-01-01", "Loan Issued", "L-002", 50000, 0, 50000, "issue")
    engine.loan_service.catch_up_loan(ind, "L-002", target_date="2025-03-01")
    assert project_member(db, ind) == 0          # both already correct


def test_a_loan_with_no_events_is_skipped(loan):
    db, _engine, ind = loan
    assert project_loan(db, ind, "L-999") == 0


def test_the_projector_never_touches_unearned_interest(loan):
    """Regression, and an expensive one.

    unearned_interest is what the loan was written to charge and has not charged
    yet — a term, not a derivation. reconstruct() seeds the replay with the
    interest already accrued, so the derived value lands at zero for a
    fully-replayed loan.

    Writing it zeroed the column on 96 production loans. catch_up_loan accrues
    min(monthly_interest, unearned), so zero unearned means *no interest row is
    ever created again* — every future deduction would be pure principal. It
    surfaced as "the Interest Earned row has disappeared", which is what a user
    noticed before any test did.
    """
    db, _engine, ind = loan
    before = db.get_loan_by_ref(ind, "L-001")["unearned_interest"]
    assert before > 0

    project_loan(db, ind, "L-001")

    assert db.get_loan_by_ref(ind, "L-001")["unearned_interest"] == before


def test_catch_up_still_accrues_after_a_projection(loan):
    """The symptom, stated directly: projecting a loan must not stop it
    charging interest."""
    db, engine, ind = loan
    project_loan(db, ind, "L-001")

    before = db.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND event_type='Interest Earned'",
        (ind,)).fetchone()[0]
    engine.loan_service.catch_up_loan(ind, "L-001", target_date="2025-07-01")
    after = db.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND event_type='Interest Earned'",
        (ind,)).fetchone()[0]
    assert after > before, "catch-up stopped creating interest rows"
