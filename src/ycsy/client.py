"""Starting and stopping the remote-desktop client, per operating system.

The web UI opens a ``VA://`` link, which works through a protocol handler
registered by the client installer. Handing that link to the browser is
unreliable (it can silently do nothing), so on Windows we read the handler
from the registry and spawn the executable directly.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

WINDOWS_CLIENT = "AR.EXE"
POSIX_CLIENT = "AR"


def _windows_client_exe() -> Optional[str]:
    try:
        import winreg
    except ImportError:  # not on Windows
        return None
    candidates = (
        (winreg.HKEY_CLASSES_ROOT, r"VA\shell\open\command"),
        (winreg.HKEY_CURRENT_USER, r"Software\Classes\VA\shell\open\command"),
    )
    for root, path in candidates:
        try:
            with winreg.OpenKey(root, path) as key:
                command = str(winreg.QueryValue(key, "") or "").strip()
        except OSError:
            continue
        if not command:
            continue
        if command.startswith('"'):
            end = command.find('"', 1)
            exe = command[1:end] if end > 0 else ""
        else:
            exe = command.split(" ")[0]
        if exe and os.path.exists(exe):
            return exe
    return None


def open_remote(url: str, log=print) -> bool:
    """Open a remote-desktop link; returns True when a client was started."""
    if not url:
        return False
    if os.name == "nt":
        exe = _windows_client_exe()
        if exe:
            try:
                subprocess.Popen([exe, url], close_fds=True)
                log("已拉起客户端：%s" % exe)
                return True
            except OSError as exc:
                log("拉起客户端失败（%s），改用系统默认程序" % exc)
        import webbrowser

        return bool(webbrowser.open(url))
    opener = "open" if os.uname().sysname == "Darwin" else "xdg-open"
    try:
        subprocess.Popen([opener, url], close_fds=True)
        return True
    except OSError as exc:
        log("打开失败：%s" % exc)
        return False


def close_remote(log=print, name: str = WINDOWS_CLIENT) -> bool:
    """Terminate the client process if it is running."""
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/IM", name, "/F"],
                                capture_output=True, text=True,
                                encoding="gbk", errors="replace")
        text = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == 0:
            log("已关闭客户端")
            return True
        log("客户端没在运行（%s）" % (text[:120] or "unknown"))
        return False
    result = subprocess.run(["pkill", "-f", POSIX_CLIENT], capture_output=True, text=True)
    if result.returncode == 0:
        log("已关闭客户端")
        return True
    log("客户端没在运行")
    return False
