"""Give a loan its own duration and interest rate.

Until now a loan's **duration** existed nowhere but a sentence in
``ledger.notes``::

    "Principal: 100000.0, Total Interest: 15000.0, Duration: 12m"

``LoanEngine._loan_heal_plan`` recovered it with three regular expressions, so a
core contractual term was carried by prose: editing that note changed the
loan's mathematics, and a loan whose note did not parse could not be healed at
all.

This adds ``loans.duration_months`` and ``loans.interest_rate`` and backfills
them from the issue note where it parses. Loans that cannot be backfilled keep
NULL — deliberately, rather than being given a guessed duration. The heal tool
falls back to the note for those, and ``python -m src.integrity`` lists them.

Additive: two nullable columns and an UPDATE. Nothing existing is altered, so
the rollback is ``ALTER TABLE loans DROP COLUMN`` (SQLite 3.35+) or simply
leaving them unused.
"""
import logging
import re

logger = logging.getLogger(__name__)

PRINCIPAL = re.compile(r"Principal:\s*([\d.]+)")
TOTAL_INTEREST = re.compile(r"Total Interest:\s*([\d.]+)")
DURATION = re.compile(r"Duration:\s*(\d+)")


def _add_column(conn, table, column, decl):
    """Add a column unless it is already there (the migration is re-runnable)."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply(conn):
    _add_column(conn, "loans", "duration_months", "INTEGER")
    _add_column(conn, "loans", "interest_rate", "REAL")

    rows = conn.execute(
        "SELECT l.id, l.individual_id, l.ref, ("
        "  SELECT g.notes FROM ledger g"
        "  WHERE g.individual_id = l.individual_id AND g.loan_id = l.ref"
        "    AND g.event_type = 'Loan Issued'"
        "  ORDER BY g.date, g.id LIMIT 1) AS issue_note "
        "FROM loans l").fetchall()

    updated = skipped = 0
    for loan_id, member_id, ref, note in rows:
        note = note or ""
        m_principal = PRINCIPAL.search(note)
        m_interest = TOTAL_INTEREST.search(note)
        m_duration = DURATION.search(note)
        if not (m_principal and m_interest and m_duration):
            skipped += 1
            continue

        duration = int(m_duration.group(1))
        principal = float(m_principal.group(1))
        interest = float(m_interest.group(1))
        if duration <= 0 or principal <= 0:
            skipped += 1
            continue

        conn.execute("UPDATE loans SET duration_months=?, interest_rate=? WHERE id=?",
                     (duration, round(interest / principal, 6), loan_id))
        updated += 1

    logger.info("003_loan_terms: backfilled %d loan(s), %d without a parseable "
                "issue note", updated, skipped)
    return updated
