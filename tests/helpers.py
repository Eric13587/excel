"""Shared test helpers."""
import sqlite3

from src.db import SettingsRepository


def make_settings(values=None):
    """A real SettingsRepository over a throwaway in-memory `settings` table.

    Test doubles for the database used to hand-roll a dict plus a get_setting
    method. Now that callers read typed properties (db.settings.fy_start_month),
    a hand-rolled fake would have to reimplement the parsing and would drift
    from it. Using the real class keeps the double honest for four lines.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    repo = SettingsRepository(conn)
    for key, value in (values or {}).items():
        repo.set(key, value)
    return repo
