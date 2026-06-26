"""Tests for the Excel import (matcher + roster + contribution)."""
import os
import sys
import tempfile

import openpyxl

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.services.name_matcher import NameMatcher
from src.services.excel_import import ExcelImporter
from src.services.christmas_service import ChristmasService


def _db():
    return DatabaseManager(os.path.join(tempfile.mkdtemp(), "e.db"))


def _xlsx(d, rows):
    path = os.path.join(d, "in.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


# ----------------------------- matcher ------------------------------------ #
def test_matcher_classifies():
    m = NameMatcher([(1, "ESTHA NDUTA"), (2, "JOHN MARANGA"), (3, "ALICE MUMBI")])
    assert m.classify("Alice Mumbi Kingara")[0] == "match"
    v, b = m.classify("Esther Nduta Gakuya")   # spelling variant
    assert v in ("match", "review") and b["id"] == 1
    assert m.classify("Zachary Unknown")[0] == "none"


# ----------------------------- roster ------------------------------------- #
def test_roster_import_updates_and_creates():
    db = _db()
    existing = db.add_individual("ALICE MUMBI", "0", "a@x")  # to be matched + updated
    d = tempfile.mkdtemp()
    path = _xlsx(d, [
        ("IDNO", "NAMES", "PHONE NUMBER"),
        ("111", "Alice Mumbi Kingara", "0722 111 111"),  # matches existing
        ("222", "Brand New Person", "0733 222 222"),     # unmatched -> create
    ])
    imp = ExcelImporter(db)
    headers, rows = imp.read_sheet(path)
    mapping = imp.detect_mapping(headers)
    assert mapping["type"] == "roster"
    assert mapping["fields"]["name"] == "NAMES" and mapping["fields"]["id_no"] == "IDNO"

    plan = imp.build_plan(rows, mapping)
    actions = {r["source_name"]: r["action"] for r in plan["rows"]}
    assert actions["Alice Mumbi Kingara"] == "update"
    assert actions["Brand New Person"] == "create"

    stats = imp.apply(plan)
    assert stats["updated"] == 1 and stats["created"] == 1
    det = db.get_individual(existing)
    assert det["id_no"] == "111" and det["phone"] == "0722111111"
    new = [i for i in db.get_individuals() if i[1] == "Brand New Person"][0]
    assert db.get_individual(new[0])["id_no"] == "222"


def test_roster_skips_colliding_id():
    db = _db()
    db.add_individual("OWNER", "0", "o@x", id_no="999")
    a = db.add_individual("ALICE MUMBI", "0", "a@x")
    d = tempfile.mkdtemp()
    path = _xlsx(d, [("IDNO", "NAMES"), ("999", "Alice Mumbi")])
    imp = ExcelImporter(db)
    headers, rows = imp.read_sheet(path)
    plan = imp.build_plan(rows, imp.detect_mapping(headers))
    stats = imp.apply(plan)
    assert (db.get_individual(a)["id_no"] or "") == ""   # collision skipped
    assert any("999" in w for w in stats["warnings"])


# -------------------------- contribution ---------------------------------- #
def test_contribution_import_sets_pf_and_deposits():
    db = _db()
    ind = db.add_individual("GEORGINA NJERI", "0", "g@x")
    d = tempfile.mkdtemp()
    path = _xlsx(d, [
        ("Employee Number", "Employee Name", "July", "August", "September"),
        ("2874", "Georgina Njeri Maina", "2500", "2500", "0"),
    ])
    imp = ExcelImporter(db)
    headers, rows = imp.read_sheet(path)
    mapping = imp.detect_mapping(headers)
    assert mapping["type"] == "contribution"
    assert len(mapping["month_columns"]) == 3
    assert mapping["fields"]["pf_no"] == "Employee Number"

    plan = imp.build_plan(rows, mapping, fund="christmas", fy_start_year=2025)
    row = plan["rows"][0]
    assert row["action"] == "update"
    assert row["months"] == [("2025-07-01", 2500.0), ("2025-08-01", 2500.0)]

    stats = imp.apply(plan, fund="christmas")
    assert stats["deposits"] == 2 and stats["total"] == 5000.0
    assert db.get_individual(ind)["pf_no"] == "2874"
    assert ChristmasService(db).get_balance(ind) == 5000
