"""Tests for created_at/updated_at timestamps and the CRUD audit_log trail."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager


def _db():
    return DatabaseManager(os.path.join(tempfile.mkdtemp(), "a.db"))


def test_audit_log_captures_individual_crud():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    log = db.get_audit_log()
    assert any(e["operation"] == "INSERT" and e["entity"] == "individual"
               and e["entity_id"] == ind for e in log)

    db.update_individual(ind, "Jane K", "0722", "jk@x")  # meaningful edit
    assert any(e["operation"] == "UPDATE" and e["entity_id"] == ind for e in db.get_audit_log())
    assert db.get_individual(ind)["updated_at"] is not None

    db.delete_individual(ind)
    assert any(e["operation"] == "DELETE" and e["entity_id"] == ind for e in db.get_audit_log())


def test_default_deduction_update_not_audited():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    before = len([e for e in db.get_audit_log() if e["operation"] == "UPDATE"])
    db.update_individual_deduction(ind, 500)  # churny derived field — should NOT log
    after = len([e for e in db.get_audit_log() if e["operation"] == "UPDATE"])
    assert after == before


def test_loan_created_timestamp_and_audit():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    db.add_loan_record(ind, "L-001", 100000, 115000, 100000, 9584, 1250,
                       "2025-01-01", "2025-02-01")
    row = db.conn.execute("SELECT id, created_at FROM loans WHERE ref='L-001'").fetchone()
    assert row[1] is not None  # created_at populated by trigger
    assert any(e["entity"] == "loan" and e["operation"] == "INSERT" for e in db.get_audit_log())


def test_audit_log_filter_by_entity():
    db = _db()
    a = db.add_individual("A", "0", "a@x")
    db.add_individual("B", "0", "b@x")
    db.update_individual(a, "A2", "0", "a@x")
    log = db.get_audit_log(entity="individual", entity_id=a)
    assert log and all(e["entity_id"] == a for e in log)
    assert any(e["operation"] == "UPDATE" for e in log)
