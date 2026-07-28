"""Read-only access to an **external** journal being imported from.

Every read of a foreign database goes through here, which matters for more than
tidiness: the file is chosen by the user and is otherwise untrusted input.
Previously the import path interpolated table names taken from the source's own
``sqlite_master`` into DELETE and INSERT statements. This class reads only from
a fixed allowlist (:data:`IMPORTABLE_TABLES`), so a crafted source file cannot
steer a statement at a table the importer never intended to touch.

The connection is opened read-only via a URI, so nothing here can modify the
source journal even by accident.
"""
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)

#: The only tables the importer will read from a foreign journal.
IMPORTABLE_TABLES = frozenset({
    "individuals", "loans", "ledger", "savings",
    "christmas_savings", "benevolent_ledger", "benevolent_accounts",
    "loan_suspensions",
})


class SourceJournalError(Exception):
    """The source file is not a usable journal."""


class SourceJournal:
    """A foreign journal, opened read-only for the duration of an import."""

    def __init__(self, path):
        self.path = path
        try:
            # mode=ro fails outright if the file is missing, and guarantees the
            # source is never written to.
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.DatabaseError as e:
            raise SourceJournalError(str(e)) from e
        self.conn.row_factory = sqlite3.Row

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            logger.debug("Closing source journal failed", exc_info=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate(path):
        """(is_valid, message) for a candidate source file.

        Checks the file exists, is non-empty, is really a SQLite database, and
        carries an ``individuals`` table with a ``name`` column — the minimum
        the importer needs to identify anybody.
        """
        if not os.path.exists(path):
            return False, "File does not exist."
        if os.path.getsize(path) == 0:
            return False, "File is empty."
        conn = None
        try:
            conn = sqlite3.connect(path)
            cur = conn.cursor()
            try:
                cur.execute("SELECT name FROM sqlite_master LIMIT 1")
            except sqlite3.DatabaseError:
                return False, "File is not a valid SQLite database or is encrypted."
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='individuals'")
            if not cur.fetchone():
                return False, "Missing required table: 'individuals'."
            columns = [r[1] for r in cur.execute("PRAGMA table_info(individuals)")]
            if "name" not in columns:
                return False, "Table 'individuals' missing required column: 'name'."
            return True, ""
        except Exception as e:                      # noqa: BLE001 — reported to the user
            return False, f"Validation Error: {str(e)}"
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def _table(self, name):
        if name not in IMPORTABLE_TABLES:
            raise ValueError(f"{name!r} is not an importable table")
        return name

    def has_table(self, name):
        """True if the source carries an importable table of this name."""
        if name not in IMPORTABLE_TABLES:
            return False
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def columns(self, table):
        return [r[1] for r in self.conn.execute(f"PRAGMA table_info({self._table(table)})")]

    def count(self, table, member_id=None):
        """Row count, optionally for one member. 0 when the table is absent."""
        if not self.has_table(table):
            return 0
        t = self._table(table)
        if member_id is None:
            return self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {t} WHERE individual_id=?", (member_id,)).fetchone()[0]

    # ------------------------------------------------------------------ #
    # Members
    # ------------------------------------------------------------------ #

    def member_summaries(self, member_ids=None):
        """(id, name, phone, email) dicts — for preview and conflict scanning."""
        if member_ids is None:
            rows = self.conn.execute(
                "SELECT id, name, phone, email FROM individuals ORDER BY name").fetchall()
        else:
            if not member_ids:
                return []
            placeholders = ",".join("?" * len(member_ids))
            rows = self.conn.execute(
                f"SELECT id, name, phone, email FROM individuals WHERE id IN ({placeholders})",
                list(member_ids)).fetchall()
        return [dict(r) for r in rows]

    def member_names(self, member_ids):
        """(id, name) pairs for the selected members."""
        if not member_ids:
            return []
        placeholders = ",".join("?" * len(member_ids))
        return [(r["id"], r["name"]) for r in self.conn.execute(
            f"SELECT id, name FROM individuals WHERE id IN ({placeholders})", list(member_ids))]

    def members(self, member_ids):
        """Full member rows for the selected ids, as sqlite3.Row (keys() works)."""
        if not member_ids:
            return []
        placeholders = ",".join("?" * len(member_ids))
        return self.conn.execute(
            f"SELECT * FROM individuals WHERE id IN ({placeholders})", list(member_ids)).fetchall()

    def basic_members(self):
        """(name, phone, email, default_deduction) for the shell-only import."""
        return self.conn.execute(
            "SELECT name, phone, email, default_deduction FROM individuals").fetchall()

    # ------------------------------------------------------------------ #
    # Loans, ledger, savings, funds
    # ------------------------------------------------------------------ #

    def loan_refs(self, member_id):
        return [r["ref"] for r in self.conn.execute(
            "SELECT ref FROM loans WHERE individual_id=?", (member_id,))]

    def min_ledger_date(self, member_id, loan_ref):
        """Earliest *well-formed* ledger date for one loan.

        The fallback when a loan in the source carries no start date. Restricted
        to ISO-shaped dates: a plain ``MIN()`` over a column mixing
        ``2026-01-01`` and ``01/02/2026`` returns the malformed one, because the
        comparison is lexicographic.
        """
        if not self.has_table("ledger"):
            return None
        row = self.conn.execute(
            "SELECT MIN(date) FROM ledger WHERE individual_id=? AND loan_id=? "
            "AND date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'",
            (member_id, loan_ref)).fetchone()
        return row[0] if row else None

    def loans(self, member_ids):
        if not member_ids:
            return []
        placeholders = ",".join("?" * len(member_ids))
        return self.conn.execute(
            f"SELECT * FROM loans WHERE individual_id IN ({placeholders})",
            list(member_ids)).fetchall()

    def ledger(self, member_ids, date_range=None):
        """Ledger rows for the selected members, optionally date-limited."""
        if not member_ids:
            return [], []
        placeholders = ",".join("?" * len(member_ids))
        sql = f"SELECT * FROM ledger WHERE individual_id IN ({placeholders})"
        params = list(member_ids)
        if date_range:
            sql += " AND date >= ? AND date <= ?"
            params.extend(date_range)
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()

    def savings(self, date_range=None):
        """All savings rows, optionally date-limited. Filtered by member later."""
        sql = "SELECT * FROM savings"
        params = []
        if date_range:
            sql += " WHERE date >= ? AND date <= ?"
            params = list(date_range)
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()

    def all_rows(self, table):
        """Every row of an importable table, as (columns, rows)."""
        t = self._table(table)
        cur = self.conn.execute(f"SELECT * FROM {t}")
        return [d[0] for d in cur.description], cur.fetchall()
