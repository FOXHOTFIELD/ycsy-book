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
