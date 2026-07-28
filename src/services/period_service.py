"""Fiscal periods: declaring them, closing them, and asking what is settled.

A closed period means *the figures published for this range are the figures*.
Migration 009 makes that a lock; this service is the vocabulary underneath it —
what the periods are, which one a date falls in, and what closing one requires.

Two decisions shape everything here.

**Periods are declared, not derived.** ``fy_start_month`` is a setting and can
be changed. Deriving boundaries from it would silently redraw periods that had
already been closed, so :meth:`suggest_periods` proposes ranges from the current
setting and the user commits them; after that the stored dates are the truth.

**Closing is refused, not warned about, when the books do not balance.** A
period whose figures are wrong is not finished, and freezing it would preserve
the error rather than the result. The checks are listed by
:meth:`closing_blockers` so the reason is visible before the attempt.
"""
import calendar
import logging
from datetime import datetime

from dateutil.relativedelta import relativedelta

from src.db.period import CLOSED, OPEN
from src.exceptions import PeriodClosedError, PeriodNotClosableError

logger = logging.getLogger(__name__)


class FiscalPeriodService:
    """Declare, close and interrogate fiscal periods."""

    def __init__(self, db_manager, gl_service=None):
        self.db = db_manager
        self._gl = gl_service

    @property
    def gl(self):
        if self._gl is None:
            from src.services.gl_service import GLService
            self._gl = GLService(self.db)
        return self._gl

    # ------------------------------------------------------------------ #
    # The calendar
    # ------------------------------------------------------------------ #
    def fy_start_month_index(self):
        """1-based month the financial year starts in, from Settings."""
        try:
            return datetime.strptime(self.db.settings.fy_start_month, "%B").month
        except (ValueError, TypeError):
            return 1

    def fy_bounds(self, on_date):
        """(start, end) of the financial year containing ``on_date``.

        Matches ReportGenerator._get_fy_start_date rather than reimplementing
        it: a month before the FY start belongs to the year that began the
        previous calendar year.
        """
        start_month = self.fy_start_month_index()
        d = self._as_date(on_date)
        year = d.year if d.month >= start_month else d.year - 1
        start = datetime(year, start_month, 1)
        end = start + relativedelta(years=1, days=-1)
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def suggest_periods(self, on_date=None, granularity="quarter"):
        """Propose the periods of one financial year, without creating them.

        Returns ``[{'label', 'start_date', 'end_date'}]``. Existing periods are
        excluded, so this can be run repeatedly to fill gaps.
        """
        if granularity not in ("quarter", "year", "month"):
            raise ValueError(f"unknown granularity {granularity!r}")
        fy_start, fy_end = self.fy_bounds(on_date or datetime.now())
        start = datetime.strptime(fy_start, "%Y-%m-%d")
        step = {"quarter": 3, "year": 12, "month": 1}[granularity]
        taken = {(p["start_date"], p["end_date"]) for p in self.db.periods.all()}
        labels = {p["label"] for p in self.db.periods.all()}

        out = []
        cursor = start
        index = 1
        while cursor.strftime("%Y-%m-%d") <= fy_end:
            end = cursor + relativedelta(months=step, days=-1)
            label = self._label(cursor, end, granularity, index, fy_start)
            pair = (cursor.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
            if pair not in taken and label not in labels:
                out.append({"label": label, "start_date": pair[0], "end_date": pair[1]})
            cursor += relativedelta(months=step)
            index += 1
        return out

    def _label(self, start, end, granularity, index, fy_start):
        fy_year = datetime.strptime(fy_start, "%Y-%m-%d").year
        if granularity == "year":
            return f"FY {fy_year}/{fy_year + 1}" if self.fy_start_month_index() != 1 \
                else f"FY {fy_year}"
        if granularity == "month":
            return f"{calendar.month_name[start.month]} {start.year}"
        return f"FY {fy_year} Q{index}"

    # ------------------------------------------------------------------ #
    # The register
    # ------------------------------------------------------------------ #
    def periods(self):
        return self.db.periods.all()

    def create(self, label, start_date, end_date, note=None):
        """Declare a period. Overlaps are rejected by the database."""
        with self.db.transaction():
            return self.db.periods.add(label, start_date, end_date, note)

    def create_suggested(self, on_date=None, granularity="quarter"):
        """Declare every suggested period of a financial year. Returns the count."""
        suggestions = self.suggest_periods(on_date, granularity)
        with self.db.transaction():
            for s in suggestions:
                self.db.periods.add(s["label"], s["start_date"], s["end_date"])
        return len(suggestions)

    def status_of(self, date):
        """``'closed'``, ``'open'``, or None when no period covers the date."""
        row = self.db.periods.covering(self._iso(date))
        return row["status"] if row else None

    def is_closed(self, date):
        """True when ``date`` falls inside a closed period."""
        return self.db.periods.closed_covering(self._iso(date)) is not None

    def assert_open(self, date, what="This entry"):
        """Raise :class:`PeriodClosedError` if ``date`` is in a closed period.

        The Python-side guard. Migration 009 enforces the same rule with
        triggers, so it holds for a code path that never reaches a service.
        """
        row = self.db.periods.closed_covering(self._iso(date))
        if row:
            raise PeriodClosedError(self._iso(date), row["label"], what)

    def settled_through(self):
        """End date of the most recent closed period, or None."""
        return self.db.periods.latest_closed_end()

    # ------------------------------------------------------------------ #
    # Closing
    # ------------------------------------------------------------------ #
    def closing_blockers(self, period_id):
        """Reasons this period cannot be closed, as a list of strings.

        Closing freezes the figures, so it is refused while they are still
        moving or still wrong. Empty means it is ready.
        """
        period = self.db.periods.get(period_id)
        if period is None:
            return [f"No period with id {period_id}."]
        if period["status"] == CLOSED:
            return [f"{period['label']} is already closed."]

        blockers = []

        earlier = [p for p in self.db.periods.all()
                   if p["end_date"] < period["start_date"] and p["status"] == OPEN]
        if earlier:
            names = ", ".join(p["label"] for p in earlier)
            blockers.append(f"An earlier period is still open: {names}.")

        depth = self.db.journal.outbox_depth()
        if depth:
            blockers.append(
                f"{depth} subledger change(s) have not been posted to the ledger "
                f"— run Treasury → Sync first.")

        rows, balanced = self.gl.get_trial_balance(as_of=period["end_date"])
        if not balanced:
            blockers.append(
                "The trial balance does not balance as at "
                f"{period['end_date']} — the books must be correct before the "
                "period is frozen.")
        return blockers

    def close(self, period_id, closed_by=None, force=False):
        """Close a period. Raises :class:`PeriodNotClosableError` if not ready.

        ``force`` records the blockers in the period's note instead of refusing,
        for the case where a break is known, accepted and documented. It is
        never silent.
        """
        blockers = self.closing_blockers(period_id)
        if blockers and not force:
            raise PeriodNotClosableError(
                self.db.periods.get(period_id) or {}, blockers)

        note = None
        if blockers:
            note = "Closed with known issues: " + " ".join(blockers)
            logger.warning("Period %s closed despite %d blocker(s)", period_id, len(blockers))
        with self.db.transaction():
            self.db.periods.set_status(
                period_id, CLOSED,
                closed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                closed_by=closed_by)
            if note:
                self.db.periods.set_note(period_id, note)
        # Only now is the period settled, so only now can its figures be
        # recorded as fact rather than cached as a guess.
        self.gl.snapshot_period(self.db.periods.get(period_id))
        return self.db.periods.get(period_id)

    def reopen(self, period_id, reason, reopened_by=None):
        """Reopen a closed period. A reason is required and is recorded.

        Reopening undoes a published statement, so it leaves a trail: the note
        keeps the original closing timestamp and why it was undone.
        """
        if not (reason or "").strip():
            raise ValueError("Reopening a period requires a reason.")
        period = self.db.periods.get(period_id)
        if period is None:
            raise ValueError(f"No period with id {period_id}.")
        if period["status"] != CLOSED:
            return period

        trail = (f"{period.get('note') or ''} "
                 f"[reopened {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                 f"by {reopened_by or 'unknown'}, closed {period['closed_at']}: "
                 f"{reason.strip()}]").strip()
        # Discard first: the snapshots are only valid while the period is
        # closed, and this one's totals are cumulative, so every later snapshot
        # depends on it too.
        discarded = self.gl.discard_snapshots_from(period["start_date"])
        with self.db.transaction():
            self.db.periods.set_status(period_id, OPEN, closed_at=None, closed_by=None)
            self.db.periods.set_note(period_id, trail)
        if discarded:
            logger.info("Discarded %d snapshot row(s) from %s onward",
                        discarded, period["start_date"])
        logger.warning("Fiscal period %s reopened: %s", period["label"], reason.strip())
        return self.db.periods.get(period_id)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_date(value):
        if isinstance(value, datetime):
            return value
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")

    def _iso(self, value):
        return self._as_date(value).strftime("%Y-%m-%d")
