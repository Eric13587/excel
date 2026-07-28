"""Phase 2c-i — LoanRepository.

Concentrates on the contracts that are easy to break silently: member scoping
on a ref, the optional-column semantics of the two dynamic UPDATEs, and the
suspension-span lifecycle.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "l.db"))
    yield db
    db.close()


def _loan(db, member_id, ref="L-001", principal=1000, total=1150, balance=1000,
          installment=100, monthly_interest=13, start="2026-01-01", due="2026-02-01",
          unearned=150):
    db.add_loan_record(member_id, ref, principal, total, balance, installment,
                       monthly_interest, start, due, unearned)
    return db.get_loan_by_ref(member_id, ref)


# --------------------------------------------------------------------------- #
# Member scoping — refs repeat across members
# --------------------------------------------------------------------------- #

def test_same_ref_belongs_to_different_members(journal):
    a = journal.add_individual("A", "1", "a@x.com")
    b = journal.add_individual("B", "2", "b@x.com")
    _loan(journal, a, principal=1000)
    _loan(journal, b, principal=9000)

    assert journal.get_loan_by_ref(a, "L-001")["principal"] == 1000
    assert journal.get_loan_by_ref(b, "L-001")["principal"] == 9000


def test_by_ref_returns_none_when_absent(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    assert journal.get_loan_by_ref(ind, "L-999") is None


def test_set_schedule_by_ref_touches_only_that_member(journal):
    """Regression for the bug behind the scoping rule: an earlier version keyed
    on ref alone and updated other members' loans."""
    a = journal.add_individual("A", "1", "a@x.com")
    b = journal.add_individual("B", "2", "b@x.com")
    _loan(journal, a)
    _loan(journal, b)

    with journal.transaction():
        journal.loans.set_schedule_by_ref(a, "L-001", installment=555, monthly_interest=66)

    assert journal.get_loan_by_ref(a, "L-001")["installment"] == 555
    assert journal.get_loan_by_ref(b, "L-001")["installment"] == 100


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #

def test_active_and_all_loan_listings(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    first = _loan(journal, ind, "L-001")
    _loan(journal, ind, "L-002")
    journal.update_loan_status(first["id"], 0, "", "Paid")

    assert len(journal.get_loans(ind)) == 2
    assert [loan["ref"] for loan in journal.get_active_loans(ind)] == ["L-002"]
    assert [loan["ref"] for loan in journal.get_all_active_loans()] == ["L-002"]
    assert journal.has_outstanding_loans(ind)


def test_active_refs_pairs_member_with_ref(journal):
    a = journal.add_individual("A", "1", "a@x.com")
    b = journal.add_individual("B", "2", "b@x.com")
    _loan(journal, a)
    _loan(journal, b)
    assert sorted(journal.loans.active_refs()) == sorted([(a, "L-001"), (b, "L-001")])


def test_overdue_count_uses_the_supplied_date(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    _loan(journal, ind, "L-001", due="2026-02-01")
    assert journal.loans.overdue_count("2026-01-01") == 0
    assert journal.loans.overdue_count("2026-03-01") == 1


def test_terms_probe(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    assert journal.loans.terms(ind, "L-001") == (loan["id"], 1150, 100, 13)
    assert journal.loans.terms(ind, "L-999") is None


def test_identity_lookup(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    assert journal.loans.identity(loan["id"]) == ("L-001", ind)
    assert journal.loans.identity(99999) == (None, None)


# --------------------------------------------------------------------------- #
# The optional-column contract on the two dynamic UPDATEs
# --------------------------------------------------------------------------- #

def test_set_status_leaves_unsupplied_columns_alone(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    with journal.transaction():
        journal.loans.set_status(loan["id"], 500, "2026-03-01", "Active",
                                 interest_balance=42)

    after = journal.get_loan_by_ref(ind, "L-001")
    assert (after["balance"], after["next_due_date"], after["status"]) == (500, "2026-03-01", "Active")
    assert after["interest_balance"] == 42
    assert after["unearned_interest"] == 150       # untouched: not supplied


def test_set_details_leaves_unsupplied_columns_alone(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    with journal.transaction():
        journal.loans.set_details(loan["id"], 2000, 1800, 200, 25, "2026-04-01",
                                  principal=1750)

    after = journal.get_loan_by_ref(ind, "L-001")
    assert (after["total_amount"], after["balance"], after["installment"]) == (2000, 1800, 200)
    assert after["principal"] == 1750
    assert after["unearned_interest"] == 150       # untouched
    assert after["interest_balance"] == 0          # untouched


def test_zero_is_written_not_treated_as_absent(journal):
    """0 is a legitimate value; only None means 'leave it alone'."""
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    with journal.transaction():
        journal.loans.set_status(loan["id"], 0, "", "Paid", interest_balance=0,
                                 unearned_interest=0)
    after = journal.get_loan_by_ref(ind, "L-001")
    assert after["interest_balance"] == 0 and after["unearned_interest"] == 0


def test_restore_writes_every_snapshot_field(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    with journal.transaction():
        journal.loans.restore(loan["id"], principal=1, total_amount=2, balance=3,
                              installment=4, interest_balance=5, unearned_interest=6,
                              monthly_interest=7, next_due_date="2026-09-09", status="Paid")
    after = journal.get_loan_by_ref(ind, "L-001")
    assert (after["principal"], after["total_amount"], after["balance"], after["installment"],
            after["interest_balance"], after["unearned_interest"], after["monthly_interest"],
            after["next_due_date"], after["status"]) == (1, 2, 3, 4, 5, 6, 7, "2026-09-09", "Paid")


# --------------------------------------------------------------------------- #
# Suspension spans
# --------------------------------------------------------------------------- #

def test_suspend_records_a_span_and_flags_the_loan(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    journal.suspend_loan(loan["id"], until_date="2026-06-01", start_date="2026-03-01")

    after = journal.get_loan_by_ref(ind, "L-001")
    assert after["is_suspended"] == 1 and after["suspend_until"] == "2026-06-01"

    spans = journal.get_loan_suspensions(ind)
    assert len(spans) == 1
    assert (spans[0]["start_date"], spans[0]["suspend_until"], spans[0]["status"]) == \
           ("2026-03-01", "2026-06-01", "active")
    assert spans[0]["resumed_date"] is None


def test_extending_a_suspension_updates_the_open_span_in_place(journal):
    """No duplicate span, and the original start date is preserved so the record
    still covers the whole skipped period."""
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    journal.suspend_loan(loan["id"], until_date="2026-06-01", start_date="2026-03-01")
    journal.suspend_loan(loan["id"], until_date="2026-09-01", start_date="2026-08-01")

    spans = journal.get_loan_suspensions(ind)
    assert len(spans) == 1
    assert spans[0]["start_date"] == "2026-03-01"     # original start kept
    assert spans[0]["suspend_until"] == "2026-09-01"  # only the resume date moved


def test_resume_closes_the_span(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    journal.suspend_loan(loan["id"], until_date="2026-06-01", start_date="2026-03-01")
    journal.resume_loan(loan["id"], resumed_date="2026-05-01")

    after = journal.get_loan_by_ref(ind, "L-001")
    assert after["is_suspended"] == 0 and after["suspend_until"] is None
    span = journal.get_loan_suspensions(ind)[0]
    assert span["status"] == "resumed" and span["resumed_date"] == "2026-05-01"


def test_record_suspension_historical_leaves_the_loan_unsuspended(journal):
    """A span that already ended is history, not a live suspension."""
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    kind = journal.record_suspension(loan["id"], "2026-01-01", "2026-02-01",
                                     today="2026-07-01")
    assert kind == "historical"
    assert journal.get_loan_by_ref(ind, "L-001")["is_suspended"] == 0
    span = journal.get_loan_suspensions(ind)[0]
    assert span["status"] == "resumed" and span["resumed_date"] == "2026-02-01"


def test_record_suspension_future_suspends_live(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    loan = _loan(journal, ind)
    kind = journal.record_suspension(loan["id"], "2026-08-01", "2026-12-01",
                                     today="2026-07-01")
    assert kind == "active"
    assert journal.get_loan_by_ref(ind, "L-001")["is_suspended"] == 1
    assert journal.get_loan_suspensions(ind)[0]["start_date"] == "2026-08-01"


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #

def test_delete_removes_the_loan_and_its_ledger_only(journal):
    ind = journal.add_individual("A", "1", "a@x.com")
    _loan(journal, ind, "L-001")
    _loan(journal, ind, "L-002")
    cur = journal.conn.cursor()
    for ref in ("L-001", "L-002"):
        cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, balance) "
                    "VALUES (?, '2026-01-01', 'Loan Issued', ?, 1000, 1000)", (ind, ref))
    journal.conn.commit()

    journal.delete_loan(ind, "L-001")

    assert journal.get_loan_by_ref(ind, "L-001") is None
    assert journal.get_loan_by_ref(ind, "L-002") is not None
    remaining = [r[0] for r in journal.conn.execute(
        "SELECT loan_id FROM ledger WHERE individual_id=?", (ind,))]
    assert remaining == ["L-002"]
