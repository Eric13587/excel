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


def _bf_col(df):
    return [c for c in df.columns if c.startswith("B/F")][0]


def test_christmas_custom_report(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    c = ChristmasService(db)
    c.add_deposit(ind, 1000, "2025-12-01")  # before period -> B/F
    c.add_deposit(ind, 500, "2026-01-15")   # in period
    c.add_deposit(ind, 500, "2026-02-15")   # in period
    out = os.path.join(d, "x.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("christmas", out, "2026-01-01", "2026-02-28")
    assert ok, msg
    df = pd.read_csv(out)
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row[_bf_col(df)] == 1000
    assert row[[c for c in df.columns if c.startswith("Contributions")][0]] == 1000
    assert row["Cash Out"] == 0
    assert row["Grand Total"] == 2000
    assert "TOTAL" in df["Name"].values


def test_benevolent_custom_report_no_cashout(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    b = BenevolentService(db)
    b.enroll(ind, 200, "2026-01-01")
    b.catch_up(ind, target_date="2026-03-01")  # Jan, Feb, Mar -> 600
    out = os.path.join(d, "b.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("benevolent", out, "2026-01-01", "2026-03-31")
    assert ok, msg
    df = pd.read_csv(out)
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row[[c for c in df.columns if c.startswith("Contributions")][0]] == 600
    assert row["Cash Out"] == 0
    assert row["Grand Total"] == 600


def test_savings_custom_report(env):
    db, d = env
    ind = db.add_individual("Jane", "0", "j@x")
    db.add_savings_transaction(ind, "2026-01-10", "Deposit", 2000, "")
    db.add_savings_transaction(ind, "2026-02-10", "Withdrawal", 500, "")
    out = os.path.join(d, "s.csv")
    ok, msg = ReportGenerator(db).generate_fund_report("savings", out, "2026-01-01", "2026-03-31")
    assert ok, msg
    df = pd.read_csv(out)
    row = df[df["Name"] == "Jane"].iloc[0]
    assert row["Cash Out"] == 500
    assert row["Grand Total"] == 1500  # 0 bf + 2000 - 500


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
