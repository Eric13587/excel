"""The ``settings`` table: per-journal configuration.

Every value is stored as TEXT, so each of the 21 call sites that read a setting
was also parsing it and carrying its own inline default. That is a schema with
no schema, and it had already diverged: ``fy_start_month`` defaulted to
'November' in the Settings dialog and 'January' in three places in
``reports.py``, so a journal where the user never opened Settings ran reports
on a January financial year while the UI displayed November (finding S1 in
docs/db/inconsistent-sql.md).

This module is now the single source of truth for the key names, their types
and their defaults. Callers ask for a typed value and get one.
"""
import logging

from src.db.base import Repository

logger = logging.getLogger(__name__)

# Key names. Referenced by src.branding for the two org keys, which predate
# this module and are re-exported there for compatibility.
ORG_NAME = "org_name"
ORG_LOGO_PATH = "org_logo_path"
APP_THEME = "app_theme"
FY_START_MONTH = "fy_start_month"
DEDUCT_SAME_MONTH = "deduct_same_month"
CHRISTMAS_UNLOCK_MONTH = "christmas_unlock_month"
DEFAULT_SAVINGS_INCREMENT = "default_savings_increment"
DEFAULT_CHRISTMAS_INCREMENT = "default_christmas_increment"
DEFAULT_INTEREST_RATE = "default_interest_rate"
EXCEL_HEADER_BG = "excel_header_bg"
EXCEL_TOTAL_BG = "excel_total_bg"

# The defaults that apply when a journal has never had the key written. These
# were previously repeated at each call site; changing one here changes it
# everywhere, which is the point.
#
# fy_start_month is 'November', the institution's actual financial year and
# what the production journal stores. It resolves finding S1, where the
# Settings dialog displayed 'November' when the key was unset while reports.py
# computed on 'January'.
#
# This default now only ever applies to a brand-new journal: migration 002
# writes every key below into the journal, so an existing journal has an
# explicit row and is unaffected. Change it in Settings -> Financial rather
# than here.
DEFAULTS = {
    ORG_NAME: "",
    ORG_LOGO_PATH: "",
    APP_THEME: "Light",
    FY_START_MONTH: "November",
    DEDUCT_SAME_MONTH: "false",
    CHRISTMAS_UNLOCK_MONTH: "12",
    DEFAULT_SAVINGS_INCREMENT: "2500",
    DEFAULT_CHRISTMAS_INCREMENT: "0",
    # Flat interest rate a loan's gross obligation is grossed-up by for display
    # (principal x (1 + rate)). 0.15 reproduces the previous hardcoded x 1.15,
    # so an unset journal shows exactly the same figures as before.
    DEFAULT_INTEREST_RATE: "0.15",
    EXCEL_HEADER_BG: "#E2E8F0",     # slate-200, matches branding.EXCEL_HEADER_BG
    EXCEL_TOTAL_BG: "#F1F5F9",      # slate-100, matches branding.EXCEL_TOTAL_BG
}

_TRUE = {"true", "1", "yes", "on"}


class SettingsRepository(Repository):
    """Every statement that touches ``settings`` lives here."""

    # -- raw access (the shape DatabaseManager.get_setting/set_setting keep) -- #

    def get(self, key, default=None):
        """Raw TEXT value. Falls back to ``default``, then to DEFAULTS[key].

        Preserves the existing contract exactly: an explicit ``default``
        argument wins, so no current caller changes behaviour when it passes
        one.
        """
        value = self._scalar("SELECT value FROM settings WHERE key=?", (key,))
        if value is not None:
            return value
        return default if default is not None else DEFAULTS.get(key)

    def set(self, key, value):
        """Write a setting. Does not commit — the caller owns the transaction."""
        self._execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                      (key, str(value)))

    def all(self):
        """Every stored key/value pair, defaults not included."""
        return dict(self._rows("SELECT key, value FROM settings ORDER BY key"))

    # -- typed accessors -------------------------------------------------- #

    def _text(self, key):
        return self.get(key) or ""

    def _bool(self, key):
        return str(self.get(key) or "").strip().lower() in _TRUE

    def _int(self, key):
        try:
            return int(float(self.get(key)))
        except (TypeError, ValueError):
            logger.warning("Setting %s is not an integer (%r); using the default",
                           key, self.get(key))
            return int(DEFAULTS[key])

    def _float(self, key):
        try:
            return float(self.get(key) or 0)
        except (TypeError, ValueError):
            logger.warning("Setting %s is not a number (%r); using the default",
                           key, self.get(key))
            return float(DEFAULTS[key])

    @property
    def org_name(self):
        return self._text(ORG_NAME).strip()

    @property
    def org_logo_path(self):
        return self._text(ORG_LOGO_PATH)

    @property
    def app_theme(self):
        return self._text(APP_THEME) or DEFAULTS[APP_THEME]

    @property
    def fy_start_month(self):
        """Financial-year start month, as an English month name."""
        return self._text(FY_START_MONTH) or DEFAULTS[FY_START_MONTH]

    @property
    def deduct_same_month(self):
        """True when a loan's first deduction falls in its month of issue."""
        return self._bool(DEDUCT_SAME_MONTH)

    @property
    def christmas_unlock_month(self):
        """Month (1-12) in which Christmas withdrawals are permitted."""
        return self._int(CHRISTMAS_UNLOCK_MONTH)

    @property
    def default_savings_increment(self):
        return self._float(DEFAULT_SAVINGS_INCREMENT)

    @property
    def default_christmas_increment(self):
        return self._float(DEFAULT_CHRISTMAS_INCREMENT)

    @property
    def default_interest_rate(self):
        """Flat interest rate (e.g. 0.15) used to gross-up a loan's obligation
        for display. Grossed-up total = principal x (1 + this)."""
        return self._float(DEFAULT_INTEREST_RATE)

    @property
    def excel_header_bg(self):
        return self._text(EXCEL_HEADER_BG) or DEFAULTS[EXCEL_HEADER_BG]

    @property
    def excel_total_bg(self):
        return self._text(EXCEL_TOTAL_BG) or DEFAULTS[EXCEL_TOTAL_BG]
