"""Shared test configuration.

Force Qt's offscreen platform before any PyQt6 import so the suite runs
headlessly (CI containers, SSH sessions) without a display server.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
