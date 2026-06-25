"""Tests for SASRA loan-loss provisioning."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.services.gl_service import GLService
from src.services.provisioning import ProvisioningService
from src.config import GL_ALLOWANCE_LOAN_LOSS, GL_LOAN_LOSS_EXPENSE

AS_OF = "2026-06-25"


@pytest.fixture
def prov():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "p.db"))
    gl = GLService(db)
    return ProvisioningService(db, gl), gl, db


def _loan(db, ref, balance, next_due):
    ind = db.add_individual(ref, "0", f"{ref}@x")
    db.add_loan_record(ind, ref, balance, balance, balance, 100, 0, "2024-01-01", next_due)
    return ind


def _seed_one_per_band(db):
    # as_of 2026-06-25; each loan balance 10000
    _loan(db, "PERF", 10000, "2026-06-25")   # 0 days   -> Performing 1%
    _loan(db, "WATCH", 10000, "2026-05-20")  # 36 days  -> Watch 5%
    _loan(db, "SUB", 10000, "2025-12-01")    # 206 days -> Substandard 25%
    _loan(db, "DOUBT", 10000, "2025-06-01")  # 389 days -> Doubtful 50%
    _loan(db, "LOSS", 10000, "2024-10-01")   # 632 days -> Loss 100%
    # Required = 10000 * (0.01+0.05+0.25+0.50+1.00) = 18,100


def test_classification_bands(prov):
    svc, _, db = prov
    _seed_one_per_band(db)
    by_ref = {c['ref']: c for c in svc.classify_loans(AS_OF)}
    assert by_ref['PERF']['bucket'] == 'Performing' and by_ref['PERF']['rate'] == 0.01
    assert by_ref['WATCH']['bucket'] == 'Watch'
    assert by_ref['SUB']['bucket'] == 'Substandard'
    assert by_ref['DOUBT']['bucket'] == 'Doubtful'
    assert by_ref['LOSS']['bucket'] == 'Loss' and by_ref['LOSS']['provision'] == 10000.0


def test_suspended_loan_is_performing(prov):
    svc, _, db = prov
    _loan(db, "SUSP", 10000, "2024-01-01")  # very overdue on paper
    cur = db.conn.cursor()
    cur.execute("UPDATE loans SET is_suspended=1 WHERE ref='SUSP'")
    db.conn.commit()
    c = svc.classify_loans(AS_OF)[0]
    assert c['bucket'] == 'Performing' and c['days_overdue'] == 0


def test_summary_totals_and_par(prov):
    svc, _, db = prov
    _seed_one_per_band(db)
    s = svc.get_provisioning_summary(AS_OF)
    assert s['total_gross'] == 50000.0
    assert s['total_provision'] == 18100.0
    # PAR = non-performing (>30 days) gross / total = 40000 / 50000
    assert s['par_ratio'] == 0.8
    band = {b['bucket']: b for b in s['bands']}
    assert band['Loss']['provision'] == 10000.0 and band['Performing']['provision'] == 100.0


def test_book_provision_posts_and_balances(prov):
    svc, gl, db = prov
    _seed_one_per_band(db)
    res = svc.book_provision(AS_OF)
    assert res['required'] == 18100.0
    assert res['change'] == 18100.0
    # Allowance is now built to the required level.
    assert gl.get_account_balance(GL_ALLOWANCE_LOAN_LOSS, AS_OF) == 18100.0
    assert gl.get_account_balance(GL_LOAN_LOSS_EXPENSE, AS_OF) == 18100.0
    _, balanced = gl.get_trial_balance(AS_OF)
    assert balanced


def test_book_provision_idempotent(prov):
    svc, _, db = prov
    _seed_one_per_band(db)
    svc.book_provision(AS_OF)
    again = svc.book_provision(AS_OF)
    assert again['entry_id'] is None and again['change'] == 0.0


def test_provision_releases_when_portfolio_improves(prov):
    svc, gl, db = prov
    _seed_one_per_band(db)
    svc.book_provision(AS_OF)  # allowance 18,100
    # The Loss loan gets cured (next due in the future) -> provision drops 10,000.
    cur = db.conn.cursor()
    cur.execute("UPDATE loans SET next_due_date='2026-07-01' WHERE ref='LOSS'")
    db.conn.commit()
    res = svc.book_provision(AS_OF)
    assert res['change'] < 0  # released
    assert gl.get_account_balance(GL_ALLOWANCE_LOAN_LOSS, AS_OF) == res['required']
    _, balanced = gl.get_trial_balance(AS_OF)
    assert balanced


def _member_loan(db, name, ref, balance, next_due, savings=0):
    ind = db.add_individual(name, "0", f"{name}@x")
    db.add_loan_record(ind, ref, balance, balance, balance, 100, 0, "2024-01-01", next_due)
    if savings:
        db.add_savings_transaction(ind, "2024-01-01", "Deposit", savings, "dep")
    return ind


def test_savings_netting_reduces_provision(prov):
    svc, _, db = prov
    # Loss loan (100%) of 10,000 with 4,000 attachable savings.
    _member_loan(db, "A", "L", 10000, "2024-10-01", savings=4000)
    netted = svc.classify_loans(AS_OF)[0]
    assert netted['net_exposure'] == 6000.0 and netted['provision'] == 6000.0
    # Disabling netting provisions on the gross balance.
    gross = svc.classify_loans(AS_OF, net_of_savings=False)[0]
    assert gross['provision'] == 10000.0


def test_full_collateral_zero_provision(prov):
    svc, _, db = prov
    _member_loan(db, "B", "L", 10000, "2024-10-01", savings=12000)
    c = svc.classify_loans(AS_OF)[0]
    assert c['net_exposure'] == 0.0 and c['provision'] == 0.0


def test_savings_allocated_pro_rata_across_loans(prov):
    svc, _, db = prov
    ind = db.add_individual("C", "0", "c@x")
    db.add_loan_record(ind, "BIG", 6000, 6000, 6000, 100, 0, "2024-01-01", "2024-10-01")
    db.add_loan_record(ind, "SMALL", 4000, 4000, 4000, 100, 0, "2024-01-01", "2024-10-01")
    db.add_savings_transaction(ind, "2024-01-01", "Deposit", 5000, "dep")
    by_ref = {c['ref']: c for c in svc.classify_loans(AS_OF)}
    # 5,000 split 60/40 -> nets 3,000 and 2,000 (both Loss => provision == net)
    assert by_ref['BIG']['net_exposure'] == 3000.0
    assert by_ref['SMALL']['net_exposure'] == 2000.0


def test_provision_reduces_net_assets_on_balance_sheet(prov):
    svc, gl, db = prov
    _seed_one_per_band(db)
    # Disburse the loans into the GL so there are gross loan assets to impair.
    for i, ref in enumerate(["PERF", "WATCH", "SUB", "DOUBT", "LOSS"]):
        gl.post_loan_disbursement(10000, "2024-01-01", f"ledger:{i}")
    before = gl.get_balance_sheet(AS_OF)['total_assets']
    svc.book_provision(AS_OF)
    after = gl.get_balance_sheet(AS_OF)['total_assets']
    # Net assets fall by the provision; the contra-asset allowance does the work.
    assert round(before - after, 2) == 18100.0
    assert gl.get_balance_sheet(AS_OF)['is_balanced']
