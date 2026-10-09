"""The on-open backup rotation keeps the dated copies and nothing else.

It used to prune every "<stem>.*.db" in the folder, which put the named safety
backups in the same queue — and since digits sort before letters, the copy just
written was always the first deleted. On the live journal that meant every
startup backup vanished the moment it was made.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager


def _backups(folder):
    return sorted(os.listdir(os.path.join(folder, "backups")))


def test_named_backups_neither_count_nor_get_pruned():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "journal.db")
    DatabaseManager(path, auto_backup=False).close()

    backups = os.path.join(folder, "backups")
    os.makedirs(backups)
    named = [f"journal.pre-repair-{i:02d}.db" for i in range(12)]
    for name in named:
        open(os.path.join(backups, name), "w").close()

    db = DatabaseManager(path, auto_backup=False)
    db._backup_on_open(keep=3)
    db.close()

    present = _backups(folder)
    assert all(name in present for name in named)
    dated = [n for n in present if n not in named]
    assert len(dated) == 1 and os.path.getsize(os.path.join(backups, dated[0])) > 0


def test_only_the_last_n_dated_backups_are_kept():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "journal.db")
    DatabaseManager(path, auto_backup=False).close()
    backups = os.path.join(folder, "backups")
    os.makedirs(backups)
    for day in range(1, 6):
        open(os.path.join(backups, f"journal.2026010{day}-120000.db"), "w").close()

    db = DatabaseManager(path, auto_backup=False)
    db._backup_on_open(keep=3)
    db.close()

    dated = [n for n in _backups(folder) if n[8:9].isdigit()]
    assert len(dated) == 3
    assert "journal.20260101-120000.db" not in dated
