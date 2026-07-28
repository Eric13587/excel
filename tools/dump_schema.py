"""Dump the schema a freshly-migrated journal actually has.

``schema.sql`` is the hand-written reference: reverse-engineered, commented, and
explaining *why* each constraint exists. It is not, and cannot be, the source of
truth — the source of truth is ``create_tables`` plus ``src/migrations/*``, and
anything maintained alongside them by hand drifts. It drifted: by Phase 8 the
reference was missing four tables and thirty-eight triggers.

This generates the companion that cannot drift, because it is produced from a
real journal built the way the application builds one.

    python -m tools.dump_schema            # write docs/db/schema-current.sql
    python -m tools.dump_schema --check    # exit 1 if the file is out of date

``tests/test_schema_current.py`` runs the check, so a migration that lands
without regenerating this fails the suite.
"""
import argparse
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUTPUT = os.path.join("docs", "db", "schema-current.sql")

HEADER = """\
-- =============================================================================
-- LoanMaster — the schema a new journal is actually created with. GENERATED.
-- =============================================================================
-- Do not edit. Regenerate with:
--
--     python -m tools.dump_schema
--
-- Produced by opening a fresh journal through DatabaseManager, so it reflects
-- create_tables followed by every migration in src/migrations/, in order. That
-- is what a new journal gets and what an old journal converges on.
--
-- schema.sql is the *annotated* reference — it explains why the constraints
-- exist and is worth reading first. This file is the authority on what is
-- there, and tests/test_schema_current.py fails if the two ever disagree with
-- reality.
-- =============================================================================

"""


def current_schema():
    """The full DDL of a newly-created, fully-migrated journal."""
    logging.disable(logging.CRITICAL)
    from src.database import DatabaseManager

    path = os.path.join(tempfile.mkdtemp(), "schema_probe.db")
    db = DatabaseManager(path, auto_backup=False)
    if not db.migration_report.ok:
        raise SystemExit(f"migrations did not apply cleanly: "
                         f"{db.migration_report.summary()}")
    rows = db.conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'view' THEN 1 "
        "                   WHEN 'index' THEN 2 ELSE 3 END, name").fetchall()
    db.close()

    out = [HEADER]
    section = None
    for kind, name, sql in rows:
        if kind != section:
            section = kind
            out.append(f"\n-- ---------- {kind}s ----------\n")
        out.append(sql.strip().rstrip(";") + ";\n")
    return "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the committed file is out of date")
    ap.add_argument("-o", "--output", default=OUTPUT)
    args = ap.parse_args(argv)

    generated = current_schema()
    if args.check:
        try:
            with open(args.output, encoding="utf-8") as fh:
                committed = fh.read()
        except OSError:
            print(f"{args.output} is missing — run: python -m tools.dump_schema",
                  file=sys.stderr)
            return 1
        if committed != generated:
            print(f"{args.output} is out of date — run: python -m tools.dump_schema",
                  file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(generated)
    tables = generated.count("CREATE TABLE")
    triggers = generated.count("CREATE TRIGGER")
    indexes = generated.count("CREATE INDEX") + generated.count("CREATE UNIQUE INDEX")
    print(f"wrote {args.output}: {tables} tables, {indexes} indexes, {triggers} triggers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
