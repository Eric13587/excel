"""Tests for the employment_status and pf_no individual fields."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager


def _db():
    return DatabaseManager(os.path.join(tempfile.mkdtemp(), "i.db"))


def test_add_individual_stores_status_and_pf():
    db = _db()
    ind = db.add_individual("Jane", "0712", "j@x", employment_status="Resigned",
                            pf_no="PF-99", id_no="12345678")
    row = db.get_individual(ind)
    assert row["employment_status"] == "Resigned"
    assert row["pf_no"] == "PF-99"
    assert row["id_no"] == "12345678"


def test_add_individual_defaults_to_active():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    assert db.get_individual(ind)["employment_status"] == "Active"


def test_update_individual_changes_status_and_pf():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    db.update_individual(ind, "Jane K", "0722", "jk@x",
                         employment_status="Suspended", pf_no="PF-1", id_no="ID-7")
    row = db.get_individual(ind)
    assert row["name"] == "Jane K"
    assert row["employment_status"] == "Suspended"
    assert row["pf_no"] == "PF-1"
    assert row["id_no"] == "ID-7"


def test_update_individual_without_fields_preserves_them():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x", employment_status="Resigned",
                            pf_no="PF-7", id_no="ID-9")
    db.update_individual(ind, "Jane", "0", "j2@x")  # no status/pf/id passed
    row = db.get_individual(ind)
    assert row["employment_status"] == "Resigned"
    assert row["pf_no"] == "PF-7"
    assert row["id_no"] == "ID-9"


def test_retire_sets_status_and_reinstate_clears_it():
    db = _db()
    ind = db.add_individual("Jane", "0", "j@x")
    db.retire_individual(ind, "2026-01-31")
    row = db.get_individual(ind)
    assert row["is_retired"] == 1 and row["employment_status"] == "Retired"
    db.reinstate_individual(ind)
    row = db.get_individual(ind)
    assert row["is_retired"] == 0 and row["employment_status"] == "Active"


def test_pf_no_owner_detects_duplicates():
    db = _db()
    a = db.add_individual("Alice", "0", "a@x", pf_no="PF-100")
    b = db.add_individual("Bob", "0", "b@x", pf_no="PF-200")
    assert db.pf_no_owner("PF-100") == "Alice"
    assert db.pf_no_owner("PF-999") is None          # unused
    assert db.pf_no_owner("") is None                # blank never a dup
    assert db.pf_no_owner("PF-100", exclude_id=a) is None  # excluding self
    assert db.pf_no_owner("PF-200", exclude_id=a) == "Bob"


def test_pf_no_unique_index_blocks_duplicate():
    import sqlite3
    db = _db()
    db.add_individual("Alice", "0", "a@x", pf_no="PF-1")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO individuals (name, pf_no) VALUES ('Eve','PF-1')")


def test_existing_positional_columns_unchanged():
    """get_individuals returns tuples indexed positionally elsewhere; the new
    columns must append after is_retired(7)/retired_date(8)."""
    db = _db()
    db.add_individual("Jane", "0", "j@x")
    row = db.get_individuals()[0]
    # id, name, phone, email, default_deduction, created_at, import_id, is_retired, retired_date, ...
    assert row[1] == "Jane"
    assert row[7] == 0  # is_retired still at index 7
