"""Console font introspection (Windows, best effort).

The font of a terminal belongs to the terminal program: Windows Terminal keeps
it in its own settings, legacy conhost keeps it in the window properties. A
program cannot portably change it either - modern terminals run apps behind a
pseudo console (ConPTY), where there is no font at all and
``SetCurrentConsoleFontEx`` is a no-op.

So this module only *reports* what it can see and leaves the choice to the user.
"""

from __future__ import annotations

import ctypes
import os
from typing import Optional

# Faces that render a box-drawn, mixed CJK/ASCII UI well. Anything without CJK
# glyphs makes the terminal fall back to a second font, which shifts baselines.
FONT_CANDIDATES = [
    "Sarasa Mono SC",
    "Sarasa Term SC",
    "更纱黑体 SC",
    "Cascadia Mono",
    "Consolas",
    "Lucida Console",
]

STD_OUTPUT_HANDLE = -11
_MAX_FACE = 32


class _Coord(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _FontInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("nFont", ctypes.c_ulong),
        ("dwFontSize", _Coord),
        ("FontFamily", ctypes.c_uint),
        ("FontWeight", ctypes.c_uint),
        ("FaceName", ctypes.c_wchar * _MAX_FACE),
    ]


def _api():
    """Return (kernel32, handle) or (None, None) when there is no real console."""
    if os.name != "nt":
        return None, None
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    invalid = ctypes.c_void_p(-1).value
    if handle and handle != invalid:
        return kernel32, handle

    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    direct = kernel32.CreateFileW(
        "CONOUT$", 0x80000000 | 0x40000000, 0x00000003, None, 3, 0, None)
    if not direct or direct == invalid:
        return None, None
    return kernel32, direct


def font_name() -> Optional[str]:
    """Face name of the current console window, or None when unavailable.

    Returns None under ConPTY (Windows Terminal and friends), which is expected
    rather than an error.
    """
    kernel32, handle = _api()
    if not kernel32:
        return None
    from ctypes import wintypes

    kernel32.GetCurrentConsoleFontEx.argtypes = [
        wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(_FontInfo)]
    kernel32.GetCurrentConsoleFontEx.restype = wintypes.BOOL
    info = _FontInfo()
    info.cbSize = ctypes.sizeof(_FontInfo)
    if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
        return None
    return info.FaceName or None


def clear_home() -> bool:
    """Blank the console and park the cursor at (0, 0).

    A fresh Windows console sits at the bottom of a tall screen buffer, so the
    first frame is drawn out of sight and the window shows the tail end of it.
    """
    kernel32, handle = _api()
    if not kernel32:
        try:
            print("\x1b[H\x1b[2J", end="", flush=True)
        except OSError:
            pass
        return False
    from ctypes import wintypes

    class _BufferInfo(ctypes.Structure):
        _fields_ = [("dwSize", _Coord), ("dwCursorPosition", _Coord),
                    ("wAttributes", ctypes.c_ushort), ("srWindow", ctypes.c_short * 4),
                    ("dwMaximumWindowSize", _Coord)]

    class _Rect(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                    ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    info = _BufferInfo()
    kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BufferInfo)]
    if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return False
    cells = info.dwSize.X * info.dwSize.Y
    written = wintypes.DWORD(0)
    origin = _Coord(0, 0)
    kernel32.FillConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE, wintypes.WCHAR, wintypes.DWORD, _Coord, ctypes.POINTER(wintypes.DWORD)]
    kernel32.FillConsoleOutputAttribute.argtypes = [
        wintypes.HANDLE, wintypes.WORD, wintypes.DWORD, _Coord, ctypes.POINTER(wintypes.DWORD)]
    kernel32.FillConsoleOutputCharacterW(handle, " ", cells, origin, ctypes.byref(written))
    kernel32.FillConsoleOutputAttribute(handle, info.wAttributes, cells, origin, ctypes.byref(written))
    kernel32.SetConsoleCursorPosition.argtypes = [wintypes.HANDLE, _Coord]
    kernel32.SetConsoleCursorPosition(handle, origin)
    return True


class _BufferInfo(ctypes.Structure):
    _fields_ = [("dwSize", _Coord), ("dwCursorPosition", _Coord),
                ("wAttributes", ctypes.c_ushort), ("srWindow", ctypes.c_short * 4),
                ("dwMaximumWindowSize", _Coord)]


def window_size():
    """(columns, rows) of the *visible* console window.

    ``os.get_terminal_size`` can report the screen buffer instead of the window
    on Windows, which is thousands of rows; trusting it makes the UI draw a
    frame taller than the window and scroll.
    """
    kernel32, handle = _api()
    if not kernel32:
        return None
    from ctypes import wintypes

    kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BufferInfo)]
    info = _BufferInfo()
    if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return None
    left, top = info.srWindow[0], info.srWindow[1]
    right, bottom = info.srWindow[2], info.srWindow[3]
    columns = max(1, right - left + 1)
    rows = max(1, bottom - top + 1)
    return columns, rows


def enable_vt() -> bool:
    """Turn on ANSI escape processing for this console.

    Without it rich falls back to a legacy renderer that cannot use the
    alternate screen, so every refresh paints over the same scrollback and the
    window ends up scrolled with the title out of view.
    """
    kernel32, handle = _api()
    if not kernel32:
        return False
    from ctypes import wintypes

    mode = wintypes.DWORD()
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    process_vt, processed_output = 0x0004, 0x0001
    if mode.value & process_vt:
        return True
    return bool(kernel32.SetConsoleMode(handle, mode.value | process_vt | processed_output))
