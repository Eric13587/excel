"""Tests for the generic fund report (savings/christmas/benevolent, quarter/custom)."""
import os
import sys
import tempfile

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.reports import ReportGenerator
from src.services.christmas_service import ChristmasService
from src.services.benevolent_service import BenevolentService


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    db = DatabaseManager(os.path.join(d, "r.db"))
    return db, d


def test_christmas_custom_report_monthly_layout(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x", pf_no="PF-1")
    c = ChristmasService(db)
    c.add_deposit(ind, 1000, "2025-12-01")  # before period -> not counted
    c.add_deposit(ind, 500, "2026-01-15")   # Jan
    c.add_deposit(ind, 500, "2026-02-15")   # Feb
    out = os.path.join(d, "x.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01", "2026-02-28")
    assert ok, msg
    df = pd.read_csv(out)
    # xmas.xlsx structure: PF No | Name | Employment Status | <months> | Total
    assert list(df.columns) == ["PF No", "Name", "Employment Status", "Jan-26", "Feb-26", "Total"]
    row = df[df["Name"] == "Jane"].iloc[0]
    assert str(row["PF No"]) == "PF-1"
    assert row["Employment Status"] == "Active"
    assert row["Jan-26"] == 500 and row["Feb-26"] == 500
    assert row["Total"] == 1000  # the Dec deposit (pre-period) is excluded
    assert "TOTAL" in df["Name"].values


def test_benevolent_custom_report_monthly(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    b = BenevolentService(db)
    b.enroll(ind, 200, "2026-01-01")
    b.catch_up(ind, target_date="2026-03-01")  # Jan, Feb, Mar -> 600
    out = os.path.join(d, "b.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("benevolent", out, "2026-01-01", "2026-03-31")
    assert ok, msg
    df = pd.read_csv(out)
    assert list(df.columns) == ["PF No", "Name", "Employment Status", "Jan-26", "Feb-26", "Mar-26", "Total"]
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row["Jan-26"] == 200 and row["Feb-26"] == 200 and row["Mar-26"] == 200
    assert row["Total"] == 600


def test_savings_custom_report_deposits_only(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    db.add_savings_transaction(ind, "2026-01-10", "Deposit", 2000, "")
    db.add_savings_transaction(ind, "2026-02-10", "Withdrawal", 500, "")  # ignored
    out = os.path.join(d, "s.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("savings", out, "2026-01-01", "2026-03-31")
    assert ok, msg
    df = pd.read_csv(out)
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row["Jan-26"] == 2000
    assert row["Feb-26"] == 0    # withdrawal excluded (deposits only)
    assert row["Total"] == 2000


def test_custom_report_includes_zero_row_participant(env):
    """A fund participant with no deposits in the period still appears (zero row)."""
    db, d = env
    ind = db.add_individual("Quiet", "0", "q@x")
    ChristmasService(db).add_deposit(ind, 1000, "2025-03-01")  # participant, but outside period
    out = os.path.join(d, "z.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01", "2026-02-28")
    assert ok, msg
    df = pd.read_csv(out)
    row = df[df["Name"] == "Quiet"].iloc[0]
    assert row["Jan-26"] == 0 and row["Feb-26"] == 0 and row["Total"] == 0


def test_christmas_quarter_report_has_monthly_columns(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    c = ChristmasService(db)
    for m in ("01", "02", "03"):
        c.add_deposit(ind, 500, f"2026-{m}-15")
    out = os.path.join(d, "q.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01")  # quarter
    assert ok, msg
    df = pd.read_csv(out)
    assert "Sub Total" in df.columns
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row["Sub Total"] == 1500


def test_retired_member_excluded_before_period_included_during(env):
    db, d = env
    # retired BEFORE the period -> excluded
    early = db.add_individual("Early", "0", "e@x")
    ChristmasService(db).add_deposit(early, 1000, "2025-06-01")
    db.retire_individual(early, "2025-09-01")
    # retired DURING the period -> included
    during = db.add_individual("During", "0", "u@x")
    ChristmasService(db).add_deposit(during, 800, "2026-01-10")
    db.retire_individual(during, "2026-02-15")
    # active -> included
    active = db.add_individual("Active", "0", "a@x")
    ChristmasService(db).add_deposit(active, 500, "2026-01-20")

    out = os.path.join(d, "r.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01", "2026-03-31")
    assert ok, msg
    names = set(pd.read_csv(out)["Name"].values)
    assert "Early" not in names
    assert {"During", "Active"} <= names


def test_custom_end_before_start_errors(env):
    db, d = env
    db.add_individual("Jane", "0", "j@x")
    ok, msg = ReportGenerator(db).generate_fund_report(
        "christmas", os.path.join(d, "e.csv"), "2026-03-01", "2026-01-01")
    assert not ok and "before" in msg.lower()


def test_custom_column_ordering_fund_report(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x", pf_no="PF-1")
    c = ChristmasService(db)
    c.add_deposit(ind, 500, "2026-01-15")
    c.add_deposit(ind, 500, "2026-02-15")

    # Custom columns for Christmas custom mode
    out = os.path.join(d, "x_custom_cols.csv")
    chosen_cols = ["Name", "Jan-26", "Total"]
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01", "2026-02-28", columns=chosen_cols)
    assert ok, msg
    df = pd.read_csv(out)
    assert list(df.columns) == chosen_cols
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row["Jan-26"] == 500
    assert row["Total"] == 1000

    # Custom columns for Christmas quarter mode
    out_q = os.path.join(d, "x_quarter_cols.csv")
    # For quarter mode, default is: ["Name", bf_label, m1_name, m2_name, m3_name, "Sub Total", "Cash Out", "Grand Total"]
    # Let's specify a subset and a different order
    chosen_cols_q = ["Grand Total", "Name", "Sub Total"]
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out_q, "2026-01-01", columns=chosen_cols_q)
    assert ok, msg
    df_q = pd.read_csv(out_q)
    assert list(df_q.columns) == chosen_cols_q


def test_custom_column_ordering_quarterly_savings_report(env):
    db, d = env
    db.add_individual("Jane", "0", "j@x")
    out = os.path.join(d, "savings_custom_cols.csv")
    chosen_cols = ["Grand Total", "Name", "Cash Out"]
    ok, msg = ReportGenerator(db).generate_quarterly_savings_report("2026-01-01", out, columns=chosen_cols)
    assert ok, msg
    df = pd.read_csv(out)
    assert list(df.columns) == chosen_cols


def test_custom_column_ordering_quarterly_loan_report(env):
    db, d = env
    db.add_individual("Jane", "0", "j@x")
    out = os.path.join(d, "loan_custom_cols.csv")
    chosen_cols = ["Grand Total", "Name", "Loan Amount", "Sub Total"]
    ok, msg = ReportGenerator(db).generate_quarterly_report("2026-01-01", out, columns=chosen_cols)
    assert ok, msg
    df = pd.read_csv(out)
    assert list(df.columns) == chosen_cols

