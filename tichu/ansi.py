"""Minimal ANSI color helpers (no dependencies, honours NO_COLOR)."""

from __future__ import annotations

import os
import sys

_enabled = (
    hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
    and os.environ.get("NO_COLOR") is None
    and os.environ.get("TERM") != "dumb"
)

if os.name == "nt":  # enable VT processing in legacy Windows consoles
    os.system("")


def set_enabled(on: bool) -> None:
    global _enabled
    _enabled = on


def _wrap(code: str, text: str) -> str:
    if not _enabled:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def bold(t): return _wrap("1", t)
def dim(t): return _wrap("2", t)
def red(t): return _wrap("31", t)
def green(t): return _wrap("32", t)
def yellow(t): return _wrap("33", t)
def blue(t): return _wrap("34", t)
def magenta(t): return _wrap("35", t)
def cyan(t): return _wrap("36", t)
def white(t): return _wrap("97", t)
def bred(t): return _wrap("1;31", t)
def byellow(t): return _wrap("1;33", t)
