"""Tests for loan-term healing (re-derive stale terms, clear contaminated locks)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.engine import LoanEngine


def _env():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "h.db"))
    return db, LoanEngine(db)


def _add_accrual(db, ind, ref, date, amount, is_edited):
    db.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, "
        "balance, notes, is_edited) VALUES (?, ?, 'Interest Earned', ?, ?, 0, 0, '', ?)",
        (ind, date, ref, amount, is_edited))
    db.conn.commit()


def test_heal_fixes_total_amount_and_clears_accrual_locks():
    db, eng = _env()
    ind = db.add_individual("Jane", "0", "j@x")
    eng.add_loan_event(ind, 300000, 24, "2025-07-28", interest_rate=0.15)  # interest 45000

    # Simulate the legacy anomaly: total_amount wiped to 0 + a locked foreign accrual.
    db.conn.execute("UPDATE loans SET total_amount=0 WHERE individual_id=? AND ref='L-001'", (ind,))
    _add_accrual(db, ind, "L-001", "2025-08-28", 250, 1)  # contaminated (is_edited=1)

    scan = eng.scan_healable_loans()
    target = [s for s in scan if s["ref"] == "L-001"]
    assert target and "total_amount" in target[0]["changes"]
    assert target[0]["contaminated_accruals"] == 1

    res = eng.heal_loan_terms(ind, "L-001")
    assert res["applied"] is True
    total = db.conn.execute("SELECT total_amount FROM loans WHERE individual_id=? AND ref='L-001'",
                            (ind,)).fetchone()[0]
    assert total == 345000.0  # 300000 + 45000, matching current creation logic
    locked = db.conn.execute(
        "SELECT COUNT(*) FROM ledger WHERE individual_id=? AND loan_id='L-001' "
        "AND event_type='Interest Earned' AND is_edited=1", (ind,)).fetchone()[0]
    assert locked == 0  # edit-locks cleared so recalc re-derives accruals


def test_healthy_loan_reports_no_changes():
    db, eng = _env()
    ind = db.add_individual("Jane", "0", "j@x")
    eng.add_loan_event(ind, 120000, 12, "2025-01-01", interest_rate=0.15)
    assert eng.scan_healable_loans() == []           # freshly created -> already correct
    res = eng.heal_loan_terms(ind, "L-001")
    assert res["healable"] is True and res.get("applied") is False


def test_loan_with_topup_not_healable():
    db, eng = _env()
    ind = db.add_individual("Jane", "0", "j@x")
    eng.add_loan_event(ind, 100000, 12, "2025-01-01")
    db.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, balance, notes) "
        "VALUES (?, '2025-03-01', 'Loan Top-Up', 'L-001', 50000, 0, 0, '')", (ind,))
    db.conn.commit()
    plan = eng._loan_heal_plan(ind, "L-001")
    assert plan["healable"] is False and "top-up" in plan["reason"]


def test_edited_repayment_blocks_heal():
    db, eng = _env()
    ind = db.add_individual("Jane", "0", "j@x")
    eng.add_loan_event(ind, 100000, 12, "2025-01-01")
    db.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, balance, notes, is_edited) "
        "VALUES (?, '2025-02-01', 'Repayment', 'L-001', 0, 9000, 0, '', 1)", (ind,))
    db.conn.commit()
    plan = eng._loan_heal_plan(ind, "L-001")
    assert plan["healable"] is False and "edited repayment" in plan["reason"]
