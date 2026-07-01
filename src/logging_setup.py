"""Application-wide logging and crash reporting for LoanMaster.

Call :func:`setup_logging` once at startup (before any window is created).
Modules then log through the stdlib pattern::

    import logging
    logger = logging.getLogger(__name__)

Logs go to a rotating file in the per-user state directory (so they survive
in the packaged .exe where stdout is invisible) and are echoed to stderr
when a console is attached.
"""
import logging
import logging.handlers
import os
import sys
import traceback

APP_NAME = "LoanMaster"

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_LOG_MAX_BYTES = 1_000_000
_LOG_BACKUP_COUNT = 5

_log_file_path = None


def get_log_dir() -> str:
    """Return the per-user log directory, creating it if needed."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        log_dir = os.path.join(base, APP_NAME, "logs")
    elif sys.platform == "darwin":
        log_dir = os.path.expanduser(f"~/Library/Logs/{APP_NAME}")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        log_dir = os.path.join(base, APP_NAME.lower(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def get_log_file() -> str | None:
    """Path of the active log file, or None if setup_logging never ran."""
    return _log_file_path


def setup_logging(level: int = logging.INFO) -> str:
    """Configure root logging (rotating file + console). Returns the log path.

    Safe to call more than once; handlers are only installed the first time.
    """
    global _log_file_path
    root = logging.getLogger()
    if _log_file_path is not None:
        return _log_file_path

    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(_LOG_FORMAT)

    _log_file_path = os.path.join(get_log_dir(), f"{APP_NAME.lower()}.log")
    file_handler = logging.handlers.RotatingFileHandler(
        _log_file_path, maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    return _log_file_path


def install_crash_handler() -> None:
    """Route uncaught exceptions to the log and show the user an error dialog.

    Without this, an uncaught exception in a Qt slot kills the packaged app
    with no trace. The dialog tells the user where the log file is and keeps
    the app running when possible.
    """
    logger = logging.getLogger(APP_NAME)

    def _handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical(
            "Uncaught exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        _show_crash_dialog(exc_type, exc_value, exc_tb)

    sys.excepthook = _handle


def _show_crash_dialog(exc_type, exc_value, exc_tb) -> None:
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is None:
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(f"{APP_NAME} - Unexpected Error")
        box.setText(
            "An unexpected error occurred. Your data was not lost, but the "
            "last action may not have completed.\n\n"
            f"{exc_type.__name__}: {exc_value}"
        )
        if _log_file_path:
            box.setInformativeText(f"Details were written to:\n{_log_file_path}")
        box.setDetailedText(detail)
        box.exec()
    except Exception:
        # Crash reporting must never crash.
        logging.getLogger(APP_NAME).exception("Failed to show crash dialog")


def install_qt_message_handler() -> None:
    """Send Qt's own warnings (qWarning etc.) into the Python log."""
    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return

    qt_logger = logging.getLogger("qt")
    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _handler(msg_type, context, message):
        qt_logger.log(level_map.get(msg_type, logging.WARNING), message)

    qInstallMessageHandler(_handler)
