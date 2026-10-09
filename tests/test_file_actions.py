"""Opening a saved file, and saying so when it cannot be opened.

The same platform switch was written out three times, twice wrapped in
`except Exception: pass` and once with no handling at all — so on a desktop with
no handler for the file type, two call sites went silent and the third raised
into the GUI after the file had already been written.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.views import file_actions


def test_a_missing_file_is_reported_not_opened(tmp_path):
    problem = file_actions.open_in_default_app(str(tmp_path / "nope.pdf"))
    assert problem and "no longer" in problem


def test_no_path_is_reported(tmp_path):
    assert file_actions.open_in_default_app("") is not None
    assert file_actions.open_in_default_app(None) is not None


def test_a_handler_failure_is_returned_not_raised(tmp_path, monkeypatch):
    """The file is already saved by this point, so failing to open it must not
    raise — the caller needs to keep showing the path."""
    target = tmp_path / "report.pdf"
    target.write_text("x")

    def boom(*a, **k):
        raise OSError("no application registered")

    monkeypatch.setattr(file_actions.subprocess, "Popen", boom)
    monkeypatch.setattr(file_actions.os, "startfile", boom, raising=False)

    problem = file_actions.open_in_default_app(str(target))
    assert problem == "no application registered"


def test_a_successful_open_reports_nothing(tmp_path, monkeypatch):
    target = tmp_path / "report.pdf"
    target.write_text("x")
    opened = []
    monkeypatch.setattr(file_actions.subprocess, "Popen", lambda a, **k: opened.append(a))
    monkeypatch.setattr(file_actions.os, "startfile", lambda p: opened.append(p),
                        raising=False)
    monkeypatch.setattr(file_actions.platform, "system", lambda: "Linux")

    assert file_actions.open_in_default_app(str(target)) is None
    assert opened == [["xdg-open", str(target)]]
