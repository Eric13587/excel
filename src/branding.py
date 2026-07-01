"""Organization identity shared by every printed/exported document.

The organization's name and logo are stored in the journal's settings table
(keys ``org_name`` and ``org_logo_path``) so each journal carries its own
branding. Documents fall back to the bundled logo when nothing is
configured, which keeps existing installations looking the way they always
have.
"""
import base64
import logging
import os
import sys

logger = logging.getLogger(__name__)

ORG_NAME_KEY = "org_name"
ORG_LOGO_KEY = "org_logo_path"


def bundled_logo_path():
    """Absolute path of the logo shipped in resources/, or None."""
    if getattr(sys, 'frozen', False):
        base_path = os.path.dirname(sys.executable)
    else:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base_path, "resources", "nairobi_water_sacco_logo.png")
    return path if os.path.exists(path) else None


def get_org_name(db):
    """The configured organization name, or '' when not set."""
    try:
        return (db.get_setting(ORG_NAME_KEY, "") or "").strip()
    except Exception:
        return ""


def resolve_logo_path(db=None, override=None):
    """Effective logo path: explicit override > journal setting > bundled."""
    if override and os.path.exists(override):
        return override
    if db is not None:
        try:
            configured = db.get_setting(ORG_LOGO_KEY, "") or ""
        except Exception:
            configured = ""
        if configured and os.path.exists(configured):
            return configured
        if configured:
            logger.warning("Configured logo not found on disk: %s", configured)
    return bundled_logo_path()


def image_data_url(path):
    """Inline a logo file as a base64 data URL for embedding in HTML."""
    if not path or not os.path.exists(path):
        return ""
    mime = {
        '.jpg': "image/jpeg", '.jpeg': "image/jpeg",
        '.gif': "image/gif", '.svg': "image/svg+xml",
    }.get(os.path.splitext(path)[1].lower(), "image/png")
    try:
        with open(path, "rb") as f:
            return f"data:{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"
    except Exception as e:
        logger.warning("Failed to encode image %s as base64: %s", path, e)
        return ""


# Excel export palette — the spreadsheet counterpart of LETTERHEAD_CSS, so
# workbooks and PDFs read as one product. The header/total backgrounds can
# still be overridden per journal via the excel_header_bg / excel_total_bg
# settings (Excel Format dialog); these are the defaults.
EXCEL_ACCENT_BG = "#2563EB"   # blue-600 — title bands (white text)
EXCEL_HEADER_BG = "#E2E8F0"   # slate-200 — column header rows
EXCEL_TOTAL_BG = "#F1F5F9"    # slate-100 — total/summary rows
EXCEL_MUTED_TEXT = "#64748B"  # slate-500 — annotations/footnotes

# One CSS block shared by every exported document, so statements and reports
# read as a single product. Palette matches the app theme (slate + blue).
LETTERHEAD_CSS = """
    .lh-header { display: flex; align-items: center; gap: 14px;
                 border-bottom: 3px solid #2563EB; padding-bottom: 10px;
                 margin-bottom: 16px; }
    .lh-logo { max-height: 64px; max-width: 160px; }
    .lh-org { font-size: 20px; font-weight: bold; color: #0f172a;
              letter-spacing: 0.5px; }
    .lh-title { font-size: 14px; color: #2563EB; font-weight: bold;
                text-transform: uppercase; margin-top: 2px; }
    .lh-subtitle { font-size: 11px; color: #64748b; margin-top: 2px; }
"""


def letterhead_html(db, title, subtitle="", logo_override=None):
    """Standard document header: logo + organization name + title.

    Degrades gracefully — missing logo or organization name simply drops
    that element rather than leaving a hole.
    """
    logo_url = image_data_url(resolve_logo_path(db, logo_override))
    org_name = get_org_name(db)
    logo_html = f'<img src="{logo_url}" class="lh-logo">' if logo_url else ""
    org_html = f'<div class="lh-org">{org_name}</div>' if org_name else ""
    subtitle_html = f'<div class="lh-subtitle">{subtitle}</div>' if subtitle else ""
    return f"""
    <div class="lh-header">
        {logo_html}
        <div>
            {org_html}
            <div class="lh-title">{title}</div>
            {subtitle_html}
        </div>
    </div>"""
