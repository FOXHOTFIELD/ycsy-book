"""End-to-end UI tests that do not need a terminal.

``selftest`` drives the real ``Live`` object into a small VT emulator that
scrolls, so layout bugs (a frame taller than the window leaving stale title
lines) fail here instead of in somebody's download.
"""

from rich.console import Group
from rich.text import Text

from ycsy import selftest


def test_view_fits_every_window_size_and_charset():
    failures = []
    for width, height in selftest.SIZES:
        for charset in selftest.CHARSETS:
            for art in (False, True):
                failures.extend(selftest.check(width, height, charset, art))
    assert failures == [], "\n".join(failures)


def test_selftest_itself_detects_overflow(monkeypatch):
    """Canary: if the harness cannot see a too-tall frame, the test above lies."""
    from ycsy.tui import Tui

    def overflow(self):
        return Group(*[Text("line %d" % index) for index in range(30)])

    monkeypatch.setattr(Tui, "_view", overflow)
    failures = selftest.check(60, 20, "unicode", False)
    assert failures, "溢出没有被检测出来"
    assert "标题" in failures[0]


def test_only_one_title_line_after_three_refreshes():
    screen, _ = selftest.render(100, 30, "unicode", False, refreshes=3)
    titles = [line for line in screen.text() if "远程实验预约值守" in line]
    assert len(titles) == 1


def test_ascii_mode_never_uses_ambiguous_width_characters():
    screen, _ = selftest.render(100, 30, "ascii", False, refreshes=1)
    for line in screen.text():
        for char in line:
            if ord(char) < 128 or 0x4E00 <= ord(char) <= 0x9FFF:
                continue
            assert char in "，。：；、（）【】“”‘’《》…！？·—", (char, line)

def test_grid_never_wraps_a_cell():
    """Every row of the slot grid must hold whole cells - a wrapped 约 is a bug."""
    import re

    cell = re.compile(r"\d\d:\d\d [过空约用]")
    for width, height in ((91, 30), (100, 30), (120, 40), (80, 24)):
        screen, _ = selftest.render(width, height, "unicode", False, refreshes=1)
        rows = [len(cell.findall(line)) for line in screen.text()]
        rows = [count for count in rows if count]
        assert rows, width
        full = max(rows)
        assert rows[:-1] == [full] * (len(rows) - 1), (width, rows)
        assert sum(rows) == 39, (width, sum(rows))

def test_desk_picker_fits_small_windows():
    """The desk picker is short, but it must still fit and never wrap."""
    from ycsy.config import Config as Cfg
    from ycsy.model import Desk, Experiment
    from ycsy.tui import Tui

    tui = Tui(Cfg())
    tui.mode = "desk"
    tui.desk_data = {
        "slot": "2026-11-08 18:00",
        "experiment": Experiment(id=206, name="组合电路", limit_count=100, limit_time=20,
                                 lab_start="09:00", lab_end="22:00"),
        "desks": [Desk(id=index, name="hdu10-%02d" % index, flag=index % 2)
                  for index in range(1, 9)],
        "mine": None,
    }
    for width, height in ((80, 24), (60, 20), (120, 40)):
        screen, _ = selftest.render(width, height, "unicode", False, refreshes=1,
                                    view=tui._view_desk())
        lines = [line for line in screen.text() if line.strip()]
        assert lines, (width, height)
        assert max(len(line) for line in lines) <= width
        assert len(lines) <= height
        assert any("选择台位" in line for line in lines)
