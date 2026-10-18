"""Local state: token cache, metadata cache, last session, started log, lock.

Everything lives under the user cache dir, never next to the code, so the
repository can never accidentally commit a token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def redact(text: str, keep: int = 12) -> str:
    """Shorten secrets before they reach a log line.

    The remote-desktop link embeds an encrypted credential blob; printing it in
    full is the same as printing the password.
    """
    if not text:
        return text
    out = []
    for word in str(text).split():
        if word.lower().startswith("va://") and len(word) > keep + 8:
            out.append(word[: keep] + "...<redacted %d chars>" % (len(word) - keep))
        else:
            out.append(word)
    return " ".join(out)


class Store:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- helpers
    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read(self, name: str, default: Any = None) -> Any:
        path = self._path(name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data: Any) -> None:
        path = self._path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # --------------------------------------------------------------- token
    @property
    def token(self) -> str:
        data = self._read("token.json", {}) or {}
        return str(data.get("token") or "")

    @token.setter
    def token(self, value: str) -> None:
        self._write("token.json", {"token": value})

    # --------------------------------------------------------------- cache
    def cache(self) -> Dict[str, Any]:
        return self._read("cache.json", {}) or {}

    def save_cache(self, data: Dict[str, Any]) -> None:
        self._write("cache.json", data)

    # ------------------------------------------------------------- session
    def session(self) -> Dict[str, Any]:
        return self._read("session.json", {}) or {}

    def save_session(self, data: Dict[str, Any]) -> None:
        self._write("session.json", data)

    # ------------------------------------------------------------- started
    def started(self) -> List[Dict[str, Any]]:
        data = self._read("started.json", [])
        return data if isinstance(data, list) else []

    def record_started(self, entry: Dict[str, Any]) -> None:
        rows = [r for r in self.started() if str(r.get("id")) != str(entry.get("id"))]
        rows.append(entry)
        self._write("started.json", rows[-200:])

    def started_today(self, day: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for row in self.started():
            when = str(row.get("time") or "")
            if when.startswith(day):
                out[when] = row
        return out


class FileLock:
    """Advisory lock held for the lifetime of the process.

    Uses msvcrt on Windows and flock elsewhere; both are released by the OS if
    the process dies, so a crash can never leave a permanent lock behind.

    The pid is written to a *separate* plain file: the lock file itself is
    exclusive, and ``msvcrt.locking`` makes it unreadable for everybody else -
    which is why the UI used to report "not running" while a watcher was up.
    """

    def __init__(self, path: Path, pid_path: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.pid_path = Path(pid_path) if pid_path else None
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise SystemExit("已有另一个值守进程在运行，本次退出")
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        if self.pid_path is not None:
            try:
                self.pid_path.write_text(str(os.getpid()), encoding="utf-8")
            except OSError:
                pass
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self.pid_path is not None:
            try:
                self.pid_path.unlink()
            except OSError:
                pass
        if self._fh:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()
            self._fh = None
        return False


def read_pid(path: Path) -> Optional[int]:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip() or 0) or None
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    """Is this process still running?

    On Windows ``os.kill(pid, 0)`` is *not* a probe - it calls TerminateProcess -
    so the liveliness check has to go through OpenProcess/GetExitCodeProcess.
    """
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes

    query_limited = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    handle = kernel32.OpenProcess(query_limited, False, int(pid))
    if not handle:
        # ERROR_ACCESS_DENIED means the process exists but we may not query it;
        # ERROR_INVALID_PARAMETER means there is no such process.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def live_pid(path: Path) -> Optional[int]:
    """Read the pid file; drop it if the process is gone."""
    pid = read_pid(path)
    if not pid:
        return None
    if pid_alive(pid):
        return pid
    try:
        Path(path).unlink()
    except OSError:
        pass
    return None
