"""Character sets for the UI.

A terminal lays out text by cell count, not by what the font actually draws.
ASCII is always one cell wide, Han characters are always two, but the
"decorative" characters this UI likes - ``─ ▍ ○ ● · — ↑↓←→`` - live in the
East-Asian *ambiguous* width class: some fonts draw them one cell wide, some
two, some not at all. That is how a picture or a rule ends up sheared by a
couple of columns on somebody else's machine.

Two modes keep this predictable:

``unicode``
    Nicer: box-drawing rules, half-block mascot. Fine on any terminal whose font
    covers those code points at the expected width.

``ascii``
    Only characters with a guaranteed width: ASCII plus Han. No mascot, no box
    drawing, no arrows. Layout is identical on every font that can show Chinese.

Selection: ``charset`` in the config (``auto`` / ``unicode`` / ``ascii``), or
the ``YCSY_CHARSET`` environment variable, which wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MODES = ("auto", "unicode", "ascii")

# One arrow per key, in the order the keys are printed on screen, so the hint
# reads left-to-right exactly like "h j k l" moves.
ARROW_FOR_KEY = {"h": "←", "j": "↓", "k": "↑", "l": "→"}
ARROW_FOR_KEY_ASCII = {"h": "左", "j": "下", "k": "上", "l": "右"}

ARROWS = "".join(ARROW_FOR_KEY[key] for key in "hjkl")
ARROWS_ASCII = "".join(ARROW_FOR_KEY_ASCII[key] for key in "hjkl")


@dataclass(frozen=True)
class Glyphs:
    name: str
    rule: str
    heading: str
    off: str
    on: str
    dot: str
    dash: str
    arrows: str
    check: str
    cross: str
    range: str
    art: bool


UNICODE = Glyphs(
    name="unicode",
    rule="─",
    heading="▍ ",
    off="○ ",
    on="● ",
    dot="·",
    dash="—",
    arrows=ARROWS,
    check="✓",
    cross="✗",
    range="–",
    art=True,
)

ASCII = Glyphs(
    name="ascii",
    rule="-",
    heading="| ",
    off="[ ] ",
    on="[*] ",
    dot="|",
    dash="-",
    arrows=ARROWS_ASCII,
    check="OK",
    cross="X",
    range="-",
    art=False,
)


def _console_code_page() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())
    except Exception:
        return 0


def _locale_is_utf8() -> bool:
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = (os.environ.get(name) or "").upper()
        if "UTF-8" in value or "UTF8" in value:
            return True
    return False


def resolve(preference: str = "auto") -> Glyphs:
    """Pick a glyph set. ``auto`` keeps the pretty one unless the console is not UTF-8."""
    choice = (os.environ.get("YCSY_CHARSET") or preference or "auto").strip().lower()
    if choice == "ascii":
        return ASCII
    if choice == "unicode":
        return UNICODE
    if os.name == "nt":
        # 65001 means the console already speaks UTF-8; anything else is a legacy
        # code page whose font often lacks the box glyphs
        return UNICODE if _console_code_page() in (65001, 0) else ASCII
    return UNICODE if _locale_is_utf8() or not os.environ.get("LANG") else ASCII
