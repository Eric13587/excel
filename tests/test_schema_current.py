"""The generated schema reference must match what a new journal actually gets.

schema.sql is hand-written and drifted: by Phase 8 it was missing four tables
and thirty-eight triggers. Anything maintained by hand alongside the migrations
will drift, so the fix is not "remember to update it" — it is a test that fails
when someone doesn't.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import dump_schema

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_generated_schema_is_committed_and_current():
    path = os.path.join(ROOT, dump_schema.OUTPUT)
    assert os.path.exists(path), "run: python -m tools.dump_schema"
    with open(path, encoding="utf-8") as fh:
        committed = fh.read()
    assert committed == dump_schema.current_schema(), (
        "docs/db/schema-current.sql is stale — run: python -m tools.dump_schema")


def test_every_table_the_migrations_create_is_present():
    """A guard with teeth: naming the tables means a dropped one is noticed."""
    schema = dump_schema.current_schema()
    for table in ("individuals", "loans", "ledger", "savings", "christmas_savings",
                  "benevolent_ledger", "benevolent_accounts", "loan_suspensions",
                  "chart_of_accounts", "journal_entries", "journal_lines",
                  "general_ledger", "audit_log", "settings", "import_history",
                  "schema_migrations", "gl_outbox", "fiscal_periods",
                  "account_snapshots"):
        assert f"CREATE TABLE {table}" in schema or f'CREATE TABLE "{table}"' in schema, \
            f"{table} is missing from the generated schema"


def test_the_trigger_families_are_all_installed():
    """Each phase's guarantee is a family of triggers; losing one silently is
    exactly the failure mode a table rebuild causes."""
    schema = dump_schema.current_schema()
    for prefix, expected, what in (
            ("trg_outbox_", 12, "GL change queue (005)"),
            ("trg_audit_", 13, "audit trail (007)"),
            ("trg_journal_", 4, "journal immutability (006)"),
            ("trg_period_lock_", 13, "fiscal-period lock (009)"),
            ("trg_fiscal_periods_", 3, "period register integrity (008)")):
        found = schema.count(f"CREATE TRIGGER {prefix}")
        assert found == expected, f"{what}: expected {expected} triggers, found {found}"
