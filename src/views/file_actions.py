"""Opening a generated file in the desktop's default application.

The same platform switch was written out three times — two report exports and
the Treasury statements — each wrapped in ``except Exception: pass``. Swallowing
it means a user who asked for a report, was told it saved, and then saw nothing
happen has no way to tell whether the file exists.

The file genuinely is saved by that point; only the *opening* fails. So the
right behaviour is not an error dialog but an honest message with the path,
which is what the user needs in order to go and find it.
"""
import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)


def open_in_default_app(path):
    """Open ``path`` with the desktop's default handler.

    Returns None on success, or a short reason it could not be opened. Never
    raises: the caller has already saved the file and the failure is cosmetic.
    """
    if not path or not os.path.exists(path):
        return "the file is no longer where it was saved"
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(path)                          # noqa: S606 - desktop handler
        elif system == "Darwin":
            subprocess.Popen(["open", path])            # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", path])        # noqa: S603,S607
        return None
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Could not open %s in the default application: %s", path, e)
        return str(e)


def report_saved(parent, title, path, what="report"):
    """Tell the user a file was saved, and open it.

    Folds the "saved" message and the open attempt together, so a failure to
    open is reported alongside the path rather than silently discarded.
    """
    from PyQt6.QtWidgets import QMessageBox

    problem = open_in_default_app(path)
    if problem is None:
        QMessageBox.information(parent, title, f"{what.capitalize()} saved to:\n{path}")
    else:
        QMessageBox.information(
            parent, title,
            f"{what.capitalize()} saved to:\n{path}\n\n"
            f"It could not be opened automatically ({problem}). "
            f"You can open it from that folder.")
