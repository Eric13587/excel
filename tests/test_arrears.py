"""The people behind the "Loans in Arrears" number.

The dashboard has always shown a count and nothing else. These tests pin the
list you now get by clicking it: that it is the same population as the count,
that the derived figures are the ones a SACCO would recognise, and that what is
printed is what was on screen.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.reports import ReportGenerator
from src.services import arrears


AS_OF = "2026-06-15"


@pytest.fixture
def db():
    d = DatabaseManager(os.path.join(tempfile.mkdtemp(), "a.db"), auto_backup=False)
    yield d
    d.close()


def _member(db, name, **kw):
    ind = db.add_individual(name, kw.get("phone", "0700"), f"{name}@x")
    if kw.get("pf_no"):
        db.members.set_pf_no(ind, kw["pf_no"])
        db.conn.commit()
    return ind


def _loan(db, ind, ref, due, balance=100000, installment=5750):
    db.add_loan_record(ind, ref, balance, balance, balance, installment, 750,
                       "2025-01-01", due)
    return db.get_loan_by_ref(ind, ref)["id"]


# --------------------------------------------------------------------------- #
# Which loans appear
# --------------------------------------------------------------------------- #

def test_only_loans_past_their_due_date_appear(db):
    ind = _member(db, "Late")
    _loan(db, ind, "L-001", "2026-01-01")          # overdue
    _loan(db, ind, "L-002", "2026-12-01")          # not yet due

    rows = arrears.get_arrears(db, AS_OF)
    assert [r["ref"] for r in rows] == ["L-001"]


def test_a_paid_loan_never_appears(db):
    ind = _member(db, "Done")
    loan_id = _loan(db, ind, "L-001", "2026-01-01")
    db.update_loan_status(loan_id, 0, "2026-01-01", "Paid")

    assert arrears.get_arrears(db, AS_OF) == []


def test_the_list_matches_the_number_on_the_tile(db):
    """Same population, deliberately — the two can never disagree."""
    a = _member(db, "Ann")
    b = _member(db, "Ben")
    _loan(db, a, "L-001", "2026-01-01")
    _loan(db, a, "L-002", "2026-03-01")
    _loan(db, b, "L-001", "2026-05-01")
    _loan(db, b, "L-002", "2027-01-01")

    as_of_rows = db.loans.overdue(AS_OF)
    assert len(as_of_rows) == db.loans.overdue_count(AS_OF) == 3


def test_a_suspended_loan_is_flagged_not_dropped(db):
    ind = _member(db, "Held")
    loan_id = _loan(db, ind, "L-001", "2026-01-01")
    db.record_suspension(loan_id, "2026-01-01", None, today=AS_OF)

    rows = arrears.get_arrears(db, AS_OF)
    assert len(rows) == 1
    assert rows[0]["suspended"] is True
    assert ReportGenerator._arrears_status(rows[0]) == "Suspended (indefinite)"


def test_a_fixed_suspension_says_when_it_ends(db):
    ind = _member(db, "Held")
    loan_id = _loan(db, ind, "L-001", "2026-01-01")
    db.record_suspension(loan_id, "2026-01-01", "2027-01-01", today=AS_OF)

    row = arrears.get_arrears(db, AS_OF)[0]
    assert ReportGenerator._arrears_status(row) == "Suspended until 2027-01-01"


# --------------------------------------------------------------------------- #
# What the rows say
# --------------------------------------------------------------------------- #

def test_days_and_missed_instalments_count_the_overdue_month(db):
    ind = _member(db, "Ann", pf_no="PF9")
    _loan(db, ind, "L-001", "2026-03-15")          # exactly 3 months before AS_OF

    row = arrears.get_arrears(db, AS_OF)[0]
    assert row["days"] == 92
    assert row["months_missed"] == 4               # Mar, Apr, May, Jun
    assert row["pf_no"] == "PF9"


def test_one_day_late_is_one_instalment_missed(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-06-14")

    row = arrears.get_arrears(db, AS_OF)[0]
    assert row["days"] == 1
    assert row["months_missed"] == 1


def test_the_overdue_amount_is_instalments_missed(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-03-15", balance=100000, installment=5750)

    row = arrears.get_arrears(db, AS_OF)[0]
    assert row["overdue_amount"] == 4 * 5750


def test_the_overdue_amount_never_exceeds_the_balance(db):
    """Two years behind on a loan with 3,000 left owes 3,000, not 138,000."""
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2024-06-15", balance=3000, installment=5750)

    row = arrears.get_arrears(db, AS_OF)[0]
    assert row["overdue_amount"] == 3000


def test_a_loan_with_no_instalment_reports_no_amount(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-01-01", installment=0)

    row = arrears.get_arrears(db, AS_OF)[0]
    assert row["overdue_amount"] == 0
    assert row["balance"] == 100000


def test_an_unreadable_due_date_is_skipped_not_fatal(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-01-01")
    _loan(db, ind, "L-002", "2026-02-01")
    db.conn.execute("UPDATE loans SET next_due_date='not a date' WHERE ref='L-002'")
    db.conn.commit()

    rows = arrears.build_rows(db.loans.overdue(AS_OF) + [
        {"ref": "L-002", "next_due_date": "not a date"}], AS_OF)
    assert [r["ref"] for r in rows] == ["L-001"]


def test_worst_arrears_come_first(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-05-01")
    _loan(db, ind, "L-002", "2025-05-01")
    _loan(db, ind, "L-003", "2026-01-01")

    assert [r["ref"] for r in arrears.get_arrears(db, AS_OF)] == \
        ["L-002", "L-003", "L-001"]


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("days,band", [
    (1, "Performing"), (30, "Performing"), (31, "Watch"), (180, "Watch"),
    (181, "Substandard"), (360, "Substandard"), (361, "Doubtful"),
    (540, "Doubtful"), (541, "Loss"), (5000, "Loss"),
])
def test_the_sasra_bands_are_used_as_written(days, band):
    assert arrears.classify(days) == band


# --------------------------------------------------------------------------- #
# Totals
# --------------------------------------------------------------------------- #

def test_the_summary_counts_people_not_loans(db):
    a = _member(db, "Ann")
    b = _member(db, "Ben")
    _loan(db, a, "L-001", "2026-01-01")
    _loan(db, a, "L-002", "2026-02-01")
    _loan(db, b, "L-001", "2026-03-01")

    totals = arrears.summarize(arrears.get_arrears(db, AS_OF))
    assert totals["loans"] == 3
    assert totals["members"] == 2
    assert totals["worst_days"] == 165


def test_the_summary_of_nothing_is_zero():
    assert arrears.summarize([]) == {
        "loans": 0, "members": 0, "overdue_amount": 0, "balance": 0,
        "suspended": 0, "worst_days": 0}


# --------------------------------------------------------------------------- #
# Printing it
# --------------------------------------------------------------------------- #

def test_the_export_prints_the_rows_it_is_given(db, tmp_path):
    """The dialog passes its *filtered* rows, so the file matches the screen."""
    import pandas as pd

    a = _member(db, "Ann", pf_no="PF1")
    b = _member(db, "Ben", pf_no="PF2")
    _loan(db, a, "L-001", "2026-01-01")
    _loan(db, b, "L-001", "2026-02-01")

    rows = arrears.get_arrears(db, AS_OF)
    out = tmp_path / "arrears.csv"
    ok, _msg = ReportGenerator(db).generate_arrears_report(str(out), rows[:1],
                                                           as_of=AS_OF)
    assert ok

    written = pd.read_csv(out)
    assert list(written["Member"])[:1] == ["Ann"]
    assert len(written) == 2                       # one loan + the TOTAL line


def test_the_export_totals_the_rows(db, tmp_path):
    import pandas as pd

    a = _member(db, "Ann")
    b = _member(db, "Ben")
    _loan(db, a, "L-001", "2026-03-15", installment=1000)
    _loan(db, b, "L-001", "2026-03-15", installment=2000)

    rows = arrears.get_arrears(db, AS_OF)
    out = tmp_path / "arrears.csv"
    ReportGenerator(db).generate_arrears_report(str(out), rows, as_of=AS_OF)

    written = pd.read_csv(out)
    total = written.iloc[-1]
    assert "TOTAL — 2 member(s), 2 loan(s)" in str(total["Member"])
    assert total["Overdue (est.)"] == 4 * 1000 + 4 * 2000


def test_exporting_an_empty_list_says_so_instead_of_writing_a_file(db, tmp_path):
    out = tmp_path / "arrears.csv"
    ok, message = ReportGenerator(db).generate_arrears_report(str(out), [])
    assert not ok
    assert "no loans in arrears" in message
    assert not out.exists()


def test_every_report_column_exists_on_a_row(db):
    ind = _member(db, "Ann")
    _loan(db, ind, "L-001", "2026-01-01")
    row = arrears.get_arrears(db, AS_OF)[0]

    for key, _label in ReportGenerator.ARREARS_COLUMNS:
        assert key == "status" or key in row, f"{key} missing from arrears rows"
