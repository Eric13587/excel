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
    assert categories == {"structural", "schema", "referential", "accounting",
                          "projection", "fiscal periods", "phase 4 readiness"}


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


# --------------------------------------------------------------------------- #
# Projection health (Phase 5b)
# --------------------------------------------------------------------------- #

def test_projection_checks_report_an_undrained_queue(journal):
    from src.services.gl_service import GLService
    svc = GLService(journal)
    _seed_member_with_loan(journal)
    svc.sync()
    assert _check(integrity.verify(journal), "projection change queue drained").status \
        == integrity.PASS

    journal.add_savings_transaction(
        journal.conn.execute("SELECT id FROM individuals").fetchone()[0],
        "2026-02-01", "Deposit", 100, "")
    check = _check(integrity.verify(journal), "projection change queue drained")
    assert check.status == integrity.WARN and check.count == 1


def test_projection_baseline_is_reported_before_the_first_sync(journal):
    _seed_member_with_loan(journal)
    assert _check(integrity.verify(journal), "projection baseline established").status \
        == integrity.WARN


def test_detects_an_entry_that_outlived_its_source_row(journal):
    from src.services.gl_service import GLService
    svc = GLService(journal)
    _seed_member_with_loan(journal)
    svc.sync()

    # Delete the row behind the entry without letting the queue see it.
    journal.conn.execute("DROP TRIGGER trg_outbox_ledger_del")
    journal.conn.execute("DELETE FROM ledger")
    journal.conn.commit()

    check = _check(integrity.verify(journal), "projected entries have a live source row")
    assert check.status == integrity.WARN and check.count == 1


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


def test_period_checks_report_an_empty_register(journal):
    """How migration 008 ships: nothing declared, so nothing is frozen."""
    check = _check(integrity.verify(journal), "periods declared")
    assert check.status == integrity.PASS and check.count == 0


def test_period_checks_report_what_is_settled(journal):
    from src.services.period_service import FiscalPeriodService
    from src.services.gl_service import GLService
    GLService(journal).sync()
    svc = FiscalPeriodService(journal)
    pid = svc.create("Q1", "2026-01-01", "2026-03-31")
    svc.close(pid)

    report = integrity.verify(journal)
    assert _check(report, "books settled through").detail == "2026-03-31"
    assert _check(report, "no edits inside a closed period").status == integrity.PASS


def test_snapshot_drift_is_a_failure_not_a_warning(journal):
    """A stored figure that disagrees with the journal is not a nuance."""
    from src.services.period_service import FiscalPeriodService
    from src.services.gl_service import GLService
    _seed_member_with_loan(journal)
    GLService(journal).sync()
    svc = FiscalPeriodService(journal)
    svc.close(svc.create("Q1", "2026-01-01", "2026-03-31"))
    assert _check(integrity.verify(journal),
                  "period snapshots match the journal").status == integrity.PASS

    journal.conn.execute("UPDATE account_snapshots SET closing_balance = 12345 "
                         "WHERE account_code='1100'")
    journal.conn.commit()
    check = _check(integrity.verify(journal), "period snapshots match the journal")
    assert check.status == integrity.FAIL and check.count == 1
    assert not integrity.verify(journal).ok
