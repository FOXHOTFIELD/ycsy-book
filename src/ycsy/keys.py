"""Single-key input that works on Windows and on POSIX terminals."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional

_WIN_ARROWS = {"H": "up", "P": "down", "K": "left", "M": "right"}
_POSIX_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


@dataclass(frozen=True)
class Key:
    name: str
    char: str = ""


class KeyReader:
    """Context manager that puts the terminal into a key-at-a-time mode."""

    def __init__(self) -> None:
        self._saved = None

    def __enter__(self) -> "KeyReader":
        if os.name != "nt":
            try:
                import termios
                import tty

                self._saved = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self._saved = None
        return self

    def __exit__(self, *exc) -> bool:
        if self._saved is not None:
            try:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        return False

    def read(self, timeout: float = 0.15) -> Optional[Key]:
        if os.name == "nt":
            return self._read_windows(timeout)
        return self._read_posix(timeout)

    # ----------------------------------------------------------- windows
    @staticmethod
    def _read_windows(timeout: float) -> Optional[Key]:
        import msvcrt
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    return Key(_WIN_ARROWS.get(code, "unknown"))
                return _normalise(char)
            time.sleep(0.02)
        return None

    # ------------------------------------------------------------- posix
    @staticmethod
    def _read_posix(timeout: float) -> Optional[Key]:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return None
        char = sys.stdin.read(1)
        if char == "\x1b":
            more, _, _ = select.select([sys.stdin], [], [], 0.01)
            if more:
                seq = sys.stdin.read(2)
                if seq.startswith("["):
                    return Key(_POSIX_ARROWS.get(seq[1:2], "unknown"))
            return Key("esc")
        return _normalise(char)


def _normalise(char: str) -> Key:
    if char in ("\r", "\n"):
        return Key("enter")
    if char in ("\x08", "\x7f"):
        return Key("backspace")
    if char == " ":
        return Key("space")
    if char == "\x1b":
        return Key("esc")
    if char in ("\x03",):
        return Key("ctrl-c")
    return Key(char.lower(), char)
