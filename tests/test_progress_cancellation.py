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
        for match in re.finditer(r"progress\.close\(\)", text):
            segment = text[match.end():match.end() + 400].split("\n    def ")[0]
            hit = segment.find(".wasCanceled()")
            if hit == -1:
                continue
            # A `return`, or the start of a loop, between the two means they are
            # different code paths — an early exit followed by the loop that
            # legitimately polls the dialog. Only a straight-line close-then-ask
            # is the mistake.
            between = segment[:hit]
            if re.search(r"\breturn\b|\bwhile\b|\bfor\b", between):
                continue
            offenders.append(f"{name} near offset {match.start()}")
    assert not offenders, (
        "wasCanceled() is being asked after close(), which always returns "
        f"True: {offenders}")
