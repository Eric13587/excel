"""Turning an exception into something a user can act on.

Two kinds of thing go wrong in this application, and they deserve different
treatment.

**Refusals.** `PostingError`, `OperationNotAllowed`, `PeriodClosedError` and
their siblings are the software declining to do something, on purpose, with a
message written for a clerk:

    test has retired (2026-08-09), so no new savings activity can be recorded.
    Repayments and withdrawals are still allowed.

That sentence is the whole point of raising them. Reaching a terminal as a
traceback wastes it — the person sees the operation do nothing and is told
nothing.

**Faults.** Everything else is a bug. The user cannot act on the detail, but
they must still be told the operation did not happen, rather than watching a
button appear to do nothing.

WHY THIS IS CENTRAL RATHER THAN PER-CALL-SITE
---------------------------------------------
Thirty-four places in the views can raise a refusal, and guarding each one means
thirty-four chances to forget — a count that only grows as rules are added. Qt
makes it worse: an exception raised inside a slot does not propagate to the
caller, so a try/except around the code that *opened* a dialog never sees it.

`logging_setup.install_crash_handler` already routes uncaught exceptions here,
so this is the one place that decides how any of them is shown, and a rule added
tomorrow is reported properly without anyone remembering to wrap it.
"""
import logging
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.exceptions import LoanMasterError

logger = logging.getLogger(__name__)

#: Exceptions whose message is written for the person reading it. Anything
#: deriving from LoanMasterError qualifies — they are raised deliberately and
#: their text is the reason for raising them.
REFUSAL = LoanMasterError


def _active_window():
    app = QApplication.instance()
    return app.activeWindow() if app else None


def report(exc, parent=None, context=None):
    """Show one exception to the user. Returns True if it was a refusal.

    Safe to call directly from a handler that wants to carry on afterwards.
    """
    parent = parent or _active_window()
    if isinstance(exc, REFUSAL):
        logger.info("Refused: %s", exc)
        QMessageBox.information(parent, "Not Allowed", str(exc))
        return True

    logger.exception("Unhandled error%s", f" during {context}" if context else "",
                     exc_info=exc)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle("Something Went Wrong")
    box.setText("That operation could not be completed."
                + (f"\n\nWhile: {context}" if context else ""))
    box.setInformativeText(
        "Nothing has been changed by the part that failed. The details below "
        "are for whoever maintains the software.")
    box.setDetailedText("".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__)))
    box.exec()
    return False
