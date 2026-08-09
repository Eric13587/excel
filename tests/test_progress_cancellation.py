"""A completed operation must not report itself as cancelled.

QProgressDialog.close() sets the cancellation flag, so code that closes the
dialog and then asks wasCanceled() sees True on a perfectly successful run.
That put "Operation cancelled. Processed 8 deductions." in front of users who
had just watched the operation finish.

The rule these pin: capture the answer while the loop is still running.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QProgressDialog


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def test_close_sets_the_cancellation_flag(qt_app):
    """The Qt behaviour this guards against, asserted rather than assumed."""
    dialog = QProgressDialog("work", "Cancel", 0, 3)
    dialog.setMinimumDuration(0)
    for i in range(3):
        dialog.setValue(i + 1)
    assert dialog.wasCanceled() is False, "a completed loop is not a cancellation"

    dialog.close()
    assert dialog.wasCanceled() is True, (
        "close() sets the flag — which is why the answer must be captured "
        "before closing, not after")


def test_capturing_during_the_loop_survives_close(qt_app):
    """The pattern the views now use."""
    dialog = QProgressDialog("work", "Cancel", 0, 3)
    dialog.setMinimumDuration(0)
    cancelled = False
    for i in range(3):
        if dialog.wasCanceled():
            cancelled = True
            break
        dialog.setValue(i + 1)
    dialog.close()
    assert cancelled is False


def test_a_real_cancellation_is_still_reported(qt_app):
    """The fix must not swallow an actual cancel."""
    dialog = QProgressDialog("work", "Cancel", 0, 5)
    dialog.setMinimumDuration(0)
    cancelled = False
    for i in range(5):
        if i == 2:
            dialog.cancel()
        if dialog.wasCanceled():
            cancelled = True
            break
        dialog.setValue(i + 1)
    dialog.close()
    assert cancelled is True


def test_no_view_asks_wasCanceled_after_closing():
    """A structural guard: the pattern must not come back somewhere else."""
    import re
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "views")
    offenders = []
    for name in os.listdir(root):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(root, name), encoding="utf-8").read()
        for match in re.finditer(r"\.close\(\)", text):
            after = text[match.end():match.end() + 400]
            # a wasCanceled() check before the next def is the mistake
            segment = after.split("\n    def ")[0]
            if ".wasCanceled()" in segment:
                offenders.append(f"{name} near offset {match.start()}")
    assert not offenders, (
        "wasCanceled() is being asked after close(), which always returns "
        f"True: {offenders}")
