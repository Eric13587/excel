"""Phase 1b — the versioned migration runner and migration 001.

The most important test here is test_migrations_actually_applied: the runner is
deliberately non-fatal, so a broken migration leaves a working application and
a silently unmigrated journal. Without an explicit assertion the whole suite
goes green while none of the integrity work is present.
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import migrations
from src.database import DatabaseManager


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "m.db"))
    yield db
    db.close()


def _objects(conn, kind, prefix):
    return sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type=? AND name LIKE ?", (kind, prefix + "%")))


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #

def test_migrations_actually_applied(journal):
    """Guard against a silently-failed migration leaving a green suite."""
    assert journal.migration_report.ok, journal.migration_report.summary()
    assert journal.migration_report.applied, "no migration was applied to a new journal"
    assert migrations.schema_version(journal.conn) >= 1


def test_migrations_are_recorded_and_not_reapplied(journal):
    first = migrations.applied_versions(journal.conn)
    assert 1 in first

    report = migrations.apply_pending(journal.conn)
    assert report.ok
    assert report.applied == []                       # nothing new to do
    assert (1, "integrity_constraints") in report.already_applied
    assert migrations.applied_versions(journal.conn) == first


def test_reopening_a_journal_is_idempotent():
    path = os.path.join(tempfile.mkdtemp(), "reopen.db")
    a = DatabaseManager(path)
    indexes = _objects(a.conn, "index", "idx_")
    a.close()

    b = DatabaseManager(path)
    try:
        assert b.migration_report.ok
        assert b.migration_report.applied == []       # already at the latest version
        assert _objects(b.conn, "index", "idx_") == indexes
    finally:
        b.close()


def test_failed_migration_rolls_back_and_does_not_record(journal, monkeypatch, tmp_path):
    """A migration that cannot apply must leave no trace and no version row."""
    bad = tmp_path / "999_broken.sql"
    bad.write_text(
        "CREATE INDEX idx_probe_marker ON individuals(name);\n"
        "CREATE INDEX idx_broken ON no_such_table(nope);\n"
    )
    monkeypatch.setattr(migrations, "discover",
                        lambda: [(999, "broken", str(bad))])

    report = migrations.apply_pending(journal.conn)

    assert not report.ok
    assert report.failed[0] == 999
    assert 999 not in migrations.applied_versions(journal.conn)
    # The first statement succeeded but must have been rolled back with the rest.
    assert "idx_probe_marker" not in _objects(journal.conn, "index", "idx_")


def test_failed_migration_leaves_the_journal_usable(journal, monkeypatch, tmp_path):
    bad = tmp_path / "999_broken.sql"
    bad.write_text("CREATE INDEX idx_broken ON no_such_table(nope);\n")
    monkeypatch.setattr(migrations, "discover", lambda: [(999, "broken", str(bad))])

    migrations.apply_pending(journal.conn)
    ind = journal.add_individual("Still Works", "0700", "s@x.com")
    assert journal.get_individual(ind)["name"] == "Still Works"


def test_split_statements_keeps_trigger_bodies_whole():
    script = """
    -- a comment
    CREATE TRIGGER t BEFORE DELETE ON a BEGIN
        DELETE FROM b WHERE a_id = OLD.id;
        DELETE FROM c WHERE a_id = OLD.id;
    END;
    CREATE INDEX i ON b(a_id);
    """
    statements = migrations.split_statements(script)
    assert len(statements) == 2
    assert statements[0].count("DELETE FROM") == 2
    assert statements[0].rstrip().endswith("END;")
    assert statements[1].startswith("CREATE INDEX")


def test_split_statements_ignores_comment_only_fragments():
    assert migrations.split_statements("-- nothing here\n\n-- still nothing\n") == []


# --------------------------------------------------------------------------- #
# Migration 001 content
# --------------------------------------------------------------------------- #

def test_cascade_triggers_exist(journal):
    assert _objects(journal.conn, "trigger", "trg_cascade") == [
        "trg_cascade_individual_delete", "trg_cascade_loan_delete"]


def test_expected_indexes_exist(journal):
    names = set(_objects(journal.conn, "index", "idx_"))
    for expected in ("idx_ledger_individual_date", "idx_ledger_individual_loan",
                     "idx_ledger_date", "idx_ledger_batch", "idx_ledger_import",
                     "idx_loans_individual", "idx_loans_status_due",
                     "idx_savings_individual_date", "idx_christmas_individual_date",
                     "idx_benevolent_individual_date", "idx_suspensions_individual",
                     "idx_journal_entries_date", "idx_individuals_name_nocase",
                     "idx_audit_ts", "idx_loans_member_ref",
                     "idx_journal_entries_source_ref"):
        assert expected in names, f"missing index {expected}"


def test_loan_ref_is_unique_per_member(journal):
    a = journal.add_individual("A", "1", "a@x.com")
    b = journal.add_individual("B", "2", "b@x.com")
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-001', 100)", (a,))
    # Same ref for a different member is fine — refs are per-member.
    cur.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-001', 100)", (b,))
    journal.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("INSERT INTO loans (individual_id, ref, principal) VALUES (?, 'L-001', 200)", (a,))


def test_journal_source_ref_cannot_double_post(journal):
    cur = journal.conn.cursor()
    cur.execute("INSERT INTO journal_entries (entry_date, source, source_ref) "
                "VALUES ('2026-01-01', 'repayment', 'ledger:1')")
    journal.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        cur.execute("INSERT INTO journal_entries (entry_date, source, source_ref) "
                    "VALUES ('2026-01-01', 'repayment', 'ledger:1')")
    journal.conn.rollback()
    # Manual entries carry no source_ref and must still be freely insertable.
    cur.execute("INSERT INTO journal_entries (entry_date, source, source_ref) "
                "VALUES ('2026-01-01', 'manual', NULL)")
    cur.execute("INSERT INTO journal_entries (entry_date, source, source_ref) "
                "VALUES ('2026-01-01', 'manual', NULL)")
    journal.conn.commit()


def test_cascade_does_not_touch_other_members(journal):
    keep = journal.add_individual("Keep", "1", "k@x.com")
    drop = journal.add_individual("Drop", "2", "d@x.com")
    cur = journal.conn.cursor()
    for ind in (keep, drop):
        cur.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
                    "VALUES (?, '2026-01-01', 'Deposit', 10, 10)", (ind,))
    journal.conn.commit()

    journal.delete_individual(drop)

    assert journal.conn.execute(
        "SELECT COUNT(*) FROM savings WHERE individual_id=?", (keep,)).fetchone()[0] == 1
    assert journal.conn.execute(
        "SELECT COUNT(*) FROM individuals WHERE id=?", (keep,)).fetchone()[0] == 1


def test_foreign_keys_enforced_on_a_clean_journal(journal):
    assert journal.fk_enforced
    assert journal.fk_violations == 0
    with pytest.raises(sqlite3.IntegrityError):
        journal.conn.execute(
            "INSERT INTO ledger (individual_id, date, event_type, loan_id) "
            "VALUES (99999, '2026-01-01', 'Repayment', 'L-001')")
