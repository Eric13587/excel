"""Tests for the ThemeManager stylesheet helpers and shared export palette."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers import make_settings
from src import branding
from src.theme import Theme, ThemeManager


class FakeDB:
    def __init__(self, theme="Light"):
        self.settings = make_settings({"app_theme": theme})

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings.set(key, value)


def test_button_css_uses_palette_and_tracks_theme():
    light = ThemeManager(FakeDB("Light"))
    dark = ThemeManager(FakeDB("Dark"))
    assert Theme.LIGHT["danger"] in light.button_css("danger")
    assert Theme.DARK["danger"] in dark.button_css("danger")
    assert light.button_css("accent") != dark.button_css("accent")


def test_button_css_has_hover_and_disabled_states():
    css = ThemeManager(FakeDB()).button_css("success")
    assert "QPushButton:hover" in css
    assert "QPushButton:disabled" in css
    assert Theme.LIGHT["border"] in css  # disabled background


def test_warning_button_gets_dark_text_for_contrast():
    css = ThemeManager(FakeDB()).button_css("warning")
    assert "color: #1F2937" in css     # amber is light -> dark text
    accent = ThemeManager(FakeDB()).button_css("accent")
    assert "color: white" in accent    # blue is dark -> white text


def test_hint_and_outline_and_status_helpers():
    t = ThemeManager(FakeDB())
    assert Theme.LIGHT["text_secondary"] in t.hint_css()
    assert Theme.LIGHT["accent"] in t.outline_button_css("accent")
    assert Theme.LIGHT["danger"] in t.status_label_css("danger")


def test_neutral_role_exists_in_both_palettes():
    assert "neutral" in Theme.LIGHT and "neutral" in Theme.DARK


def test_excel_palette_constants_are_valid_hex():
    for c in (branding.EXCEL_ACCENT_BG, branding.EXCEL_HEADER_BG,
              branding.EXCEL_TOTAL_BG, branding.EXCEL_MUTED_TEXT):
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", c)



# --------------------------------------------------------------------------- #
# Menus
# --------------------------------------------------------------------------- #
# Nothing styled a QMenu, so the member right-click, the ledger's context menus
# and the window menu bar rendered in whatever colours the desktop supplied —
# white-on-white in light mode on some systems. The same failure the alternating
# table rows had, fixed the same way: name the colours.

def test_menu_css_names_every_part_of_a_menu():
    for theme in ("Light", "Dark"):
        css = ThemeManager(FakeDB(theme)).menu_css()
        for rule in ("QMenu {", "QMenu::item", "QMenu::item:selected",
                     "QMenu::separator", "QMenuBar {"):
            assert rule in css, f"{rule} missing in {theme}"


def test_menu_text_is_never_the_menu_background():
    """The bug, as an assertion."""
    for theme in ("Light", "Dark"):
        t = ThemeManager(FakeDB(theme))
        assert t.get_color("text_primary").lower() != t.get_color("bg_secondary").lower()


def test_a_highlighted_menu_item_keeps_readable_text():
    for theme in ("Light", "Dark"):
        t = ThemeManager(FakeDB(theme))
        accent = t.get_color("accent")
        css = t.menu_css()
        assert f"background-color: {accent}" in css
        # Picked from the accent's luminance, never assumed to be white.
        assert t._text_on(accent) in css


def test_menu_css_tracks_the_theme():
    light = ThemeManager(FakeDB("Light")).menu_css()
    dark = ThemeManager(FakeDB("Dark")).menu_css()
    assert light != dark
    assert Theme.LIGHT["bg_secondary"] in light
    assert Theme.DARK["bg_secondary"] in dark
