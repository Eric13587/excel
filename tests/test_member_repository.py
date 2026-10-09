"""Phase 2b — MemberRepository.

Weighted towards behaviour preservation: this is a refactor, so the return
shapes and the partial-update semantics matter more than the new API.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.exceptions import TransactionError
from src.db import MemberRepository


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "m.db"))
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# Return shapes the callers depend on
# --------------------------------------------------------------------------- #

def test_rows_returns_tuples_indexable_by_position(journal):
    """Ten call sites do m[0] for the id; the raw tuple shape is load-bearing."""
    a = journal.add_individual("Alpha", "0700", "a@x.com")
    journal.add_individual("Beta", "0711", "b@x.com")

    rows = journal.get_individuals()
    assert isinstance(rows, list) and isinstance(rows[0], tuple)
    assert [r[0] for r in rows] == [a, a + 1]
    assert rows[0][1] == "Alpha"          # column order: id, name, ...


def test_get_returns_a_dict_keyed_by_column(journal):
    ind = journal.add_individual("Gamma", "0700", "g@x.com", 500)
    member = journal.get_individual(ind)
    assert member["id"] == ind
    assert member["name"] == "Gamma"
    assert member["default_deduction"] == 500
    assert member["employment_status"] == "Active"


def test_get_returns_none_for_a_missing_member(journal):
    assert journal.get_individual(99999) is None


def test_name_lookup_keeps_its_display_fallback(journal):
    ind = journal.add_individual("Delta", "0700", "d@x.com")
    assert journal.get_individual_name(ind) == "Delta"
    # The fallback label is a presentation concern and stays on the manager;
    # the repository reports absence honestly.
    assert journal.get_individual_name(99999) == "Individual 99999"
    assert journal.members.name_of(99999) is None


# --------------------------------------------------------------------------- #
# Retirement derivation
# --------------------------------------------------------------------------- #

def test_retirement_for_status_derivation():
    assert MemberRepository.retirement_for_status("Active") == (0, None)
    assert MemberRepository.retirement_for_status(None) == (0, None)
    assert MemberRepository.retirement_for_status("") == (0, None)

    flag, date = MemberRepository.retirement_for_status("Retired")
    assert flag == 1 and date

    # An existing retirement date is preserved rather than restamped.
    assert MemberRepository.retirement_for_status("Deceased", "2020-01-01") == (1, "2020-01-01")


def test_non_active_status_retires_on_create(journal):
    ind = journal.add_individual("Retiree", "0700", "r@x.com", employment_status="Retired")
    member = journal.get_individual(ind)
    assert member["is_retired"] == 1 and member["retired_date"]


def test_status_change_drives_the_retirement_flag(journal):
    ind = journal.add_individual("Switcher", "0700", "s@x.com")
    journal.update_individual(ind, "Switcher", "0700", "s@x.com", employment_status="Resigned")
    assert journal.get_individual(ind)["is_retired"] == 1

    journal.update_individual(ind, "Switcher", "0700", "s@x.com", employment_status="Active")
    member = journal.get_individual(ind)
    assert member["is_retired"] == 0 and member["retired_date"] is None


def test_retire_and_reinstate(journal):
    ind = journal.add_individual("Rita", "0700", "r@x.com")
    journal.retire_individual(ind, "2026-03-01")
    member = journal.get_individual(ind)
    assert (member["is_retired"], member["retired_date"], member["employment_status"]) == \
           (1, "2026-03-01", "Retired")

    journal.reinstate_individual(ind)
    member = journal.get_individual(ind)
    assert (member["is_retired"], member["retired_date"], member["employment_status"]) == \
           (0, None, "Active")


# --------------------------------------------------------------------------- #
# Uniqueness probes
# --------------------------------------------------------------------------- #

def test_name_exists_is_case_insensitive(journal):
    journal.add_individual("John Doe", "0700", "j@x.com")
    assert journal.individual_name_exists("john doe")
    assert journal.individual_name_exists("JOHN DOE")
    assert not journal.individual_name_exists("Jane Doe")


def test_pf_and_id_owner_lookup(journal):
    ind = journal.add_individual("Owner", "0700", "o@x.com", pf_no="PF-1", id_no="ID-1")
    assert journal.pf_no_owner("PF-1") == "Owner"
    assert journal.id_no_owner("ID-1") == "Owner"
    assert journal.pf_no_owner("PF-2") is None
    # Excluding the holder is how an edit checks "is anyone *else* using this".
    assert journal.pf_no_owner("PF-1", exclude_id=ind) is None


def test_blank_numbers_are_never_duplicates(journal):
    journal.add_individual("NoNumbers", "0700", "n@x.com")
    for blank in ("", "   ", None):
        assert journal.pf_no_owner(blank) is None
        assert journal.id_no_owner(blank) is None


def test_owner_lookup_strips_whitespace(journal):
    journal.add_individual("Spacey", "0700", "s@x.com", pf_no="PF-9")
    assert journal.pf_no_owner("  PF-9  ") == "Spacey"


def test_owner_column_is_whitelisted(journal):
    with pytest.raises(ValueError):
        journal.members._owner_of("name", "x", None)


# --------------------------------------------------------------------------- #
# Partial update semantics
# --------------------------------------------------------------------------- #

def test_update_does_not_clobber_unsupplied_fields(journal):
    ind = journal.add_individual("Keep", "0700", "k@x.com",
                                 employment_status="Retired", pf_no="PF-7", id_no="ID-7")
    journal.update_individual(ind, "Renamed", "0711", "new@x.com")

    member = journal.get_individual(ind)
    assert (member["name"], member["phone"], member["email"]) == ("Renamed", "0711", "new@x.com")
    # Untouched because they were not supplied.
    assert member["employment_status"] == "Retired"
    assert member["pf_no"] == "PF-7"
    assert member["id_no"] == "ID-7"


def test_update_applies_supplied_optional_fields(journal):
    ind = journal.add_individual("Fields", "0700", "f@x.com")
    journal.update_individual(ind, "Fields", "0700", "f@x.com", pf_no="PF-A", id_no="ID-A")
    member = journal.get_individual(ind)
    assert member["pf_no"] == "PF-A" and member["id_no"] == "ID-A"


def test_update_is_atomic_across_its_statements(journal):
    """The multi-statement update now runs in one transaction, so a failure
    part-way leaves the row exactly as it was."""
    a = journal.add_individual("First", "0700", "a@x.com", pf_no="PF-DUP")
    b = journal.add_individual("Second", "0711", "b@x.com", pf_no="PF-B")

    # The unique index on pf_no rejects the collision after the name has been
    # updated in the same transaction.
    with pytest.raises(TransactionError):
        journal.update_individual(b, "Renamed", "0711", "b@x.com", pf_no="PF-DUP")

    member = journal.get_individual(b)
    assert member["name"] == "Second", "the earlier statement was not rolled back"
    assert member["pf_no"] == "PF-B"
    assert journal.get_individual(a)["pf_no"] == "PF-DUP"


def test_default_deduction_update(journal):
    ind = journal.add_individual("Deduct", "0700", "d@x.com")
    journal.update_individual_deduction(ind, 1234.5)
    assert journal.get_individual(ind)["default_deduction"] == 1234.5


# --------------------------------------------------------------------------- #
# Deletion
# --------------------------------------------------------------------------- #

def test_delete_removes_the_member_and_cascades(journal):
    ind = journal.add_individual("Gone", "0700", "g@x.com")
    journal.conn.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, "
                         "added, balance) VALUES (?, '2026-01-01', 'Loan Issued', 'L-001', 10, 10)",
                         (ind,))
    journal.conn.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, "
                         "balance) VALUES (?, '2026-01-01', 'Deposit', 10, 10)", (ind,))
    journal.conn.commit()

    journal.delete_individual(ind)

    assert journal.get_individual(ind) is None
    for table in ("ledger", "savings"):
        assert journal.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE individual_id=?", (ind,)).fetchone()[0] == 0


def test_audit_triggers_still_fire_through_the_repository(journal):
    """The SQL is unchanged, so the trigger-maintained audit trail is too."""
    ind = journal.add_individual("Audited", "0700", "a@x.com")
    journal.update_individual(ind, "Audited Renamed", "0700", "a@x.com")
    journal.delete_individual(ind)

    ops = [r["operation"] for r in journal.get_audit_log(entity="individual", entity_id=ind)]
    assert set(ops) == {"INSERT", "UPDATE", "DELETE"}


# --------------------------------------------------------------------------- #
# The repository works against a bare connection
# --------------------------------------------------------------------------- #

def test_repository_needs_only_a_connection():
    """Repositories take a sqlite3.Connection, not the DatabaseManager, so they
    cannot reach back into application logic and stay trivially testable."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                 "phone TEXT, email TEXT, default_deduction REAL DEFAULT 0, created_at TEXT, "
                 "employment_status TEXT, pf_no TEXT, id_no TEXT, is_retired INTEGER, "
                 "retired_date TEXT)")
    repo = MemberRepository(conn)
    ind = repo.add("Standalone", "0700", "s@x.com")
    assert repo.get(ind)["name"] == "Standalone"
    assert repo.name_exists("STANDALONE")
