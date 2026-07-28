"""Phase 2a — SettingsRepository.

The behavioural contract matters more than the API here: this refactor must not
change a single value the application computes, so the tests pin the defaults
and the parsing rather than just exercising the methods.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import DatabaseManager
from src.db import settings as settings_module
from tests.helpers import make_settings


@pytest.fixture
def journal():
    db = DatabaseManager(os.path.join(tempfile.mkdtemp(), "s.db"))
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# Raw access — the contract DatabaseManager.get_setting/set_setting keep
# --------------------------------------------------------------------------- #

def test_get_returns_stored_value(journal):
    journal.settings.set("app_theme", "Dark")
    assert journal.settings.get("app_theme") == "Dark"


def test_get_falls_back_to_the_explicit_default_first(journal):
    """An explicit default must still win, so no existing caller changes."""
    assert journal.settings.get("app_theme", "Caller Wins") == "Caller Wins"


def test_get_falls_back_to_the_module_default(journal):
    assert journal.settings.get("app_theme") == "Light"


def test_get_returns_none_for_an_unknown_key(journal):
    assert journal.settings.get("no_such_key") is None


def test_set_overwrites(journal):
    journal.settings.set("app_theme", "Dark")
    journal.settings.set("app_theme", "Light")
    assert journal.settings.get("app_theme") == "Light"


def test_set_stringifies(journal):
    journal.settings.set("christmas_unlock_month", 6)
    assert journal.settings.get("christmas_unlock_month") == "6"


def test_all_returns_only_stored_pairs(journal):
    journal.settings.set("app_theme", "Dark")
    stored = journal.settings.all()
    assert stored["app_theme"] == "Dark"
    assert "christmas_unlock_month" not in stored     # defaults are not rows


def test_database_manager_still_delegates(journal):
    """The public API every existing caller uses is unchanged."""
    journal.set_setting("app_theme", "Dark")
    assert journal.get_setting("app_theme") == "Dark"
    assert journal.get_setting("missing", "fallback") == "fallback"


# --------------------------------------------------------------------------- #
# Typed accessors
# --------------------------------------------------------------------------- #

def test_bool_parsing_accepts_the_stored_forms():
    for stored in ("true", "True", "TRUE", "1", "yes", "on"):
        assert make_settings({"deduct_same_month": stored}).deduct_same_month is True
    for stored in ("false", "False", "0", "no", "", "nonsense"):
        assert make_settings({"deduct_same_month": stored}).deduct_same_month is False


def test_bool_default_is_false():
    assert make_settings().deduct_same_month is False


def test_int_accessor_and_recovery_from_junk():
    assert make_settings({"christmas_unlock_month": "6"}).christmas_unlock_month == 6
    # A corrupt value must not crash a fund operation; fall back to the default.
    assert make_settings({"christmas_unlock_month": "December"}).christmas_unlock_month == 12
    assert make_settings().christmas_unlock_month == 12


def test_float_accessors():
    assert make_settings({"default_savings_increment": "3000.50"}).default_savings_increment == 3000.50
    assert make_settings().default_savings_increment == 2500.0
    assert make_settings().default_christmas_increment == 0.0
    assert make_settings({"default_savings_increment": "abc"}).default_savings_increment == 2500.0


def test_org_name_is_trimmed():
    assert make_settings({"org_name": "  Acme Co-op  "}).org_name == "Acme Co-op"
    assert make_settings().org_name == ""


def test_excel_palette_defaults_match_branding():
    from src import branding
    s = make_settings()
    assert s.excel_header_bg == branding.EXCEL_HEADER_BG
    assert s.excel_total_bg == branding.EXCEL_TOTAL_BG


# --------------------------------------------------------------------------- #
# Behaviour preservation (finding S1)
# --------------------------------------------------------------------------- #

def test_fy_start_month_default_preserves_report_behaviour():
    """reports.py computed on 'January' when the key was unset; that is the
    value that affects money, so unifying on it changes no existing figure."""
    assert settings_module.DEFAULTS[settings_module.FY_START_MONTH] == "January"
    assert make_settings().fy_start_month == "January"


def test_fy_start_month_respects_a_stored_value():
    assert make_settings({"fy_start_month": "November"}).fy_start_month == "November"


def test_every_default_is_a_string():
    """DEFAULTS mirrors the TEXT column, so the typed accessors do the parsing
    in one place rather than each caller guessing."""
    assert all(isinstance(v, str) for v in settings_module.DEFAULTS.values())


def test_every_documented_key_has_a_default():
    keys = {v for k, v in vars(settings_module).items()
            if k.isupper() and isinstance(v, str) and not k.startswith("_")
            and k not in ("DEFAULTS",)}
    assert keys <= set(settings_module.DEFAULTS), keys - set(settings_module.DEFAULTS)


# --------------------------------------------------------------------------- #
# Consumers read through the repository
# --------------------------------------------------------------------------- #

def test_theme_manager_reads_through_the_repository(journal):
    from src.theme import ThemeManager
    journal.settings.set("app_theme", "Dark")
    assert ThemeManager(journal).current_theme_name == "Dark"


def test_christmas_service_reads_unlock_month(journal):
    from src.services.christmas_service import ChristmasService
    journal.settings.set("christmas_unlock_month", 6)
    svc = ChristmasService(journal)
    assert svc.get_unlock_month() == 6
    assert svc.withdrawals_allowed("2026-06-15") is True
    assert svc.withdrawals_allowed("2026-12-15") is False
