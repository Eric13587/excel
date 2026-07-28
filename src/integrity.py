"""Journal integrity verification.

Answers one question: *can this journal be trusted right now?* — structurally,
referentially, and as a set of books.

Design principle: **assert, don't enforce.** Several invariants this reports on
cannot yet be database constraints, either because tightening them needs a table
rebuild (Phase 4) or because fixing a break is a business decision, not a
schema change. Reporting them makes the gap visible and measurable instead of
silent, and produces the evidence needed before the constraints are enforced.

Usage:
    python -m src.integrity path/to/journal.db [--strict] [--quiet]

Exit status: 0 when no failures (``--strict`` also fails on warnings), 1
otherwise. Suitable for a scheduled job.
"""
import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

PASS, WARN, FAIL = "pass", "warn", "fail"

# Money is stored as floats, so equality is only meaningful to the cent.
TOLERANCE = 0.005

# Chart-of-accounts codes whose GL balance must agree with a subledger.
GL_LOANS_RECEIVABLE = "1100"
GL_MEMBER_DEPOSITS = "2000"
GL_CHRISTMAS_SAVINGS = "2010"
GL_BENEVOLENT_FUND = "2300"

# The vocabularies Phase 4 will enforce as CHECK constraints.
LEDGER_EVENT_TYPES = ("Loan Issued", "Loan Top-Up", "Interest Earned",
                      "Repayment", "Loan Buyoff", "Loan Restructure")
LOAN_STATUSES = ("Active", "Paid")
SAVINGS_TYPES = ("Deposit", "Withdrawal", "Interest")


@dataclass
class Check:
    name: str
    category: str
    status: str
    detail: str
    count: int = 0


@dataclass
class IntegrityReport:
    journal: str
    checks: list = field(default_factory=list)

    def add(self, name, category, status, detail, count=0):
        self.checks.append(Check(name, category, status, detail, count))

    @property
    def failures(self):
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self):
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self):
        return not self.failures

    def format_text(self):
        symbol = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
        lines = [f"Integrity report — {self.journal}", "=" * 78]
        current = None
        for c in self.checks:
            if c.category != current:
                current = c.category
                lines.append(f"\n[{current}]")
            lines.append(f"  [{symbol[c.status]}] {c.name:<44} {c.detail}")
        lines.append("")
        lines.append("-" * 78)
        lines.append(f"{len(self.checks)} checks — "
                     f"{len(self.failures)} failed, {len(self.warnings)} warnings")
        return "\n".join(lines)


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row and row[0] is not None else 0


def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


# --------------------------------------------------------------------------- #
# Structural
# --------------------------------------------------------------------------- #

def _check_structural(conn, report, quick):
    pragma = "quick_check" if quick else "integrity_check"
    try:
        rows = [r[0] for r in conn.execute(f"PRAGMA {pragma}")]
    except sqlite3.DatabaseError as e:
        report.add(pragma, "structural", FAIL, f"could not run: {e}")
        return
    if rows == ["ok"]:
        report.add(pragma, "structural", PASS, "database file is well-formed")
    else:
        report.add(pragma, "structural", FAIL, "; ".join(rows[:5]), len(rows))

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        tables = sorted({v[0] for v in violations})
        report.add("foreign_key_check", "structural", FAIL,
                   f"{len(violations)} violation(s) in {', '.join(tables)}", len(violations))
    else:
        report.add("foreign_key_check", "structural", PASS, "no violations")

    # Whether enforcement is *possible*, which is a property of the data. The
    # PRAGMA itself is per-connection, so reading it from this tool's own
    # read-only connection would always say OFF and mean nothing; the live
    # setting is reported separately in verify() when a DatabaseManager is
    # supplied.
    report.add("foreign keys enforceable", "structural", PASS if not violations else FAIL,
               "yes" if not violations else "no — repair the orphaned rows first")


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

def _check_schema(conn, report):
    from src import migrations
    version = migrations.schema_version(conn)
    latest = max((v for v, _, _ in migrations.discover()), default=0)
    if version >= latest:
        report.add("schema version", "schema", PASS, f"{version} (latest)")
    else:
        report.add("schema version", "schema", WARN,
                   f"{version}, latest is {latest} — migrations pending")

    for kind, name in (("index", "idx_loans_member_ref"),
                       ("index", "idx_journal_entries_source_ref"),
                       ("index", "idx_ledger_individual_date")):
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                               (kind, name)).fetchone() is not None
        report.add(f"{kind} {name}", "schema", PASS if present else WARN,
                   "present" if present else "missing — migration 001 not applied")


# --------------------------------------------------------------------------- #
# Referential
# --------------------------------------------------------------------------- #

_CHILD_TABLES = ("ledger", "loans", "savings", "christmas_savings",
                 "benevolent_ledger", "benevolent_accounts", "loan_suspensions")


def _check_referential(conn, report):
    total = 0
    detail = []
    for table in _CHILD_TABLES:
        if not _table_exists(conn, table):
            continue
        n = _scalar(conn, f"SELECT COUNT(*) FROM {table} WHERE individual_id IS NULL "
                          f"OR individual_id NOT IN (SELECT id FROM individuals)")
        if n:
            total += n
            detail.append(f"{table}={n}")
    report.add("orphaned child rows", "referential", PASS if not total else FAIL,
               "none" if not total else ", ".join(detail), total)

    n = _scalar(conn, "SELECT COUNT(*) FROM ledger l WHERE NOT EXISTS "
                      "(SELECT 1 FROM loans o WHERE o.individual_id=l.individual_id "
                      "AND o.ref=l.loan_id)")
    report.add("ledger rows resolving to a loan", "referential", PASS if not n else WARN,
               "all resolve" if not n else f"{n} row(s) reference a missing loan", n)

    n = _scalar(conn, "SELECT COUNT(*) FROM (SELECT individual_id, ref FROM loans "
                      "GROUP BY 1,2 HAVING COUNT(*)>1)")
    report.add("loan ref unique per member", "referential", PASS if not n else FAIL,
               "unique" if not n else f"{n} duplicated (member, ref) pair(s)", n)


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #

def _check_accounting(conn, report):
    if not _table_exists(conn, "journal_lines"):
        return

    n = _scalar(conn, "SELECT COUNT(*) FROM (SELECT entry_id FROM journal_lines "
                      "GROUP BY 1 HAVING ABS(SUM(debit)-SUM(credit)) > ?)", (TOLERANCE,))
    report.add("every journal entry balances", "accounting", PASS if not n else FAIL,
               "all balanced" if not n else f"{n} unbalanced entry/entries", n)

    net = _scalar(conn, "SELECT COALESCE(SUM(debit)-SUM(credit),0) FROM journal_lines")
    report.add("trial balance nets to zero", "accounting",
               PASS if abs(net) <= TOLERANCE else FAIL, f"debits - credits = {net:,.2f}")

    n = _scalar(conn, "SELECT COUNT(*) FROM (SELECT source, source_ref FROM journal_entries "
                      "WHERE source_ref IS NOT NULL GROUP BY 1,2 HAVING COUNT(*)>1)")
    report.add("no subledger row posted twice", "accounting", PASS if not n else FAIL,
               "idempotent" if not n else f"{n} duplicated (source, source_ref)", n)

    # --- control accounts: the GL must agree with the subledger it summarises ---
    _control(conn, report, "Loans Receivable", GL_LOANS_RECEIVABLE, "debit",
             "SELECT COALESCE(SUM(balance),0) FROM loans", "loans.balance")
    _control(conn, report, "Member Deposits", GL_MEMBER_DEPOSITS, "credit",
             "SELECT COALESCE(SUM(CASE transaction_type WHEN 'Withdrawal' "
             "THEN -amount ELSE amount END),0) FROM savings", "savings")
    if _table_exists(conn, "christmas_savings"):
        _control(conn, report, "Christmas Savings", GL_CHRISTMAS_SAVINGS, "credit",
                 "SELECT COALESCE(SUM(CASE transaction_type WHEN 'Withdrawal' "
                 "THEN -amount ELSE amount END),0) FROM christmas_savings", "christmas_savings")
    if _table_exists(conn, "benevolent_ledger"):
        _control(conn, report, "Benevolent Fund", GL_BENEVOLENT_FUND, "credit",
                 "SELECT COALESCE(SUM(CASE transaction_type WHEN 'Withdrawal' "
                 "THEN -amount ELSE amount END),0) FROM benevolent_ledger", "benevolent_ledger")

    # --- cached loan balances vs the ledger they are derived from ---
    drifted = conn.execute(
        """SELECT l.individual_id, l.ref, l.balance, COALESCE((
               SELECT SUM(CASE WHEN g.event_type IN ('Loan Issued','Loan Top-Up') THEN g.added
                               WHEN g.principal_portion + g.interest_portion <= 0 THEN -g.deducted
                               ELSE -g.principal_portion END)
               FROM ledger g
               WHERE g.individual_id = l.individual_id AND g.loan_id = l.ref), 0) AS derived
           FROM loans l
           WHERE l.status = 'Active'"""
    ).fetchall()
    off = [(i, r, bal, der) for (i, r, bal, der) in drifted if abs((bal or 0) - der) > 0.5]
    if off:
        worst = max(off, key=lambda row: abs(row[2] - row[3]))
        total = sum(abs(row[2] - row[3]) for row in off)
        report.add("loans.balance matches its ledger", "accounting", WARN,
                   f"{len(off)} loan(s) drifted, total {total:,.2f}; "
                   f"worst member {worst[0]} {worst[1]} "
                   f"cached {worst[2]:,.2f} vs ledger {worst[3]:,.2f}", len(off))
    else:
        report.add("loans.balance matches its ledger", "accounting", PASS, "no drift")

    n = _scalar(conn, "SELECT COUNT(*) FROM loans WHERE status='Paid' AND ABS(balance) > 0.5")
    report.add("settled loans carry a zero balance", "accounting", PASS if not n else WARN,
               "all zero" if not n else f"{n} 'Paid' loan(s) with a non-zero balance", n)

    # Migration 003 gave loans their own duration and rate; before that a
    # duration existed only inside the issue note's prose.
    if "duration_months" in {r[1] for r in conn.execute("PRAGMA table_info(loans)")}:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM loans "
            "WHERE duration_months IS NULL OR duration_months <= 0 GROUP BY status").fetchall()
        n = sum(count for _, count in rows)
        active = sum(count for status, count in rows if status == 'Active')
        report.add("loans record their own duration", "accounting",
                   PASS if not n else WARN,
                   "all recorded" if not n else
                   f"{n} loan(s) without a duration ({active} still Active) — "
                   f"their issue note did not parse", n)


def _check_projection(conn, report):
    """The GL projection's own health, independent of whether it reconciles.

    Since migration 005 the projection is maintained incrementally from a change
    queue rather than rebuilt wholesale, so two things are worth stating
    explicitly: whether the queue has been drained, and whether any projected
    entry has outlived the subledger row it was derived from. The opposite kind
    of drift — a subledger row with no journal — is what the control-account
    comparisons above already measure.
    """
    if not _table_exists(conn, "gl_outbox"):
        return
    cat = "projection"

    established = _scalar(conn, "SELECT COUNT(*) FROM settings "
                                "WHERE key='gl_projection_established' AND value='1'")
    report.add("projection baseline established", cat, PASS if established else WARN,
               "established" if established else
               "not yet — the next sync will rebuild in full")

    depth = _scalar(conn, "SELECT COUNT(*) FROM gl_outbox")
    report.add("projection change queue drained", cat, PASS if not depth else WARN,
               "empty" if not depth else
               f"{depth} change(s) not yet posted — run Treasury → Sync", depth)

    stale = conn.execute(
        "SELECT COUNT(*) FROM journal_entries e WHERE ("
        "  (e.source_ref LIKE 'ledger:%' AND NOT EXISTS (SELECT 1 FROM ledger l"
        "     WHERE l.id = CAST(substr(e.source_ref, 8) AS INTEGER)))"
        "  OR (e.source_ref LIKE 'savings:%' AND NOT EXISTS (SELECT 1 FROM savings s"
        "     WHERE s.id = CAST(substr(e.source_ref, 9) AS INTEGER)))"
        "  OR (e.source_ref LIKE 'christmas:%' AND NOT EXISTS (SELECT 1 FROM christmas_savings c"
        "     WHERE c.id = CAST(substr(e.source_ref, 11) AS INTEGER)))"
        "  OR (e.source_ref LIKE 'benevolent:%' AND NOT EXISTS (SELECT 1 FROM benevolent_ledger b"
        "     WHERE b.id = CAST(substr(e.source_ref, 12) AS INTEGER)))"
        ") AND e.source IN ('loan_disbursement','repayment','interest','savings',"
        "                   'christmas','benevolent')").fetchone()[0]
    report.add("projected entries have a live source row", cat, PASS if not stale else WARN,
               "all live" if not stale else
               f"{stale} entry/entries outlived their subledger row", stale)


def _check_periods(conn, report):
    """The fiscal-period register, once migration 008 has created it.

    Nothing here is a failure. An empty register is the normal state for a
    journal that has not started closing periods, and is exactly how 008 ships.
    """
    if not _table_exists(conn, "fiscal_periods"):
        return
    cat = "fiscal periods"

    rows = conn.execute(
        "SELECT status, COUNT(*) FROM fiscal_periods GROUP BY status").fetchall()
    counts = dict(rows)
    total = sum(counts.values())
    report.add("periods declared", cat, PASS,
               "none — nothing is frozen" if not total else
               f"{total} ({counts.get('closed', 0)} closed, {counts.get('open', 0)} open)",
               total)

    settled = _scalar(conn, "SELECT MAX(end_date) FROM fiscal_periods WHERE status='closed'")
    if settled:
        report.add("books settled through", cat, PASS, str(settled))

        # Activity dated inside a closed period that was written after the
        # close. Migration 009 makes this impossible; until then it is the
        # measurement that says whether turning the lock on is safe.
        n = _scalar(conn,
            "SELECT COUNT(*) FROM audit_log a JOIN fiscal_periods p "
            "  ON p.status='closed' "
            "WHERE a.entity IN ('ledger','savings','christmas_savings','benevolent_ledger') "
            "  AND a.ts > p.closed_at "
            "  AND json_extract(a.before_image, '$.date') BETWEEN p.start_date AND p.end_date")
        report.add("no edits inside a closed period", cat, PASS if not n else WARN,
                   "none recorded" if not n else
                   f"{n} money row(s) changed after their period was closed", n)


def _control(conn, report, label, code, side, subledger_sql, subledger_name):
    """Compare a GL control account against the subledger it summarises."""
    expr = ("SUM(debit)-SUM(credit)") if side == "debit" else ("SUM(credit)-SUM(debit)")
    gl = _scalar(conn, f"SELECT COALESCE({expr},0) FROM journal_lines WHERE account_code=?", (code,))
    sub = _scalar(conn, subledger_sql)
    delta = gl - sub
    report.add(f"control {code} {label}", "accounting",
               PASS if abs(delta) <= TOLERANCE else WARN,
               f"GL {gl:,.2f} vs {subledger_name} {sub:,.2f} — delta {delta:,.2f}")


# --------------------------------------------------------------------------- #
# Phase 4 readiness
# --------------------------------------------------------------------------- #

def _check_phase4_readiness(conn, report):
    """Rows that would be rejected by the constraints Phase 4 will enforce.

    These are warnings, not failures: the constraints are not in force yet.
    Clearing this section is the entry criterion for migration 004
    (docs/db/proposed/004_strict_schema.sql).
    """
    cat = "phase 4 readiness"
    probes = [
        ("ledger.event_type in vocabulary",
         "SELECT COUNT(*) FROM ledger WHERE event_type IS NULL OR event_type NOT IN "
         f"({','.join('?' * len(LEDGER_EVENT_TYPES))})", LEDGER_EVENT_TYPES),
        ("loans.status in vocabulary",
         "SELECT COUNT(*) FROM loans WHERE status IS NULL OR status NOT IN "
         f"({','.join('?' * len(LOAN_STATUSES))})", LOAN_STATUSES),
        ("savings.transaction_type in vocabulary",
         "SELECT COUNT(*) FROM savings WHERE transaction_type IS NULL OR transaction_type NOT IN "
         f"({','.join('?' * len(SAVINGS_TYPES))})", SAVINGS_TYPES),
        ("loans.start_date present",
         "SELECT COUNT(*) FROM loans WHERE start_date IS NULL OR start_date = ''", ()),
        ("ledger.loan_id present",
         "SELECT COUNT(*) FROM ledger WHERE loan_id IS NULL OR loan_id = ''", ()),
        # Not a Phase 4 blocker: the column deliberately carries a replay
        # anchor above 1. Reported so the split into is_edited + edited_amount
        # (migration 005) can be sized.
        ("ledger.is_edited carries an anchor amount",
         "SELECT COUNT(*) FROM ledger WHERE is_edited NOT IN (0, 1)", ()),
        ("ledger rows are one-sided",
         "SELECT COUNT(*) FROM ledger WHERE added <> 0 AND deducted <> 0", ()),
        ("no negative ledger amounts",
         "SELECT COUNT(*) FROM ledger WHERE added < 0 OR deducted < 0", ()),
        ("no negative savings amounts",
         "SELECT COUNT(*) FROM savings WHERE amount < 0", ()),
        ("dates are YYYY-MM-DD",
         "SELECT COUNT(*) FROM ledger WHERE date NOT GLOB "
         "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'", ()),
    ]
    for name, sql, params in probes:
        try:
            n = _scalar(conn, sql, params)
        except sqlite3.DatabaseError as e:
            report.add(name, cat, WARN, f"could not evaluate: {e}")
            continue
        report.add(name, cat, PASS if not n else WARN,
                   "clean" if not n else f"{n} row(s) would be rejected", n)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #

def verify(target, quick=False):
    """Run every check against a journal.

    ``target`` may be a path, a sqlite3.Connection, or a DatabaseManager.
    """
    conn = getattr(target, "conn", target)
    name = getattr(target, "db_name", None)
    if isinstance(conn, str):
        name, conn = conn, sqlite3.connect(conn)
    report = IntegrityReport(journal=name or "<connection>")

    _check_structural(conn, report, quick)
    if hasattr(target, "fk_enforced"):
        report.add("foreign keys enforced on this connection", "structural",
                   PASS if target.fk_enforced else WARN,
                   "ON" if target.fk_enforced else "OFF")
    _check_schema(conn, report)
    _check_referential(conn, report)
    _check_accounting(conn, report)
    _check_projection(conn, report)
    _check_periods(conn, report)
    _check_phase4_readiness(conn, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m src.integrity",
        description="Verify a LoanMaster journal's structural, referential and accounting integrity.")
    parser.add_argument("journal", help="path to the .db file")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on warnings as well as failures")
    parser.add_argument("--quick", action="store_true",
                        help="use PRAGMA quick_check instead of the full integrity_check")
    parser.add_argument("--quiet", action="store_true", help="print only the summary line")
    args = parser.parse_args(argv)

    try:
        conn = sqlite3.connect(f"file:{args.journal}?mode=ro", uri=True)
    except sqlite3.DatabaseError as e:
        print(f"Cannot open {args.journal}: {e}", file=sys.stderr)
        return 1

    try:
        report = verify(conn, quick=args.quick)
        report.journal = args.journal
    finally:
        conn.close()

    if args.quiet:
        print(f"{args.journal}: {len(report.failures)} failed, {len(report.warnings)} warnings")
    else:
        print(report.format_text())

    if report.failures:
        return 1
    return 1 if (args.strict and report.warnings) else 0


if __name__ == "__main__":
    sys.exit(main())
