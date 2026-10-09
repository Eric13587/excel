"""Shared plumbing for repositories.

A repository owns every SQL statement that touches one table (or one closely
held group of tables) and exposes intent-revealing methods instead. The base
class supplies the cursor handling and row mapping so subclasses contain SQL
and nothing else.

Every method here parameterises its inputs. Identifiers can never be
parameterised in SQL, so where a table or column name must be interpolated the
subclass is responsible for taking it from a fixed whitelist — see
:meth:`Repository._checked_identifier`.
"""
import logging

logger = logging.getLogger(__name__)


class Repository:
    """Base class: holds a connection and maps rows.

    Takes the live ``sqlite3.Connection`` rather than the DatabaseManager, so
    repositories cannot reach back into application logic and remain trivially
    testable against a bare connection.
    """

    def __init__(self, conn):
        self.conn = conn

    # -- reads ------------------------------------------------------------ #

    def _rows(self, sql, params=()):
        """All rows as tuples."""
        return self.conn.execute(sql, params).fetchall()

    def _row(self, sql, params=()):
        """First row as a tuple, or None."""
        return self.conn.execute(sql, params).fetchone()

    def _scalar(self, sql, params=(), default=None):
        """First column of the first row, or ``default`` when there is no row.

        Note the distinction: a row containing NULL returns None, an absent row
        returns ``default``. Callers rely on telling those apart.
        """
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row is not None else default

    def _dicts(self, sql, params=()):
        """All rows as dicts keyed by column name."""
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _dict(self, sql, params=()):
        """First row as a dict, or None."""
        cur = self.conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    # -- writes ----------------------------------------------------------- #

    def _execute(self, sql, params=()):
        """Run a write and return the cursor (for ``lastrowid``/``rowcount``)."""
        return self.conn.execute(sql, params)

    def _executemany(self, sql, seq):
        return self.conn.executemany(sql, seq)

    # -- identifiers ------------------------------------------------------ #

    @staticmethod
    def _checked_identifier(name, allowed):
        """Return ``name`` if it is in ``allowed``, else raise.

        The only safe way to interpolate an identifier: compare against a fixed
        set the caller controls, never against user input.
        """
        if name not in allowed:
            raise ValueError(f"Unknown identifier {name!r}; expected one of {sorted(allowed)}")
        return name
