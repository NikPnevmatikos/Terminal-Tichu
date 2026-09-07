"""Minimal ANSI color helpers (no dependencies, honours NO_COLOR)."""

from __future__ import annotations

import os
import sys

_enabled = os.environ.get("TICHU_FORCE_COLOR") is not None or (
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


def enabled() -> bool:
    return _enabled


def _wrap(code: str, text: str) -> str:
    if not _enabled:
        return text
    # A nested style ends with a reset that would also cancel *this* style
    # for the rest of the text (a green name inside a dim line): re-arm it
    # after every reset the text already contains.
    text = text.replace("\x1b[0m", f"\x1b[0m\x1b[{code}m")
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
def bgreen(t): return _wrap("1;32", t)
def byellow(t): return _wrap("1;33", t)
def on_green(t): return _wrap("1;97;42", t)  # bold white on a green block
def on_red(t): return _wrap("1;97;41", t)    # bold white on a red block


# Whole-window flash. OSC 11 repaints the terminal's default background and
# OSC 111 restores it - understood by xterm, Windows Terminal, VTE (GNOME
# Terminal), iTerm2, kitty, Alacritty, WezTerm and xterm.js; terminals that
# don't know the pair simply ignore it. The web gateway turns the very same
# sequence into a flash of the page.
FLASH_COLORS = {"green": "#2ea043", "red": "#da3633"}


def flash_on(color: str) -> str:
    return f"\x1b]11;{FLASH_COLORS[color]}\x07" if _enabled else ""


def flash_off() -> str:
    return "\x1b]111\x07" if _enabled else ""
