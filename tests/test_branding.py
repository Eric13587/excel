"""Tests for the shared organization-branding module."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import branding


class FakeDB:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def _tmp_png():
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n\x1a\n")
    os.close(fd)
    return path


def test_org_name_defaults_to_empty():
    assert branding.get_org_name(FakeDB()) == ""
    assert branding.get_org_name(FakeDB({branding.ORG_NAME_KEY: "  Acme Co-op  "})) == "Acme Co-op"


def test_resolve_logo_override_wins():
    override = _tmp_png()
    configured = _tmp_png()
    try:
        db = FakeDB({branding.ORG_LOGO_KEY: configured})
        assert branding.resolve_logo_path(db, override) == override
        assert branding.resolve_logo_path(db) == configured
    finally:
        os.remove(override)
        os.remove(configured)


def test_resolve_logo_missing_file_falls_back_to_bundled():
    db = FakeDB({branding.ORG_LOGO_KEY: "/nonexistent/logo.png"})
    assert branding.resolve_logo_path(db) == branding.bundled_logo_path()


def test_image_data_url_roundtrip_and_missing():
    path = _tmp_png()
    try:
        url = branding.image_data_url(path)
        assert url.startswith("data:image/png;base64,")
    finally:
        os.remove(path)
    assert branding.image_data_url("/nonexistent.png") == ""
    assert branding.image_data_url(None) == ""


def test_letterhead_includes_org_and_title():
    db = FakeDB({branding.ORG_NAME_KEY: "Acme Co-op"})
    html = branding.letterhead_html(db, "Quarterly Interest Report", "Period: Jan - Mar 2026")
    assert "Acme Co-op" in html
    assert "Quarterly Interest Report" in html
    assert "Period: Jan - Mar 2026" in html


def test_letterhead_omits_missing_elements():
    html = branding.letterhead_html(FakeDB(), "Report Title")
    assert 'class="lh-org"' not in html      # no org configured
    assert 'class="lh-subtitle"' not in html # no subtitle given
    assert "Report Title" in html
