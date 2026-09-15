"""Tests verifying fixes for issues identified in status_report.md (loanmaster.log analysis).

Covers:
1. Migration 001 pre-flight cleanup and success on legacy journals with duplicate orphaned loans.
2. Schema migration through version 12, ensuring duration_months and interest_rate exist.
3. Inactive loan buyoff protection preventing uncaught LoanInactiveError.
4. Defensive guards in open_repair_loans_dialog for unmigrated journals.
5. Orphan deletion handling dangling loan_suspensions referencing deleted loans.
"""
import os
import sqlite3
import tempfile
import pytest

from src.database import DatabaseManager
from src import migrations
from src.exceptions import LoanInactiveError


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_legacy_journal_with_duplicate_orphaned_loans_migrates_cleanly(temp_db_path, monkeypatch):
    """Reproduces the exact failure in loanmaster.log:
    Deleted member with duplicate 'L-002' loans causes UNIQUE constraint failure on idx_loans_member_ref.
    Verifies that the pre-flight migration fix cleans orphans and successfully applies all migrations.
    """
    # Prevent migrations from running during initial DatabaseManager legacy bootstrap
    real_apply = migrations.apply_pending
    monkeypatch.setattr(migrations, "apply_pending", lambda conn: migrations.MigrationReport([], [], None))
    db = DatabaseManager(temp_db_path, bootstrap="legacy")
    monkeypatch.setattr(migrations, "apply_pending", real_apply)

    try:
        # Verify database is currently at legacy pre-migration state (version 0)
        assert migrations.schema_version(db.conn) == 0

        # 1. Active member with valid loans
        ind = db.add_individual("Active Member", "0700", "active@x.com")
        db.conn.execute("""
            INSERT INTO loans (individual_id, ref, principal, total_amount, balance, installment,
                               monthly_interest, start_date, next_due_date, status)
            VALUES (?, 'L-001', 50000, 57500, 50000, 5000, 750, '2026-01-01', '2026-02-01', 'Active')
        """, (ind,))
        db.conn.execute("""
            INSERT INTO loans (individual_id, ref, principal, total_amount, balance, installment,
                               monthly_interest, start_date, next_due_date, status)
            VALUES (?, 'L-002', 30000, 34500, 30000, 3000, 450, '2026-01-01', '2026-02-01', 'Active')
        """, (ind,))

        # 2. Plant orphaned rows for deleted member 999 (with duplicate L-002, exactly like the incident log!)
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("INSERT INTO loans (individual_id, ref, principal, status, start_date) VALUES (999, 'L-001', 57500, 'Paid', '2025-01-01')")
        db.conn.execute("INSERT INTO loans (individual_id, ref, principal, status, start_date) VALUES (999, 'L-002', 57500, 'Paid', '2025-01-01')")
        db.conn.execute("INSERT INTO loans (individual_id, ref, principal, status, start_date) VALUES (999, 'L-002', 50000, 'Active', '2025-01-01')")

        # 3. Orphaned suspension referencing non-existent loan
        db.conn.execute("INSERT INTO loan_suspensions (loan_id, individual_id, loan_ref, start_date) VALUES (9999, 999, 'L-002', '2025-02-01')")
        db.conn.commit()

        # Run migration runner: should execute pre-flight cleanup, apply 001-012 cleanly
        report = migrations.apply_pending(db.conn)
        assert report.ok, f"Migration failed: {report.failed}"
        assert migrations.schema_version(db.conn) >= 12

        # Verify duplicate orphaned loans were removed and valid loans remain
        remaining_loans = db.conn.execute("SELECT individual_id, ref FROM loans WHERE individual_id=999").fetchall()
        assert len(remaining_loans) == 0

        # Verify unique index exists and is active
        idx = db.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_loans_member_ref'").fetchone()
        assert idx is not None

        # Verify duration_months and interest_rate exist
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(loans)").fetchall()]
        assert "duration_months" in cols
        assert "interest_rate" in cols

        # Verify valid member's loans are still intact
        active_loans = db.conn.execute("SELECT ref FROM loans WHERE individual_id=?", (ind,)).fetchall()
        assert sorted([r[0] for r in active_loans]) == ["L-001", "L-002"]
    finally:
        db.close()


def test_inactive_loan_buyoff_validation(temp_db_path):
    """Verifies that attempting buyoff on a settled/paid loan is prevented cleanly."""
    from src.engine import LoanEngine

    db = DatabaseManager(temp_db_path)
    engine = LoanEngine(db)
    try:
        ind = db.add_individual("Borrower", "0712345678", "b@x.com")
        db.add_loan_record(ind, "L-001", 10000, 11500, 0, 1000, 150, "2026-01-01", "2026-02-01")
        # Mark loan as Paid
        db.conn.execute("UPDATE loans SET status='Paid', balance=0 WHERE individual_id=? AND ref='L-001'", (ind,))
        db.conn.commit()

        # Direct engine call should raise LoanInactiveError
        with pytest.raises(LoanInactiveError) as excinfo:
            engine.buyoff_loan(ind, "L-001")
        assert "is not active (status: Paid)" in str(excinfo.value)
    finally:
        db.close()


def test_repair_repository_purges_loan_suspensions_orphans(temp_db_path):
    """Verifies that delete_orphans removes loan_suspensions whose loan_id was deleted."""
    db = DatabaseManager(temp_db_path)
    try:
        ind = db.add_individual("Suspension Member", "0711111111", "s@x.com")
        db.add_loan_record(ind, "L-001", 5000, 5750, 5000, 500, 75, "2026-01-01", "2026-02-01")
        loan = db.get_loan_by_ref(ind, "L-001")

        # Add valid suspension
        db.conn.execute("""
            INSERT INTO loan_suspensions (individual_id, loan_id, loan_ref, start_date)
            VALUES (?, ?, 'L-001', '2026-03-01')
        """, (ind, loan['id']))

        # Plant an orphaned suspension with non-existent loan_id (temporarily disabling FKs to plant)
        if db.conn.in_transaction:
            db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = OFF")
        # Direct raw cursor without starting an implicit transaction
        db.conn.cursor().execute("""
            INSERT INTO loan_suspensions (individual_id, loan_id, loan_ref, start_date)
            VALUES (?, 99999, 'L-OLD', '2026-02-01')
        """, (ind,))
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")

        # Run orphan deletion
        deleted = db.repair.delete_orphans("loan_suspensions")
        assert deleted == 1

        # Check remaining
        remaining = db.conn.execute("SELECT loan_id FROM loan_suspensions").fetchall()
        assert remaining == [(loan['id'],)]
    finally:
        db.close()


def test_open_repair_loans_dialog_guards_unmigrated_schema(monkeypatch):
    """Verifies that open_repair_loans_dialog checks schema version before scanning healable loans."""
    from src.views.dashboard import Dashboard
    from PyQt6.QtWidgets import QApplication

    # Ensure QApplication exists in headless test environment
    app = QApplication.instance() or QApplication([])

    warnings_shown = []
    from PyQt6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings_shown.append((title, text)))

    # Create dummy mock dashboard with schema_version = 1
    class DummyDB:
        class DummyConn:
            pass
        conn = DummyConn()
    
    class DummyDashboard:
        db = DummyDB()
        open_repair_loans_dialog = Dashboard.open_repair_loans_dialog

    d = DummyDashboard()
    # Mock schema_version to return 1 (less than 3)
    monkeypatch.setattr(migrations, "schema_version", lambda conn: 1)

    d.open_repair_loans_dialog()
    assert len(warnings_shown) == 1
    assert "Migration Required" in warnings_shown[0][0]
