"""Tests for the schema-complete DB import (new fields + funds + suspensions)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.database import DatabaseManager
from src.services.christmas_service import ChristmasService
from src.services.benevolent_service import BenevolentService


def _make_source(d):
    src = DatabaseManager(os.path.join(d, "src.db"))
    ind = src.add_individual("Jane Doe", "0712", "j@x",
                             employment_status="Active", pf_no="PF-5", id_no="ID-9")
    src.retire_individual(ind, "2026-02-01")  # -> is_retired=1, status='Retired'
    loan_id = src.add_loan_record(ind, "L-001", 10000, 12000, 12000, 1000, 100,
                                  "2025-01-01", "2025-02-01")
    if not loan_id:
        loan_id = src.conn.execute("SELECT id FROM loans WHERE ref='L-001'").fetchone()[0]
    src.record_suspension(loan_id, "2025-03-01", "2025-05-01")  # closed historical
    ChristmasService(src).add_deposit(ind, 500, "2025-07-01")
    b = BenevolentService(src)
    b.enroll(ind, 200, "2025-01-01")
    b.deduct_single(ind)
    src.add_savings_transaction(ind, "2025-01-10", "Deposit", 1000, "")
    return os.path.join(d, "src.db"), ind


def test_import_carries_new_fields_funds_and_suspensions():
    d = tempfile.mkdtemp()
    src_path, src_ind = _make_source(d)
    dest = DatabaseManager(os.path.join(d, "dest.db"))
    res = dest.import_selected_data(
        src_path, [src_ind],
        options={"import_loans": True, "import_savings": True, "import_funds": True},
        decision_map={src_ind: "new"})
    assert res["status"] in ("success", "partial"), res

    inds = dest.get_individuals()
    assert len(inds) == 1
    new_id = inds[0][0]
    det = dest.get_individual(new_id)
    assert det["employment_status"] == "Retired"
    assert det["pf_no"] == "PF-5"
    assert det["id_no"] == "ID-9"
    assert det["is_retired"] == 1
    assert det["retired_date"] == "2026-02-01"

    assert ChristmasService(dest).get_balance(new_id) == 500
    assert BenevolentService(dest).get_total(new_id) == 200
    assert len(dest.get_loan_suspensions(new_id)) == 1
    assert dest.get_savings_balance(new_id) == 1000


def test_import_drops_colliding_pf_and_id():
    d = tempfile.mkdtemp()
    src_path, src_ind = _make_source(d)  # source member has PF-5 / ID-9
    dest = DatabaseManager(os.path.join(d, "dest.db"))
    dest.add_individual("Existing", "0", "e@x", pf_no="PF-5", id_no="ID-9")
    res = dest.import_selected_data(
        src_path, [src_ind],
        options={"import_loans": False, "import_savings": False, "import_funds": False},
        decision_map={src_ind: "new"})
    assert res["status"] in ("success", "partial"), res
    imported = [i for i in dest.get_individuals() if i[1] == "Jane Doe"][0]
    det = dest.get_individual(imported[0])
    assert (det["pf_no"] or "") == ""   # colliding PF dropped, member still imported
    assert (det["id_no"] or "") == ""
    assert dest.pf_no_owner("PF-5") == "Existing"  # original keeps it
