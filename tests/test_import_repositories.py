"""Phase 2f-i — SourceJournal, RepairRepository, ImportHistoryRepository."""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.db.source import SourceJournal, SourceJournalError


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "d.db"))
    yield db
    db.close()


@pytest.fixture
def source():
    path = os.path.join(tempfile.mkdtemp(), "s.db")
    db = DatabaseManager(path)
    a = db.add_individual("Alice", "111", "a@x.com")
    b = db.add_individual("Bob", "222", "b@x.com")
    db.add_loan_record(a, "L-001", 1000, 1150, 1000, 100, 13, "2026-01-01", "2026-02-01")
    db.add_transaction(a, "2026-01-01", "Loan Issued", "L-001", 1000, 0, 1000, "issued")
    db.add_savings_transaction(b, "2026-01-01", "Deposit", 500)
    db.close()
    return path, a, b


# --------------------------------------------------------------------------- #
# SourceJournal
# --------------------------------------------------------------------------- #

def test_source_is_opened_read_only(source):
    path, _, _ = source
    with SourceJournal(path) as src:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            src.conn.execute("DELETE FROM individuals")


def test_missing_source_raises(tmp_path):
    with pytest.raises(SourceJournalError):
        SourceJournal(str(tmp_path / "nope.db"))


def test_only_allowlisted_tables_are_reachable(source):
    """A crafted source file cannot steer a read at a table the importer never
    intended to touch."""
    path, _, _ = source
    with SourceJournal(path) as src:
        for bad in ("settings", "journal_entries", "sqlite_master", "individuals; DROP TABLE x"):
            assert src.has_table(bad) is False
            with pytest.raises(ValueError):
                src.all_rows(bad)
        assert src.has_table("individuals") is True


def test_member_reads(source):
    path, a, b = source
    with SourceJournal(path) as src:
        everyone = src.member_summaries()
        assert [m["name"] for m in everyone] == ["Alice", "Bob"]      # ordered by name
        assert [m["name"] for m in src.member_summaries([a])] == ["Alice"]
        assert src.member_summaries([]) == []
        assert dict(src.member_names([a, b])) == {a: "Alice", b: "Bob"}
        assert src.members([a])[0]["name"] == "Alice"


def test_counts_and_absent_tables(source):
    path, a, b = source
    with SourceJournal(path) as src:
        assert src.count("loans") == 1
        assert src.count("ledger", member_id=a) == 1
        assert src.count("ledger", member_id=b) == 0
        assert src.count("savings") == 1
        # A table the source happens not to have reports zero rather than raising.
        assert src.count("loan_suspensions") == 0


def test_loan_and_ledger_reads(source):
    path, a, b = source
    with SourceJournal(path) as src:
        assert src.loan_refs(a) == ["L-001"]
        assert [dict(r)["ref"] for r in src.loans([a])] == ["L-001"]
        assert src.loans([]) == []

        cols, rows = src.ledger([a])
        assert "event_type" in cols and len(rows) == 1
        _, none = src.ledger([a], date_range=("2030-01-01", "2030-12-31"))
        assert none == []

        cols, rows = src.savings()
        assert "transaction_type" in cols and len(rows) == 1


def test_validate(source, tmp_path):
    path, _, _ = source
    assert SourceJournal.validate(path) == (True, "")
    assert SourceJournal.validate(str(tmp_path / "nope.db"))[0] is False

    empty = tmp_path / "empty.db"; empty.touch()
    assert "empty" in SourceJournal.validate(str(empty))[1].lower()

    junk = tmp_path / "junk.txt"; junk.write_text("not a database")
    assert "not a valid SQLite" in SourceJournal.validate(str(junk))[1]

    no_table = tmp_path / "notable.db"
    conn = sqlite3.connect(str(no_table)); conn.execute("CREATE TABLE other (id INTEGER)"); conn.close()
    assert "Missing required table" in SourceJournal.validate(str(no_table))[1]

    no_col = tmp_path / "nocol.db"
    conn = sqlite3.connect(str(no_col))
    conn.execute("CREATE TABLE individuals (id INTEGER PRIMARY KEY, phone TEXT)"); conn.close()
    assert "missing required column" in SourceJournal.validate(str(no_col))[1]


# --------------------------------------------------------------------------- #
# RepairRepository
# --------------------------------------------------------------------------- #

def _make_orphans(db):
    """Simulate a pre-migration-001 journal, where a parent delete orphaned rows."""
    db.conn.execute("DROP TRIGGER IF EXISTS trg_cascade_individual_delete")
    db.conn.commit()
    keep = db.add_individual("Keep", "1", "k@x.com")
    gone = db.add_individual("Gone", "2", "g@x.com")
    for ind in (keep, gone):
        db.add_savings_transaction(ind, "2026-01-01", "Deposit", 100)
        db.add_transaction(ind, "2026-01-01", "Loan Issued", "L-001", 10, 0, 10, "x")
    db.conn.execute("PRAGMA foreign_keys = OFF")
    db.conn.execute("DELETE FROM individuals WHERE id=?", (gone,))
    db.conn.commit()
    return keep, gone


def test_child_tables_are_discovered(journal):
    tables = journal.repair.child_tables()
    assert "individuals" not in tables
    for expected in ("ledger", "loans", "savings", "christmas_savings",
                     "benevolent_ledger", "benevolent_accounts", "loan_suspensions"):
        assert expected in tables
    # Tables without an individual_id are excluded.
    assert "settings" not in tables and "journal_entries" not in tables


def test_orphan_groups_and_deletion(journal):
    _, gone = _make_orphans(journal)

    groups = {t: journal.repair.orphan_groups(t) for t in journal.repair.child_tables()}
    assert groups["savings"] == [(gone, 1, "2026-01-01", "2026-01-01")]
    assert groups["ledger"] == [(gone, 1, "2026-01-01", "2026-01-01")]
    assert groups["loans"] == []

    assert len(journal.repair.orphan_frame("savings")) == 1
    assert journal.repair.orphan_frame("loans").empty

    with journal.transaction():
        assert journal.repair.delete_orphans("savings") == 1
        assert journal.repair.delete_orphans("loans") == 0
    assert journal.repair.orphan_groups("savings") == []


def test_null_member_rows_count_as_orphans(journal):
    journal.conn.execute("PRAGMA foreign_keys = OFF")
    journal.conn.execute("INSERT INTO savings (individual_id, date, transaction_type, amount, "
                         "balance) VALUES (NULL, '2026-01-01', 'Deposit', 5, 5)")
    journal.conn.commit()
    assert journal.repair.orphan_groups("savings") == [(None, 1, "2026-01-01", "2026-01-01")]


def test_tables_without_a_date_column_report_none(journal):
    _, gone = _make_orphans(journal)
    journal.conn.execute("PRAGMA foreign_keys = OFF")
    journal.conn.execute("INSERT INTO benevolent_accounts (individual_id, monthly_amount, "
                         "active) VALUES (?, 50, 1)", (gone,))
    journal.conn.commit()
    assert journal.repair.orphan_groups("benevolent_accounts") == [(gone, 1, None, None)]


# --------------------------------------------------------------------------- #
# ImportHistoryRepository
# --------------------------------------------------------------------------- #

def test_import_run_lifecycle(journal):
    with journal.transaction():
        run = journal.imports.start("2026-07-28 10:00:00", "/tmp/src.db")
        journal.imports.finish(run, '{"individuals": 3}', 42)

    history = journal.imports.all()
    assert len(history) == 1
    assert history[0]["id"] == run
    assert history[0]["source_file"] == "/tmp/src.db"
    assert history[0]["details"] == '{"individuals": 3}'
    assert history[0]["item_count"] == 42


def test_history_is_newest_first(journal):
    with journal.transaction():
        first = journal.imports.start("2026-01-01 00:00:00", "a.db")
        second = journal.imports.start("2026-02-01 00:00:00", "b.db")
    assert [h["id"] for h in journal.get_import_history()] == [second, first]


def test_undo_removes_only_what_the_run_created(journal):
    """Merged members carry no import_id, so an undo never removes them."""
    pre_existing = journal.add_individual("Pre Existing", "1", "p@x.com")
    with journal.transaction():
        run = journal.imports.start("2026-07-28 10:00:00", "src.db")
        journal.conn.execute(
            "INSERT INTO individuals (name, import_id) VALUES ('Imported', ?)", (run,))
        imported = journal.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        journal.conn.execute(
            "INSERT INTO savings (individual_id, date, transaction_type, amount, balance, "
            "import_id) VALUES (?, '2026-01-01', 'Deposit', 10, 10, ?)", (imported, run))
        journal.conn.execute(
            "INSERT INTO savings (individual_id, date, transaction_type, amount, balance) "
            "VALUES (?, '2026-01-01', 'Deposit', 99, 99)", (pre_existing,))

    assert journal.undo_import(run) is True

    names = sorted(r[1] for r in journal.get_individuals())
    assert names == ["Pre Existing"]
    assert journal.get_savings_balance(pre_existing) == 99
    assert journal.get_import_history() == []


def test_undo_of_nothing_is_false(journal):
    assert journal.undo_import(None) is False
