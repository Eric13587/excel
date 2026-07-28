"""Phase 4a — normalisation of rows copied from a foreign journal.

Two policies, and the tests are organised around them: repair where the intent
is unambiguous, reject where it is not. A rejected row must always carry a
reason the user can act on.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.db import import_rules as rules


# --------------------------------------------------------------------------- #
# Dates
# --------------------------------------------------------------------------- #

def test_unambiguous_date_formats_are_accepted():
    for raw in ("2026-01-15", "2026/01/15", "20260115", "2026-01-15 09:30:00"):
        assert rules.normalise_date(raw) == "2026-01-15"


def test_ambiguous_dates_are_refused_not_guessed():
    """01/02/2026 is 1 February or 2 January depending on the writer. Guessing
    puts a transaction in the wrong month, so it is reported instead."""
    for raw in ("01/02/2026", "15-01-2026", "Jan 15 2026", "", None, "not a date"):
        assert rules.normalise_date(raw) is None


def test_impossible_dates_are_refused():
    assert rules.normalise_date("2026-13-01") is None
    assert rules.normalise_date("2026-01-32") is None


# --------------------------------------------------------------------------- #
# Members
# --------------------------------------------------------------------------- #

def test_member_name_is_trimmed_and_reported():
    out = rules.normalise_member({"name": "  Jane Doe  "})
    assert out.values["name"] == "Jane Doe"
    assert out.warnings and not out.rejected


def test_member_without_a_name_is_rejected():
    for name in ("", "   ", None):
        out = rules.normalise_member({"name": name})
        assert out.rejected and "no name" in out.reason


def test_member_defaults_and_retirement_pairing():
    out = rules.normalise_member({"name": "A", "employment_status": None,
                                  "default_deduction": None})
    assert out.values["employment_status"] == "Active"
    assert out.values["default_deduction"] == 0.0

    # The strict schema pairs the flag with the date; a flag with no date is
    # reconciled here rather than rejected by the constraint later.
    out = rules.normalise_member({"name": "A", "is_retired": 1, "retired_date": None})
    assert out.values["is_retired"] == 0 and out.values["retired_date"] is None
    assert any("retired flag" in w for w in out.warnings)

    out = rules.normalise_member({"name": "A", "is_retired": 1, "retired_date": "2020-06-01"})
    assert (out.values["is_retired"], out.values["retired_date"]) == (1, "2020-06-01")


# --------------------------------------------------------------------------- #
# Loans
# --------------------------------------------------------------------------- #

def _loan(**over):
    row = {"ref": "L-001", "principal": 1000, "total_amount": 1150, "balance": 1000,
           "installment": 100, "start_date": "2026-01-01", "next_due_date": "2026-02-01",
           "status": "Active", "monthly_interest": 13, "unearned_interest": 150,
           "interest_balance": 0}
    row.update(over)
    return row


def test_loan_status_is_title_cased_and_unknown_kept_outstanding():
    assert rules.normalise_loan(_loan(status="active")).values["status"] == "Active"
    assert rules.normalise_loan(_loan(status="PAID")).values["status"] == "Paid"

    out = rules.normalise_loan(_loan(status="written off"))
    assert out.values["status"] == "Active"      # conservative: debt stays outstanding
    assert any("Active" in w for w in out.warnings)


def test_loan_start_date_falls_back_to_the_ledger():
    out = rules.normalise_loan(_loan(start_date=None), fallback_start="2025-12-01")
    assert out.values["start_date"] == "2025-12-01"
    assert not out.rejected


def test_loan_without_any_usable_start_date_is_rejected():
    out = rules.normalise_loan(_loan(start_date=None))
    assert out.rejected and "start date" in out.reason
    out = rules.normalise_loan(_loan(start_date="01/02/2026"))
    assert out.rejected


def test_loan_without_a_ref_is_rejected():
    assert rules.normalise_loan(_loan(ref="")).rejected


def test_loan_with_a_non_numeric_amount_is_rejected():
    out = rules.normalise_loan(_loan(principal="lots"))
    assert out.rejected and "principal" in out.reason


def test_unreadable_next_due_becomes_blank_not_a_rejection():
    """A loan with no next due date is normal — a bought-off loan has none."""
    out = rules.normalise_loan(_loan(next_due_date="whenever"))
    assert not out.rejected and out.values["next_due_date"] == ""


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def _entry(**over):
    row = {"date": "2026-01-01", "event_type": "Repayment", "loan_id": "L-001",
           "added": 0, "deducted": 100, "balance": 900, "notes": ""}
    row.update(over)
    return row


def test_known_event_synonyms_are_mapped():
    for raw, expected in (("Loan Disbursed", "Loan Issued"),
                          ("disbursement", "Loan Issued"),
                          ("Top-Up", "Loan Top-Up"),
                          ("payment", "Repayment"),
                          ("settlement", "Loan Buyoff")):
        out = rules.normalise_ledger(_entry(event_type=raw))
        assert out.values["event_type"] == expected
        assert out.warnings


def test_unknown_event_type_is_rejected():
    """A row the recalculator has no branch for cannot be replayed into a
    balance, so importing it would silently skew the member's position."""
    out = rules.normalise_ledger(_entry(event_type="Mystery Adjustment"))
    assert out.rejected and "unknown ledger event type" in out.reason


def test_two_sided_row_is_rejected():
    out = rules.normalise_ledger(_entry(added=50, deducted=100))
    assert out.rejected and "both adds" in out.reason


def test_negative_amounts_are_rejected():
    assert rules.normalise_ledger(_entry(deducted=-5)).rejected
    assert rules.normalise_ledger(_entry(added=-5, deducted=0)).rejected


def test_unreadable_ledger_date_is_rejected():
    out = rules.normalise_ledger(_entry(date="01/02/2026"))
    assert out.rejected and "unreadable date" in out.reason


def test_missing_loan_reference_is_rejected():
    assert rules.normalise_ledger(_entry(loan_id="")).rejected


def test_edit_lock_collapses_to_a_flag():
    """Legacy rows stored the payment amount in is_edited."""
    out = rules.normalise_ledger(_entry(is_edited=5857))
    assert out.values["is_edited"] == 1 and out.warnings
    assert rules.normalise_ledger(_entry(is_edited=0)).values["is_edited"] == 0
    assert rules.normalise_ledger(_entry(is_edited=1)).values["is_edited"] == 1


# --------------------------------------------------------------------------- #
# Savings and funds
# --------------------------------------------------------------------------- #

def test_savings_synonyms_and_rejections():
    out = rules.normalise_fund_row({"date": "2026-01-01", "transaction_type": "Contribution",
                                    "amount": 500})
    assert out.values["transaction_type"] == "Deposit" and out.warnings

    assert rules.normalise_fund_row({"date": "2026-01-01", "transaction_type": "Levy",
                                     "amount": 10}).rejected
    assert rules.normalise_fund_row({"date": "2026-01-01", "transaction_type": "Deposit",
                                     "amount": -50}).rejected
    assert rules.normalise_fund_row({"date": "nope", "transaction_type": "Deposit",
                                     "amount": 10}).rejected


def test_benevolent_accepts_contribution_natively():
    out = rules.normalise_benevolent_row({"date": "2026-01-01",
                                          "transaction_type": "Contribution", "amount": 50})
    assert not out.rejected and out.values["transaction_type"] == "Contribution"
    assert not out.warnings


# --------------------------------------------------------------------------- #
# End to end: a legacy journal cannot poison the destination
# --------------------------------------------------------------------------- #

@pytest.fixture
def legacy_source():
    """A source with no constraints, carrying every class of bad value."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    s = sqlite3.connect(path)
    s.executescript("""
    CREATE TABLE individuals (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT,
        default_deduction REAL, employment_status TEXT, pf_no TEXT, id_no TEXT,
        is_retired INTEGER, retired_date TEXT, created_at TEXT);
    CREATE TABLE loans (id INTEGER PRIMARY KEY, individual_id INT, ref TEXT, principal REAL,
        total_amount REAL, balance REAL, installment REAL, start_date TEXT, next_due_date TEXT,
        status TEXT, monthly_interest REAL, unearned_interest REAL, interest_balance REAL);
    CREATE TABLE ledger (id INTEGER PRIMARY KEY, individual_id INT, date TEXT, event_type TEXT,
        loan_id TEXT, added REAL, deducted REAL, balance REAL, notes TEXT,
        installment_amount REAL, batch_id TEXT, interest_amount REAL, principal_balance REAL,
        interest_balance REAL, principal_portion REAL, interest_portion REAL,
        previous_state TEXT, is_edited INTEGER);
    CREATE TABLE savings (id INTEGER PRIMARY KEY, individual_id INT, date TEXT,
        transaction_type TEXT, amount REAL, balance REAL, notes TEXT);
    INSERT INTO individuals VALUES (1,'  Legacy Member  ','07','l@x',0,'Employed','PF1','ID1',0,NULL,'2020-01-01');
    INSERT INTO individuals VALUES (2,'','none','',0,NULL,'','',0,NULL,NULL);
    INSERT INTO loans VALUES (1,1,'L-001',1000,1150,1000,100,NULL,'2026-02-01','active',13,150,0);
    INSERT INTO ledger VALUES (1,1,'2026-01-01','Loan Disbursed','L-001',1000,0,1000,'x',0,NULL,0,1000,0,0,0,NULL,0);
    INSERT INTO ledger VALUES (2,1,'01/02/2026','Repayment','L-001',0,100,900,'bad date',0,NULL,0,900,0,100,0,NULL,5857);
    INSERT INTO ledger VALUES (3,1,'2026-03-01','Repayment','L-001',50,100,850,'two-sided',0,NULL,0,850,0,100,0,NULL,0);
    INSERT INTO savings VALUES (1,1,'2026-01-01','Contribution',500,500,'odd type');
    INSERT INTO savings VALUES (2,1,'2026-02-01','Deposit',-50,450,'negative');
    """)
    s.commit(); s.close()
    return path


def test_legacy_import_leaves_nothing_the_strict_schema_would_reject(legacy_source):
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "dst.db"))
    try:
        ids = [p["id"] for p in db.get_import_preview(legacy_source)]
        result = db.import_selected_data(legacy_source, ids,
                                         {"import_loans": True, "import_savings": True})

        assert result["stats"]["rejected"] > 0
        assert result["errors"], "rejections must be reported, never silent"

        c = db.conn
        violations = sum(c.execute(sql).fetchone()[0] for sql in (
            "SELECT COUNT(*) FROM individuals WHERE name IS NULL OR TRIM(name)=''",
            "SELECT COUNT(*) FROM loans WHERE start_date IS NULL OR start_date=''",
            "SELECT COUNT(*) FROM loans WHERE status NOT IN ('Active','Paid')",
            "SELECT COUNT(*) FROM ledger WHERE event_type NOT IN "
            "('Loan Issued','Loan Top-Up','Interest Earned','Repayment','Loan Buyoff','Loan Restructure')",
            "SELECT COUNT(*) FROM ledger WHERE date NOT GLOB "
            "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'",
            "SELECT COUNT(*) FROM ledger WHERE added<>0 AND deducted<>0",
            "SELECT COUNT(*) FROM ledger WHERE is_edited NOT IN (0,1)",
            "SELECT COUNT(*) FROM savings WHERE transaction_type NOT IN "
            "('Deposit','Withdrawal','Interest')",
            "SELECT COUNT(*) FROM savings WHERE amount < 0",
        ))
        assert violations == 0, "the strict schema would still reject imported rows"
    finally:
        db.close()


def test_repairable_rows_still_arrive(legacy_source):
    """Rejection must not be over-eager: the good and the fixable still land."""
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "dst2.db"))
    try:
        ids = [p["id"] for p in db.get_import_preview(legacy_source)]
        db.import_selected_data(legacy_source, ids, {"import_loans": True, "import_savings": True})
        c = db.conn
        assert c.execute("SELECT name FROM individuals").fetchall() == [("Legacy Member",)]
        # The loan's missing start date is recovered from its own ledger.
        assert c.execute("SELECT ref, start_date, status FROM loans").fetchall() == \
            [("L-001", "2026-01-01", "Active")]
        assert c.execute("SELECT event_type FROM ledger").fetchall() == [("Loan Issued",)]
        assert c.execute("SELECT transaction_type, amount FROM savings").fetchall() == \
            [("Deposit", 500.0)]
    finally:
        db.close()
