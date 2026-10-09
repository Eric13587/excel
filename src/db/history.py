"""Operational tables: import runs and the audit trail.

Two small surfaces that do not belong to a single-table repository: import runs
and the audit trail describe activity rather than a domain entity.
"""
import logging

from src.db.base import Repository

logger = logging.getLogger(__name__)


def _quote(identifier):
    """Quote an identifier read from sqlite_master for safe interpolation."""
    return '"' + identifier.replace('"', '""') + '"'


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


class AuditRepository(Repository):
    """The trigger-maintained ``audit_log``.

    Writes come from database triggers, not from here, so that every change is
    captured regardless of the code path taken. This is the read side only.
    """

    # Entities whose rows carry money. Migration 007 records a before-image for
    # every edit and deletion of these; 'individual' and 'loan' are logged for
    # existence only, as they were before.
    MONEY_ENTITIES = ("ledger", "savings", "christmas_savings", "benevolent_ledger")

    def recent(self, limit=500, entity=None, entity_id=None):
        """Audit entries, most recent first, optionally for one entity."""
        sql = ("SELECT ts, operation, entity, entity_id, summary, before_image "
               "FROM audit_log")
        params = []
        if entity is not None and entity_id is not None:
            sql += " WHERE entity=? AND entity_id=?"
            params = [entity, entity_id]
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._dicts(sql, tuple(params))

    def money_changes(self, limit=500, since=None):
        """Edits and deletions of rows that carry money, most recent first.

        The question this table exists to answer: *what was changed after the
        fact, and what did it say before?* Everything here is an amount, a date
        or an event type that someone altered or removed once it had already
        been posted — each with the ``before_image`` needed to see what it was.
        """
        placeholders = ",".join("?" * len(self.MONEY_ENTITIES))
        sql = (f"SELECT ts, operation, entity, entity_id, summary, before_image "
               f"FROM audit_log WHERE entity IN ({placeholders})")
        params = list(self.MONEY_ENTITIES)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._dicts(sql, tuple(params))

    def money_change_count(self, since=None):
        """How many money rows have been altered or removed after the fact."""
        placeholders = ",".join("?" * len(self.MONEY_ENTITIES))
        sql = f"SELECT COUNT(*) FROM audit_log WHERE entity IN ({placeholders})"
        params = list(self.MONEY_ENTITIES)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        return self._scalar(sql, tuple(params), 0)
