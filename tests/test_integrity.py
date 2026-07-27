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
    assert _check(report, "trigger trg_cascade_individual_delete").status == integrity.PASS


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


def test_detects_unbalanced_journal_entry(journal):
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO journal_entries (entry_date, source, status) "
                "VALUES ('2026-01-01', 'manual', 'posted')")
    entry = cur.lastrowid
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                "VALUES (?, '1000', 100, 0)", (entry,))
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                "VALUES (?, '4900', 0, 60)", (entry,))
    journal.conn.commit()

    report = integrity.verify(journal)
    assert _check(report, "every journal entry balances").status == integrity.FAIL
    assert _check(report, "trial balance nets to zero").status == integrity.FAIL


def test_detects_control_account_break(journal):
    """A loan on the books with nothing posted to Loans Receivable."""
    _seed_member_with_loan(journal)
    report = integrity.verify(journal)
    control = _check(report, "control 1100 Loans Receivable")
    assert control.status == integrity.WARN
    assert "1,000.00" in control.detail


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


def test_phase4_readiness_flags_out_of_vocabulary_values(journal):
    """The values Phase 4's CHECK constraints would reject, warned about now."""
    ind = journal.add_individual("Legacy", "0700", "l@x.com")
    journal.conn.execute(
        "INSERT INTO ledger (individual_id, date, event_type, loan_id, added, balance) "
        "VALUES (?, '2026-01-01', 'Loan Disbursed', 'L-001', 100, 100)", (ind,))
    journal.conn.commit()

    report = integrity.verify(journal)
    check = _check(report, "ledger.event_type in vocabulary")
    assert check.status == integrity.WARN
    assert check.count == 1
    # Readiness gaps are warnings, never failures — the constraint is not in force.
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


def test_cli_exit_one_on_failure(journal, capsys):
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO journal_entries (entry_date, source) VALUES ('2026-01-01','manual')")
    cur.execute("INSERT INTO journal_lines (entry_id, account_code, debit, credit) "
                "VALUES (?, '1000', 100, 0)", (cur.lastrowid,))
    journal.conn.commit()
    path = journal.db_name
    journal.close()

    assert integrity.main([path]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_cli_strict_fails_on_warnings(journal, capsys):
    _seed_member_with_loan(journal)          # produces a control-account warning
    path = journal.db_name
    journal.close()

    assert integrity.main([path]) == 0
    assert integrity.main([path, "--strict"]) == 1
    capsys.readouterr()


def test_cli_quiet_prints_one_line(journal, capsys):
    path = journal.db_name
    journal.close()
    integrity.main([path, "--quiet"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_cli_reports_a_missing_file(capsys):
    assert integrity.main([os.path.join(tempfile.mkdtemp(), "nope.db")]) == 1
    assert "Cannot open" in capsys.readouterr().err
