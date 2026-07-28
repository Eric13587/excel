"""Orphaned-row discovery and removal, and the import-run audit trail.

Two small surfaces that share a trait: both reach across every child table at
once, so neither belongs to a single-table repository.

**Orphan repair.** Legacy journals were written without foreign-key
enforcement, so deleting a member could leave transactions behind. Those
orphans skew any aggregate that sums a child table directly, and — because
enforcement is only switched on when the journal is already clean — a single
orphan disables it for the whole connection, letting the next deletion orphan
silently too. Migration 001's cascade triggers make new orphans impossible;
this exists for journals written before it.

Table names are interpolated here, which is unavoidable: the set is discovered
from ``sqlite_master`` at runtime precisely so a table added by a future
migration is covered without editing this file. The names come from SQLite's
own catalogue and are quoted before use; no user input reaches them.
"""
import logging

import pandas as pd

from src.db.base import Repository

logger = logging.getLogger(__name__)


def _quote(identifier):
    """Quote an identifier read from sqlite_master for safe interpolation."""
    return '"' + identifier.replace('"', '""') + '"'


class RepairRepository(Repository):
    """Cross-table orphan queries."""

    def child_tables(self):
        """Every table except ``individuals`` that carries an individual_id.

        Discovered rather than listed, so a table added by a later migration is
        covered automatically.
        """
        names = [r[0] for r in self._rows(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        out = []
        for name in names:
            if name == "individuals":
                continue
            cols = [c[1] for c in self._rows(f"PRAGMA table_info({_quote(name)})")]
            if "individual_id" in cols:
                out.append(name)
        return out

    def _has_date_column(self, table):
        return "date" in [c[1] for c in self._rows(f"PRAGMA table_info({_quote(table)})")]

    def orphan_groups(self, table):
        """(individual_id, rows, first_date, last_date) per missing member.

        A NULL individual_id counts as orphaned — no child row is meaningful
        without a member.
        """
        t = _quote(table)
        dates = "MIN(o.date), MAX(o.date)" if self._has_date_column(table) else "NULL, NULL"
        return self._rows(
            f"SELECT o.individual_id, COUNT(*), {dates} FROM {t} o "
            f"LEFT JOIN individuals i ON i.id = o.individual_id "
            f"WHERE i.id IS NULL GROUP BY o.individual_id ORDER BY o.individual_id")

    def orphan_frame(self, table):
        """Every orphaned row of one table, for the audit workbook."""
        t = _quote(table)
        return pd.read_sql_query(
            f"SELECT o.* FROM {t} o LEFT JOIN individuals i ON i.id = o.individual_id "
            f"WHERE i.id IS NULL", self.conn)

    def delete_orphans(self, table):
        """Delete one table's orphans; returns the row count removed."""
        t = _quote(table)
        cur = self._execute(
            f"DELETE FROM {t} WHERE individual_id IS NULL "
            f"OR individual_id NOT IN (SELECT id FROM individuals)")
        return cur.rowcount


class ImportHistoryRepository(Repository):
    """The ``import_history`` table and the undo that keys off it.

    Children carry ``import_id`` only when the import *created* them, so an
    undo never removes a member who already existed and was merged into.
    """

    #: Deleted in dependency order by :meth:`undo`.
    _CHILD_TABLES = ("savings", "ledger", "loans", "individuals")

    def start(self, timestamp, source_file):
        cur = self._execute(
            "INSERT INTO import_history (timestamp, source_file, details, item_count) "
            "VALUES (?, ?, ?, ?)", (timestamp, source_file, "Started", 0))
        return cur.lastrowid

    def finish(self, import_id, details_json, item_count):
        self._execute("UPDATE import_history SET details = ?, item_count = ? WHERE id = ?",
                      (details_json, item_count, import_id))

    def all(self):
        return self._dicts(
            "SELECT id, timestamp, source_file, details, item_count "
            "FROM import_history ORDER BY id DESC")

    def undo(self, import_id):
        """Remove everything one import run created, children first."""
        for table in self._CHILD_TABLES:
            self._execute(f"DELETE FROM {_quote(table)} WHERE import_id = ?", (import_id,))
        self._execute("DELETE FROM import_history WHERE id = ?", (import_id,))
