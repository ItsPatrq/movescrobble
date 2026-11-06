"""Read single key presses from a terminal (arrows, Enter, Esc, letters) without waiting for Enter."""
from __future__ import annotations

import contextlib
import os
import select
import sys

try:  # POSIX only; the shell falls back to line input where this is missing
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = tty = None

ESCAPES = {"[A": "up", "[B": "down", "[C": "right", "[D": "left", "OA": "up", "OB": "down", "OC": "right", "OD": "left",
           "[H": "home", "[F": "end"}


def available() -> bool:
    return bool(termios and sys.stdin.isatty() and sys.stdout.isatty())


@contextlib.contextmanager
def raw_mode(fd: int | None = None):
    """cbreak mode for the duration: keys arrive immediately, Ctrl-C still raises KeyboardInterrupt."""
    fd = sys.stdin.fileno() if fd is None else fd
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_key(fd: int | None = None) -> str:
    """One key as a name: up/down/left/right/home/end/enter/esc/space/backspace/tab, or the character."""
    fd = sys.stdin.fileno() if fd is None else fd
    b = os.read(fd, 1)
    if not b:
        raise EOFError
    c = b[0]
    if c == 0x1B:
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        seq = os.read(fd, 2).decode("latin-1")
        if seq in ESCAPES:
            return ESCAPES[seq]
        while seq[-1:] not in ("~", "") and not seq[-1:].isalpha():  # swallow longer sequences (Delete, PgUp ...)
            if not select.select([fd], [], [], 0.05)[0]:
                break
            seq += os.read(fd, 1).decode("latin-1")
        return ESCAPES.get(seq, "unknown")
    if c in (0x0D, 0x0A):
        return "enter"
    if c == 0x20:
        return "space"
    if c in (0x7F, 0x08):
        return "backspace"
    if c == 0x09:
        return "tab"
    if c == 0x03:
        raise KeyboardInterrupt
    if c == 0x04:
        raise EOFError
    if c >= 0xC0:  # multi-byte UTF-8 character
        need = 1 if c < 0xE0 else 2 if c < 0xF0 else 3
        return (b + os.read(fd, need)).decode("utf-8", "replace")
    return b.decode("latin-1")
