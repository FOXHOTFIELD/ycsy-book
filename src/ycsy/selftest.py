"""Headless UI self-test.

The bugs that kept escaping were layout bugs: a frame taller than the window
makes ``Live`` scroll, and the cursor maths then leaves stale lines behind. You
cannot catch that by rendering once into a ``Console(record=True)`` - you need a
terminal that actually scrolls.

So this module carries a tiny VT screen (just enough of cursor movement, line
erase and scrolling for rich's output) and drives the real ``Live`` object with
the real view, three refreshes in a row, at several window sizes. Then it
asserts what a human would notice:

* exactly one title line (stale frames would pile up),
* no line wider than the window,
* no frame taller than the window,
* the glyph set matches the requested charset.

``ycsy selftest`` runs it in the released executable, so CI catches a broken
build before anybody downloads it.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

from rich.console import Console
from rich.live import Live

SIZES = ((80, 24), (100, 30), (120, 40), (60, 20), (140, 45))
CHARSETS = ("unicode", "ascii")

_CSI = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")


class Screen:
    """Minimal VT100 screen: enough for rich's Live output."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(10, width)
        self.height = max(5, height)
        self.lines: List[List[str]] = [[" "] * self.width for _ in range(self.height)]
        self.row = 0
        self.col = 0

    # ------------------------------------------------------------------ io
    def _scroll(self) -> None:
        self.lines.pop(0)
        self.lines.append([" "] * self.width)
        self.row = self.height - 1

    def _newline(self) -> None:
        # a cooked terminal turns LF into CR LF, and rich relies on that
        self.col = 0
        if self.row + 1 >= self.height:
            self._scroll()
        else:
            self.row += 1

    def _put(self, char: str) -> None:
        if self.col >= self.width:
            self.col = 0
            self._newline()
        self.lines[self.row][self.col] = char
        self.col += 1

    def feed(self, data: str) -> None:
        index = 0
        while index < len(data):
            char = data[index]
            if char == "\x1b":
                match = _CSI.match(data, index)
                if not match:
                    index += 1
                    continue
                self._csi(match.group(1) or "", match.group(2))
                index = match.end()
                continue
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self._newline()
            elif char == "\x08":
                self.col = max(0, self.col - 1)
            elif char == "\t":
                self.col = min(self.width - 1, (self.col // 8 + 1) * 8)
            elif char >= " ":
                self._put(char)
            index += 1

    def _csi(self, params: str, final: str) -> None:
        if params.startswith("?"):
            return                                   # cursor visibility, ignore
        numbers = [int(p) for p in params.split(";") if p.isdigit()]
        count = numbers[0] if numbers else 1
        if final == "A":
            self.row = max(0, self.row - count)
        elif final == "B":
            self.row = min(self.height - 1, self.row + count)
        elif final == "C":
            self.col = min(self.width - 1, self.col + count)
        elif final == "D":
            self.col = max(0, self.col - count)
        elif final == "K":
            start = 0 if count in (0, 2) else self.col
            for col in range(start, self.width):
                self.lines[self.row][col] = " "
            if count == 2:
                self.lines[self.row] = [" "] * self.width
        elif final == "J":
            if count == 2:
                self.lines = [[" "] * self.width for _ in range(self.height)]
                self.row = self.col = 0
        elif final in "Hf":
            row = (numbers[0] - 1) if numbers else 0
            col = (numbers[1] - 1) if len(numbers) > 1 else 0
            self.row = max(0, min(self.height - 1, row))
            self.col = max(0, min(self.width - 1, col))

    # --------------------------------------------------------------- read
    def text(self) -> List[str]:
        return ["".join(line).rstrip() for line in self.lines]

    def dump(self) -> str:
        return "\n".join(self.text())


class _Sink:
    """File-like object rich writes into; we keep the raw stream."""

    encoding = "utf-8"

    def __init__(self) -> None:
        self.parts: List[str] = []

    def write(self, data: str) -> int:
        self.parts.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def getvalue(self) -> str:
        return "".join(self.parts)


def sample_state() -> Dict:
    """A fixed snapshot: 39 slots, two bookings, one running - no network needed."""
    cells = []
    for index in range(39):
        minute = 9 * 60 + index * 20
        stamp = "%02d:%02d" % (minute // 60, minute % 60)
        if index < 30:
            state, past = "past", True
        elif index == 30:
            state, past = "started", False
        elif index in (31, 32):
            state, past = "booked", False
        else:
            state, past = "free", False
        cells.append({"time": "2026-10-19 " + stamp, "state": state,
                      "past": past, "desk": 36 if state != "free" else None})
    return {
        "now": "2026-10-19 19:20:40",
        "user": {"login": "25040514", "name": "胡乐添"},
        "keep": 2, "active": 2, "max_keep": 3, "step": 20,
        "lab": {"name": "数电10代", "start": "09:00", "end": "22:00"},
        "cells": cells,
        "current": cells[30],
        "next": cells[31],
        "appointments": [
            {"id": 1, "time": cells[31]["time"], "experiment": 206, "name": "组合电路", "desk": 36},
            {"id": 2, "time": cells[32]["time"], "experiment": 114, "name": "硬件测试", "desk": 37},
        ],
        "quota": [{"experiment": 206, "name": "组合电路", "limit": 100, "used": 20, "left": 80}],
        "override": {},
        "runtime": {"left_seconds": 14399},
        "session": {"desk_id": 36},
    }


def render(width: int, height: int, charset: str, art: bool,
           refreshes: int = 3) -> Tuple[Screen, "Tui"]:      # noqa: F821 - forward ref
    """Drive the real Live object into a virtual terminal."""
    from .config import Config
    from .tui import Tui

    cfg = Config(username="25040514", password="x")
    cfg.charset = charset
    cfg.show_art = art
    tui = Tui(cfg)
    tui.state = sample_state()
    tui.message = "值守已启动"
    tui._width = lambda: width - 1

    # Simulate the Windows failure mode: rich is told the window is taller than
    # it really is, so a frame that trusts console.height spills and scrolls.
    claimed_height = height + 6
    from . import console as console_mod

    real_size = os.get_terminal_size
    real_window = console_mod.window_size
    os.get_terminal_size = lambda *a, **k: os.terminal_size((width, height))
    console_mod.window_size = lambda: (width, height)
    sink = _Sink()
    console = Console(file=sink, width=width, height=claimed_height,
                      force_terminal=True, legacy_windows=False,
                      color_system=None, soft_wrap=False)
    screen = Screen(width, height)
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            for index in range(refreshes):
                tui.state["now"] = "2026-10-19 19:20:%02d" % (40 + index)
                live.update(tui._view())
                live.refresh()
                screen.feed(sink.getvalue())
                sink.parts.clear()
    finally:
        os.get_terminal_size = real_size
        console_mod.window_size = real_window
    return screen, tui


def check(width: int, height: int, charset: str, art: bool) -> List[str]:
    failures: List[str] = []
    screen, tui = render(width, height, charset, art)
    lines = screen.text()

    headers = sum(1 for line in lines if "远程实验预约值守" in line)
    if headers != 1:
        failures.append("%dx%d %s art=%s: 标题出现 %d 次（应 1 次）"
                        % (width, height, charset, art, headers))

    widest = max((len(line) for line in lines), default=0)
    if widest > width:
        failures.append("%dx%d: 有 %d 列，超过窗口宽度" % (width, height, widest))

    frame = [line for line in lines if line.strip()]
    if len(frame) > height:
        failures.append("%dx%d: 画面 %d 行，超过窗口高度" % (width, height, len(frame)))

    if charset == "ascii":
        # ASCII is always one cell and Han characters always two; anything else
        # belongs to the ambiguous-width class this mode promises to avoid
        cjk_punctuation = set("，。：；、（）【】“”‘’《》…！？·—")
        for line in lines:
            offending = [ch for ch in line
                         if ord(ch) >= 128 and not 0x4E00 <= ord(ch) <= 0x9FFF
                         and ch not in cjk_punctuation]
            if offending:
                failures.append("%dx%d ascii 模式出现宽度不确定的字符 %r"
                                % (width, height, offending[0]))
                break
    if charset == "unicode" and tui.g.rule != "─":
        failures.append("%dx%d: unicode 模式没拿到框线字符" % (width, height))
    return failures


def run(verbose: bool = True) -> int:
    failures: List[str] = []
    checked = 0
    for width, height in SIZES:
        for charset in CHARSETS:
            for art in (False, True):
                checked += 1
                failures.extend(check(width, height, charset, art))
    if verbose:
        try:
            from .console import window_size
            from rich.console import Console as _Console

            probe = _Console()
            rows = window_size()
            print("console: window_size=%s rich=(%sx%s) is_terminal=%s"
                  % (rows, probe.width, probe.height, probe.is_terminal))
        except Exception as exc:                     # never fail the run
            print("console: probe failed: %s" % exc)
        if failures:
            for item in failures:
                print("FAIL  " + item)
        else:
            print("selftest OK：%d 种组合（窗口尺寸 x 字符集 x 像素画）" % checked)
    return 1 if failures else 0
