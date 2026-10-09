"""The two ways a journal comes into existence must agree.

A journal is built either by applying ``src/migrations/schema_bootstrap.sql``
directly (new journals) or by creating the legacy table shapes and running every
migration over them (existing journals, and how the bootstrap file itself is
generated). If those diverge, a new journal quietly gets a different database
from a migrated one — the kind of bug that surfaces months later as "it works on
my machine but not on the SACCO's".

So this asserts the equality outright, rather than trusting that the generated
file was regenerated when it should have been.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import migrations
from src.database import DatabaseManager
from tools import dump_schema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _schema_of(db):
    """Every object in the journal, normalised for comparison."""
    return sorted(
        (kind, name, " ".join(sql.split()))
        for kind, name, sql in db.conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))


def _journal(mode):
    return DatabaseManager(os.path.join(tempfile.mkdtemp(), f"{mode}.db"),
                           auto_backup=False, bootstrap=mode)


def test_a_new_journal_matches_a_migrated_one_exactly():
    """The safety argument for the fast path, stated as a test."""
    fast, legacy = _journal("auto"), _journal("legacy")
    try:
        assert _schema_of(fast) == _schema_of(legacy)
    finally:
        fast.close()
        legacy.close()


def test_both_paths_report_the_same_schema_version():
    fast, legacy = _journal("auto"), _journal("legacy")
    try:
        assert fast.migration_report.ok and legacy.migration_report.ok
        assert migrations.schema_version(fast.conn) == migrations.schema_version(legacy.conn)
        assert migrations.schema_version(fast.conn) == max(
            v for v, _, _ in migrations.discover())
    finally:
        fast.close()
        legacy.close()


def test_a_new_journal_has_the_same_reference_data_as_a_migrated_one():
    """The schema equality test does not cover this, and the gap bit once:
    schema_bootstrap.sql is dumped from sqlite_master so it is DDL only, and a
    journal took the fast path with migration 002's default settings recorded
    as applied but never inserted. Right shape, wrong contents."""
    fast, legacy = _journal("auto"), _journal("legacy")
    try:
        for table, columns in (("settings", "key, value"),):
            rows = [sorted(db.conn.execute(
                f"SELECT {columns} FROM {table}").fetchall()) for db in (fast, legacy)]
            assert rows[0] == rows[1], f"{table} differs between the two paths"
            assert rows[0], f"{table} is empty in both — the seed never ran"
    finally:
        fast.close()
        legacy.close()


def test_a_new_journal_is_seeded_and_usable():
    """Settings the application cannot run without, and a usable member."""
    db = _journal("auto")
    try:
        assert db.settings.fy_start_month
        ind = db.add_individual("New", "07", "n@x")
        db.add_savings_transaction(ind, "2026-01-01", "Deposit", 100, "")
        assert db.get_savings_balance(ind) == 100
    finally:
        db.close()


def test_reopening_a_bootstrapped_journal_runs_no_migrations():
    """They are recorded as applied, so the second open is a no-op."""
    path = os.path.join(tempfile.mkdtemp(), "reopen.db")
    first = DatabaseManager(path, auto_backup=False)
    first.close()
    again = DatabaseManager(path, auto_backup=False)
    try:
        assert again.migration_report.applied == []
        assert again.migration_report.ok
    finally:
        again.close()


def test_the_generated_schema_is_committed_and_current():
    path = os.path.join(ROOT, dump_schema.OUTPUT)
    assert os.path.exists(path), "run: python -m tools.dump_schema"
    with open(path, encoding="utf-8") as fh:
        committed = fh.read()
    assert committed == dump_schema.current_schema(), (
        f"{dump_schema.OUTPUT} is stale — run: python -m tools.dump_schema")


def test_the_general_ledger_is_gone():
    """Migration 011 withdrew it. The tables are created by the legacy
    bootstrap so migrations 001 and 004 still run unmodified, then dropped —
    so a finished journal must hold none of them."""
    schema = dump_schema.current_schema()
    for table in ("chart_of_accounts", "journal_entries", "journal_lines",
                  "general_ledger", "gl_outbox", "fiscal_periods",
                  "account_snapshots"):
        assert f"CREATE TABLE {table}" not in schema, f"{table} survived migration 011"
    for prefix in ("trg_outbox_", "trg_journal_", "trg_period_lock_",
                   "trg_fiscal_periods_"):
        assert prefix not in schema, f"{prefix}* triggers survived migration 011"


def test_every_table_the_migrations_create_is_present():
    schema = dump_schema.current_schema()
    for table in ("individuals", "loans", "ledger", "savings", "christmas_savings",
                  "benevolent_ledger", "benevolent_accounts", "loan_suspensions",
                  "audit_log", "settings", "import_history",
                  "schema_migrations"):
        assert f"CREATE TABLE {table}" in schema or f'CREATE TABLE "{table}"' in schema, \
            f"{table} is missing from the generated schema"


def test_the_trigger_families_are_all_installed():
    """Each phase's guarantee is a family of triggers; losing one silently is
    exactly the failure mode a table rebuild causes."""
    schema = dump_schema.current_schema()
    for prefix, expected, what in (
            ("trg_audit_", 13, "audit trail (007)"),):
        found = schema.count(f"CREATE TRIGGER {prefix}")
        assert found == expected, f"{what}: expected {expected} triggers, found {found}"


def test_a_corrupt_bootstrap_file_falls_back_instead_of_failing(monkeypatch, tmp_path):
    """It is an optimisation. Being unable to read it must not stop a journal
    opening — that would turn a missing data file into a dead application."""
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "schema_bootstrap.sql").write_text("this is not valid SQL;")
    for _v, _n, path in migrations.discover():
        (bad / os.path.basename(path)).write_bytes(open(path, "rb").read())
    monkeypatch.setattr(migrations, "migrations_dir", lambda: str(bad))

    db = DatabaseManager(str(tmp_path / "fallback.db"), auto_backup=False)
    try:
        assert db.migration_report.ok
        assert migrations.schema_version(db.conn) == max(
            v for v, _, _ in migrations.discover())
        assert db.settings.fy_start_month
    finally:
        db.close()
