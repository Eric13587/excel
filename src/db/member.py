"""The ``individuals`` table: SACCO members.

Root of every ownership chain in the journal — loans, ledger, savings, both
funds, enrolments and suspensions all hang off a member row.

Two conventions this module deliberately preserves rather than improves,
because Phase 2 is a refactor and must not change behaviour:

* :meth:`MemberRepository.rows` returns raw tuples from ``SELECT *``. Ten call
  sites index them positionally (``m[0]``), so the shape is load-bearing.
  Returning dataclasses is the Phase 2f/3 change, once those callers move.
* Blank PF/ID numbers are never treated as duplicates, matching the partial
  unique indexes on those columns.

Repositories do not commit. The caller owns the transaction boundary, so a
multi-statement operation such as :meth:`update` stays atomic.
"""
import logging
from datetime import datetime

from src.db.base import Repository

logger = logging.getLogger(__name__)

ACTIVE = "Active"


class MemberRepository(Repository):
    """Every statement that touches ``individuals`` lives here."""

    # -- domain helper ---------------------------------------------------- #

    @staticmethod
    def retirement_for_status(status, existing_retired_date=None):
        """Derive ``(is_retired, retired_date)`` from an employment status.

        Every status except 'Active' is treated as retired/inactive (mirrors the
        retirement flag), keeping an existing retirement date if one is set,
        otherwise stamping today. 'Active' clears the flag.
        """
        if (status or ACTIVE) == ACTIVE:
            return 0, None
        return 1, (existing_retired_date or datetime.now().strftime("%Y-%m-%d"))

    # -- reads ------------------------------------------------------------ #

    def rows(self):
        """Every member as a raw tuple (legacy ``SELECT *`` shape)."""
        return self._rows("SELECT * FROM individuals")

    def get(self, member_id):
        """One member as a dict keyed by column name, or None."""
        return self._dict("SELECT * FROM individuals WHERE id=?", (member_id,))

    # -- grid participation ----------------------------------------------- #
    # Which members take part in which ledger. Stored as exceptions only:
    # a member with no row is included, so an untouched journal behaves
    # exactly as it did and the table stays as small as the decisions made.

    def excluded_from(self, scope):
        """``{member_id: note}`` for everybody left out of one ledger."""
        return {row[0]: row[1] or "" for row in self._rows(
            "SELECT individual_id, note FROM grid_participation "
            "WHERE scope=? AND excluded=1", (scope,))}

    def set_excluded(self, member_id, scope, excluded, note=""):
        """Take a member out of one ledger's grid, or put them back."""
        if excluded:
            self._execute(
                "INSERT INTO grid_participation (individual_id, scope, excluded, "
                "note, created_at) VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(individual_id, scope) DO UPDATE SET excluded=1, note=?",
                (member_id, scope, note,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), note))
        else:
            self._execute(
                "DELETE FROM grid_participation WHERE individual_id=? AND scope=?",
                (member_id, scope))

    def summaries(self):
        """Every member as the few fields a listing needs, name-ordered.

        Narrower than :meth:`rows` on purpose: a screen showing hundreds of
        members at once wants identity and standing, not every column, and
        wants them without a query per member.
        """
        return self._dicts(
            "SELECT id, name, pf_no, employment_status, is_retired, retired_date "
            "FROM individuals ORDER BY name COLLATE NOCASE, id")

    def name_of(self, member_id):
        """The member's name, or None when there is no such member.

        The caller supplies any display fallback — that is a presentation
        decision, not a data one.
        """
        return self._scalar("SELECT name FROM individuals WHERE id=?", (member_id,))

    def name_exists(self, name):
        """Case-insensitive existence check.

        Uses ``LOWER(name) = LOWER(?)``. The import path's conflict scan still
        uses ``name LIKE ?`` for the same question, which additionally treats
        ``%`` and ``_`` in the supplied name as wildcards (finding D8); both
        converge here in Phase 2f.
        """
        return self._scalar(
            "SELECT COUNT(*) FROM individuals WHERE LOWER(name) = LOWER(?)", (name,), 0) > 0

    def owner_of_pf_no(self, pf_no, exclude_id=None):
        """Name of the member already using this PF number, or None."""
        return self._owner_of("pf_no", pf_no, exclude_id)

    def owner_of_id_no(self, id_no, exclude_id=None):
        """Name of the member already using this ID number, or None."""
        return self._owner_of("id_no", id_no, exclude_id)

    def _owner_of(self, column, value, exclude_id):
        """Shared body for the PF/ID uniqueness probes.

        ``column`` is interpolated, so it is checked against a fixed whitelist;
        the value is always parameterised. Blank numbers are never duplicates —
        many members have none, and they must not collide with each other.
        """
        column = self._checked_identifier(column, {"pf_no", "id_no"})
        if not value or not str(value).strip():
            return None
        value = str(value).strip()
        if exclude_id is not None:
            return self._scalar(
                f"SELECT name FROM individuals WHERE {column}=? AND id<>?", (value, exclude_id))
        return self._scalar(f"SELECT name FROM individuals WHERE {column}=?", (value,))

    def names_to_ids(self):
        """{name: id} for merge-by-exact-name during an import."""
        return {name: member_id for name, member_id in
                self._rows("SELECT name, id FROM individuals")}

    def used_pf_numbers(self):
        return {r[0] for r in self._rows(
            "SELECT pf_no FROM individuals WHERE pf_no IS NOT NULL AND pf_no != ''")}

    def used_id_numbers(self):
        return {r[0] for r in self._rows(
            "SELECT id_no FROM individuals WHERE id_no IS NOT NULL AND id_no != ''")}

    def match_by_name(self, name):
        """Candidate duplicates by name, for the import conflict scan.

        Uses ``LIKE``, which is case-insensitive for ASCII but also treats ``%``
        and ``_`` in the *supplied* name as wildcards — so a member named
        ``O'Brien_J`` matches names nobody intended (finding D8).
        :meth:`name_exists` answers the same question with
        ``LOWER(name) = LOWER(?)``. Preserved as-is; converging them changes
        which rows the importer flags as conflicts.
        """
        return self._dicts(
            "SELECT id, name, phone, email FROM individuals WHERE name LIKE ?", (name,))

    def match_by_phone(self, phone):
        return self._dicts(
            "SELECT id, name, phone, email FROM individuals WHERE phone = ?", (phone,))

    def match_by_email(self, email):
        """Also ``LIKE`` — same caveat as :meth:`match_by_name`."""
        return self._dicts(
            "SELECT id, name, phone, email FROM individuals WHERE email LIKE ?", (email,))

    def retired_date_of(self, member_id):
        return self._scalar("SELECT retired_date FROM individuals WHERE id=?", (member_id,))

    # -- writes ----------------------------------------------------------- #

    def add(self, name, phone, email, default_deduction=0,
            employment_status=ACTIVE, pf_no="", id_no=""):
        """Insert a member and return the new id."""
        status = employment_status or ACTIVE
        is_retired, retired_date = self.retirement_for_status(status)
        cur = self._execute(
            "INSERT INTO individuals (name, phone, email, default_deduction, created_at, "
            "employment_status, pf_no, id_no, is_retired, retired_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, phone, email, default_deduction,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             status, pf_no or "", id_no or "", is_retired, retired_date))
        return cur.lastrowid

    def add_imported(self, name, phone, email, default_deduction, created_at,
                     employment_status, pf_no, id_no, is_retired, retired_date, import_id):
        """Insert a member created by an import run.

        ``import_id`` is what makes the run undoable: only rows the import
        *created* carry one, so a member who already existed and was merged
        into is never removed by an undo.
        """
        cur = self._execute(
            "INSERT INTO individuals (name, phone, email, default_deduction, created_at, "
            "employment_status, pf_no, id_no, is_retired, retired_date, import_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, phone, email, default_deduction, created_at, employment_status,
             pf_no, id_no, is_retired, retired_date, import_id))
        return cur.lastrowid

    def update_core(self, member_id, name, phone, email):
        self._execute("UPDATE individuals SET name=?, phone=?, email=? WHERE id=?",
                      (name, phone, email, member_id))

    def set_employment_status(self, member_id, status):
        """Set the status and the retirement flag it implies, together."""
        is_retired, retired_date = self.retirement_for_status(
            status, self.retired_date_of(member_id))
        self._execute(
            "UPDATE individuals SET employment_status=?, is_retired=?, retired_date=? WHERE id=?",
            (status, is_retired, retired_date, member_id))

    def set_pf_no(self, member_id, pf_no):
        self._execute("UPDATE individuals SET pf_no=? WHERE id=?", (pf_no, member_id))

    def set_id_no(self, member_id, id_no):
        self._execute("UPDATE individuals SET id_no=? WHERE id=?", (id_no, member_id))

    def set_default_deduction(self, member_id, amount):
        """Cached SUM(installment) over the member's active loans."""
        self._execute("UPDATE individuals SET default_deduction=? WHERE id=?",
                      (amount, member_id))

    def retire(self, member_id, date_str):
        self._execute("UPDATE individuals SET is_retired=1, retired_date=?, "
                      "employment_status='Retired' WHERE id=?", (date_str, member_id))

    def reinstate(self, member_id):
        self._execute("UPDATE individuals SET is_retired=0, retired_date=NULL, "
                      "employment_status='Active' WHERE id=?", (member_id,))

    def delete(self, member_id):
        """Delete a member and their loan ledger.

        The explicit ``ledger`` delete is redundant on any journal that has had
        migration 001 applied — the cascade trigger clears it, along with the
        six other child tables this statement never covered. It is kept so the
        operation behaves identically on a journal that has not been migrated
        (see docs/db/phase-1-migration.md §1). The caller must supply the
        transaction; without one, a failure here is what caused the silent
        half-delete that Phase 1a fixed.
        """
        self._execute("DELETE FROM ledger WHERE individual_id=?", (member_id,))
        self._execute("DELETE FROM individuals WHERE id=?", (member_id,))
