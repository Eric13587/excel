"""Phase 1c — journal integrity verification."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import integrity
from src.database import DatabaseManager


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "i.db"))
    yield db
    db.close()


def _check(report, name):
    for c in report.checks:
        if c.name == name:
            return c
    raise AssertionError(f"no check named {name!r}; have "
                         f"{[c.name for c in report.checks]}")


def _seed_member_with_loan(db):
    ind = db.add_individual("Member", "0700", "m@x.com")
    cur = db.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, status, start_date) "
                "VALUES (?, 'L-001', 1000, 1150, 1000, 100, 'Active', '2026-01-01')", (ind,))
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, "
                "balance, principal_portion, interest_portion) "
                "VALUES (?, '2026-01-01', 'Loan Issued', 'L-001', 1000, 0, 1000, 0, 0)", (ind,))
    db.conn.commit()
    return ind


# --------------------------------------------------------------------------- #
# Healthy journal
# --------------------------------------------------------------------------- #

def test_fresh_journal_has_no_failures(journal):
    report = integrity.verify(journal)
    assert report.ok, [(c.name, c.detail) for c in report.failures]


def test_report_covers_every_category(journal):
    categories = {c.category for c in integrity.verify(journal).checks}
    assert categories == {"structural", "schema", "referential",
                          "accounting", "phase 4 readiness"}


def test_live_connection_enforcement_is_reported(journal):
    report = integrity.verify(journal)
    assert _check(report, "foreign keys enforced on this connection").status == integrity.PASS


def test_schema_checks_see_migration_001(journal):
    report = integrity.verify(journal)
    assert _check(report, "schema version").status == integrity.PASS
    assert _check(report, "index idx_loans_member_ref").status == integrity.PASS


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def test_detects_orphaned_rows(journal):
    ind = _seed_member_with_loan(journal)
    journal.conn.execute("DROP TRIGGER IF EXISTS trg_cascade_individual_delete")
    journal.conn.execute("PRAGMA foreign_keys = OFF")
    journal.conn.execute("DELETE FROM individuals WHERE id=?", (ind,))
    journal.conn.commit()

    report = integrity.verify(journal)
    orphans = _check(report, "orphaned child rows")
    assert orphans.status == integrity.FAIL
    assert orphans.count >= 2                     # the loan and the ledger row
    assert _check(report, "foreign keys enforceable").status == integrity.FAIL
    assert not report.ok


def test_detects_cached_loan_balance_drift(journal):
    ind = _seed_member_with_loan(journal)
    # The ledger says 1000 was disbursed; corrupt the cache to disagree.
    journal.conn.execute("UPDATE loans SET balance = 9999 WHERE individual_id=?", (ind,))
    journal.conn.commit()

    drift = _check(integrity.verify(journal), "loans.balance matches its ledger")
    assert drift.status == integrity.WARN
    assert drift.count == 1


def test_detects_settled_loan_with_a_balance(journal):
    ind = _seed_member_with_loan(journal)
    journal.conn.execute("UPDATE loans SET status='Paid', balance=500 WHERE individual_id=?", (ind,))
    journal.conn.commit()

    check = _check(integrity.verify(journal), "settled loans carry a zero balance")
    assert check.status == integrity.WARN
    assert check.count == 1


def test_phase4_readiness_is_clean_once_the_constraints_are_enforced(journal):
    """Migration 004 turned these probes into constraints, so a migrated journal
    cannot hold a value they would flag. The probes stay: they still describe a
    pre-004 journal, and the reporting is what tells you it is safe to migrate."""
    import sqlite3
    ind = journal.add_individual("Modern", "0700", "m@x.com")
    with pytest.raises(sqlite3.IntegrityError):
        journal.conn.execute(
            "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, balance) "
            "VALUES (?, '2026-01-01', 'Loan Disbursed', 'L-001', 100, 100)", (ind,))
    journal.conn.rollback()

    report = integrity.verify(journal)
    assert _check(report, "ledger.event_type in vocabulary").status == integrity.PASS
    assert report.ok


def test_phase4_readiness_clean_on_a_healthy_journal(journal):
    _seed_member_with_loan(journal)
    report = integrity.verify(journal)
    readiness = [c for c in report.checks if c.category == "phase 4 readiness"]
    assert readiness and all(c.status == integrity.PASS for c in readiness)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def test_cli_exit_zero_on_healthy_journal(journal, capsys):
    path = journal.db_name
    journal.close()
    assert integrity.main([path]) == 0
    assert "0 failed" in capsys.readouterr().out


def test_cli_quiet_prints_one_line(journal, capsys):
    path = journal.db_name
    journal.close()
    integrity.main([path, "--quiet"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_cli_reports_a_missing_file(capsys):
    assert integrity.main([os.path.join(tempfile.mkdtemp(), "nope.db")]) == 1
    assert "Cannot open" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Subledger audit trail (Phase 6b)
# --------------------------------------------------------------------------- #

def _audit(journal, entity=None):
    sql = "SELECT operation, entity, entity_id, before_image FROM audit_log"
    if entity:
        sql += f" WHERE entity='{entity}'"
    return journal.conn.execute(sql + " ORDER BY id").fetchall()


def test_a_balance_recalculation_is_not_audited(journal):
    """It rewrites `balance` on thousands of rows and changes nobody's
    position. Auditing it would bury the real events."""
    _seed_member_with_loan(journal)
    journal.conn.execute("DELETE FROM audit_log")
    journal.conn.execute("UPDATE ledger SET balance = balance + 1")
    journal.conn.commit()
    assert _audit(journal) == []


def test_editing_an_amount_records_what_it_was(journal):
    import json
    _seed_member_with_loan(journal)
    journal.conn.execute("DELETE FROM audit_log")
    journal.conn.execute("UPDATE ledger SET added = 750 WHERE event_type='Loan Issued'")
    journal.conn.commit()

    rows = _audit(journal, "ledger")
    assert len(rows) == 1 and rows[0][0] == "UPDATE"
    assert json.loads(rows[0][3])["added"] == 1000.0, "the before-image, not the new value"


def test_deleting_a_member_records_every_money_row_it_takes(journal):
    """The Phase 1 bug that started this: a member's ledger rows were destroyed
    with nothing recording it. The cascade fires the child triggers, so the
    before-images survive the parent."""
    import json
    ind = _seed_member_with_loan(journal)
    journal.add_savings_transaction(ind, "2026-02-01", "Deposit", 500, "")
    journal.conn.execute("DELETE FROM audit_log")
    journal.conn.commit()

    journal.delete_individual(ind)

    logged = {(e, op) for op, e, _, _ in _audit(journal)}
    assert ("ledger", "DELETE") in logged
    assert ("savings", "DELETE") in logged
    assert ("individual", "DELETE") in logged
    gone = json.loads([r for r in _audit(journal, "ledger")][0][3])
    assert gone["added"] == 1000.0 and gone["individual_id"] == ind


def test_money_changes_reads_back_only_money(journal):
    ind = _seed_member_with_loan(journal)
    journal.conn.execute("DELETE FROM audit_log")
    journal.conn.execute("UPDATE individuals SET name='Renamed' WHERE id=?", (ind,))
    journal.conn.execute("UPDATE ledger SET added = 900 WHERE event_type='Loan Issued'")
    journal.conn.commit()

    changes = journal.audit.money_changes()
    assert [c["entity"] for c in changes] == ["ledger"]
    assert journal.audit.money_change_count() == 1
    assert journal.audit.recent(), "the member rename is still in the full trail"


def test_inserting_activity_is_not_a_money_change(journal):
    """Only after-the-fact edits and deletions belong here — posting a
    transaction in the first place is not a restatement."""
    ind = _seed_member_with_loan(journal)
    journal.conn.execute("DELETE FROM audit_log")
    journal.conn.commit()
    journal.add_savings_transaction(ind, "2026-03-01", "Deposit", 100, "")
    assert journal.audit.money_change_count() == 0


# --------------------------------------------------------------------------- #
# Why a loan goes "Paid" with a negative balance
# --------------------------------------------------------------------------- #

def _issued_loan(journal, ref="L-001", member="Borrower"):
    ind = journal.add_individual(member, "07", "b@x")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, status, start_date) "
                "VALUES (?, ?, 100000, 115000, 100000, 5750, 'Active', '2025-01-01')",
                (ind, ref))
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                "deducted, balance, principal_portion, interest_portion) "
                "VALUES (?, '2025-01-01', 'Loan Issued', ?, 100000, 0, 100000, 0, 0)",
                (ind, ref))
    journal.conn.commit()
    return ind


def test_a_loan_with_repayments_and_no_disbursement_is_named(journal):
    """The cause behind production loan 483: ten deductions against a
    disbursement that was never entered."""
    ind = journal.add_individual("No Issue Row", "07", "n@x")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, status, start_date) VALUES (?, 'L-002', 529770, 609235, "
                "-101880, 11717, 'Paid', '2025-01-01')", (ind,))
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                "deducted, balance, principal_portion, interest_portion) "
                "VALUES (?, '2025-08-28', 'Repayment', 'L-002', 0, 11717, 0, 10188, 1529)",
                (ind,))
    journal.conn.commit()

    check = _check(integrity.verify(journal), "loans record their disbursement")
    assert check.status == integrity.WARN and check.count == 1
    assert "L-002" in check.detail and "No Issue Row" in check.detail


def test_a_properly_issued_loan_is_not_flagged(journal):
    _issued_loan(journal)
    assert _check(integrity.verify(journal),
                  "loans record their disbursement").status == integrity.PASS


def test_over_allocated_principal_is_named(journal):
    """The cause behind production loan 193: a buy-off entered as an ordinary
    repayment, allocating more to principal than was outstanding."""
    ind = _issued_loan(journal)
    journal.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, "
        "balance, principal_portion, interest_portion) "
        "VALUES (?, '2025-11-28', 'Repayment', 'L-001', 0, 95833, 0, 111898, 600)", (ind,))
    journal.conn.commit()

    check = _check(integrity.verify(journal), "principal repaid stays within what was lent")
    assert check.status == integrity.WARN and check.count == 1
    assert "11,898.00" in check.detail


def test_repaying_exactly_what_was_lent_is_not_flagged(journal):
    ind = _issued_loan(journal)
    journal.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, deducted, "
        "balance, principal_portion, interest_portion) "
        "VALUES (?, '2025-11-28', 'Repayment', 'L-001', 0, 100000, 0, 100000, 0)", (ind,))
    journal.conn.commit()
    assert _check(integrity.verify(journal),
                  "principal repaid stays within what was lent").status == integrity.PASS


def test_the_checks_are_scoped_per_member(journal):
    """Loan refs are unique per member only. Joining on ref alone would let one
    member's disbursement excuse another member's missing one."""
    _issued_loan(journal, ref="L-001", member="Has Issue Row")
    other = journal.add_individual("No Issue Row", "07", "n@x")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, status, start_date) VALUES (?, 'L-001', 50000, 57500, "
                "-5000, 2875, 'Paid', '2025-01-01')", (other,))
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                "deducted, balance, principal_portion, interest_portion) "
                "VALUES (?, '2025-08-28', 'Repayment', 'L-001', 0, 2875, 0, 2500, 375)",
                (other,))
    journal.conn.commit()

    check = _check(integrity.verify(journal), "loans record their disbursement")
    assert check.count == 1, "the other member's issue row must not cover this one"
    assert "No Issue Row" in check.detail


def test_a_gap_in_deductions_is_reported_with_the_interest_not_charged(journal):
    """Accrual happens inside the deduction loop, not on a calendar, so a month
    with no deduction is also a month with no interest. A gap is therefore
    money, not just a missed payment."""
    ind = journal.add_individual("Gappy", "07", "g@x")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, monthly_interest, status, start_date) VALUES "
                "(?, 'L-001', 100000, 115000, 60000, 5750, 750, 'Active', '2025-01-01')",
                (ind,))
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                "deducted, balance, principal_portion, interest_portion) "
                "VALUES (?, '2025-01-01', 'Loan Issued', 'L-001', 100000, 0, 100000, 0, 0)",
                (ind,))
    for date in ("2025-02-01", "2025-03-01", "2025-07-01"):     # Apr, May, Jun missing
        cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                    "deducted, balance, principal_portion, interest_portion) "
                    "VALUES (?, ?, 'Repayment', 'L-001', 0, 5750, 0, 5000, 750)",
                    (ind, date))
    journal.conn.commit()

    check = _check(integrity.verify(journal), "deductions run without unexplained gaps")
    assert check.status == integrity.WARN and check.count == 1
    assert "3 month(s)" in check.detail
    assert "2,250" in check.detail, "3 months x 750 of interest never charged"


def test_a_gap_covered_by_a_suspension_is_not_reported(journal):
    """A recorded suspension is the legitimate reason for a gap."""
    ind = journal.add_individual("Suspended", "07", "s@x")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal, total_amount, balance, "
                "installment, monthly_interest, status, start_date) VALUES "
                "(?, 'L-001', 100000, 115000, 60000, 5750, 750, 'Active', '2025-01-01')",
                (ind,))
    loan_id = cur.lastrowid
    cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                "deducted, balance, principal_portion, interest_portion) "
                "VALUES (?, '2025-01-01', 'Loan Issued', 'L-001', 100000, 0, 100000, 0, 0)",
                (ind,))
    for date in ("2025-02-01", "2025-07-01"):
        cur.execute("INSERT INTO ledger (individual_id, date, event_type, loan_id, added, "
                    "deducted, balance, principal_portion, interest_portion) "
                    "VALUES (?, ?, 'Repayment', 'L-001', 0, 5750, 0, 5000, 750)",
                    (ind, date))
    cur.execute("INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date, "
                "suspend_until, resumed_date, status) VALUES (?, ?, 'L-001', '2025-03-01', "
                "'2025-07-01', '2025-07-01', 'resumed')", (loan_id, ind))
    journal.conn.commit()

    assert _check(integrity.verify(journal),
                  "deductions run without unexplained gaps").status == integrity.PASS
