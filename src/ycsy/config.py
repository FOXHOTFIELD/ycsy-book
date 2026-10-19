"""Configuration loading plus the hard limits of a released build.

Lookup order (first hit wins):

1. ``--config`` / ``YCSY_CONFIG``
2. ``./config.json``
3. ``<user config dir>/config.json``

Credentials may come from the environment instead of the file:
``YCSY_USERNAME`` / ``YCSY_PASSWORD``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, List, Optional

from platformdirs import user_cache_dir, user_config_dir, user_log_dir

APP_NAME = "ycsy-book"
DEFAULT_API = "https://api.remote-lab.com"
DEFAULT_WEB = "https://www.ycsypt.com"

# Hard limits of a released build. They are not tunable from config.json:
# the point is that a careless user cannot reserve a whole day, hold a dozen
# benches at once, or leave a watcher running for a week.
MAX_KEEP = 3
MAX_RUNTIME_HOURS = 4.0


@dataclass
class Coverage:
    # only used to build the day grid; booking never crosses midnight
    days_ahead: int = 1
    day_start: str = "08:00"
    day_end: str = "22:00"
    slot_minutes: int = 0  # 0 = follow the experiment's limitTime
    lead_minutes: int = 1


@dataclass
class RunPolicy:
    keep: int = 2  # how many slots to hold: current + (keep - 1) ahead
    auto_start: bool = True
    auto_open: bool = True
    interval_sec: tuple = (3, 6)
    idle_sec: int = 300
    keep_poll_sec: int = 60
    max_book_per_pass: int = 0
    max_runtime_hours: float = MAX_RUNTIME_HOURS
    verify: bool = True


@dataclass
class Config:
    username: str = ""
    password: str = ""
    api_base: str = DEFAULT_API
    web_base: str = DEFAULT_WEB
    experiments: List[int] = field(default_factory=list)
    lab_id: int = 0
    coverage: Coverage = field(default_factory=Coverage)
    run: RunPolicy = field(default_factory=RunPolicy)
    notify: bool = True
    # Process name of the remote-desktop client, used only by "close client".
    client_process: str = "AR.EXE"
    # Corner mascot is off by default: block glyphs are font dependent.
    show_art: bool = False
    # "auto" / "unicode" / "ascii"; ascii keeps layout identical on any font.
    charset: str = "auto"
    # Optional maintenance channel. Empty means: never make that request.
    policy_urls: List[str] = field(default_factory=list)
    source: str = ""

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        candidate = _pick_config_path(path)
        data: dict = {}
        if candidate:
            with open(candidate, "r", encoding="utf-8") as fh:
                data = json.load(fh)

        # A config.json sitting next to the executable wins over the user one,
        # so a leftover template with blank credentials used to hide the real
        # account and ask for the password on every start. Only for implicit
        # discovery: an explicit --config is taken literally.
        if path is None and not str(data.get("username") or "").strip():
            home = cls.home_config_path()
            if candidate != home and home.exists():
                try:
                    with open(home, "r", encoding="utf-8") as fh:
                        fallback = json.load(fh)
                except (OSError, ValueError):
                    fallback = {}
                if str(fallback.get("username") or "").strip():
                    data, candidate = fallback, home

        cfg = cls(**{k: v for k, v in _filter(data, cls).items() if k != "source"})
        cfg.coverage = _build(Coverage, data.get("coverage"))
        cfg.run = _build(RunPolicy, data.get("run"))
        cfg.experiments = [int(x) for x in (data.get("experiments") or [])]
        cfg.lab_id = int(data.get("lab_id") or 0)
        cfg.charset = str(data.get("charset") or cfg.charset)
        cfg.policy_urls = [str(u) for u in (data.get("policy_urls") or [])]
        cfg.source = str(candidate or "")

        # environment wins over the file, so a password never has to be stored
        cfg.username = os.environ.get("YCSY_USERNAME") or cfg.username
        cfg.password = os.environ.get("YCSY_PASSWORD") or cfg.password
        cfg.api_base = os.environ.get("YCSY_API_BASE") or cfg.api_base

        cfg.run.keep = clamp_keep(cfg.run.keep)
        cfg.run.max_runtime_hours = min(
            float(cfg.run.max_runtime_hours or MAX_RUNTIME_HOURS), MAX_RUNTIME_HOURS
        )
        return cfg

    def require_credentials(self) -> None:
        if not self.username or not self.password:
            raise SystemExit(
                "缺少账号：运行 `ycsy account` 或 `ycsy tui` 在界面里填，"
                "也可以写进 %s，或设置 YCSY_USERNAME / YCSY_PASSWORD"
                % self.home_config_path()
            )

    def save(self, path: Optional[str] = None) -> Path:
        """Persist the whole config to the user config dir (or an explicit path)."""
        target = Path(path).expanduser() if path else self.home_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "api_base": self.api_base,
            "web_base": self.web_base,
            "username": self.username,
            "password": self.password,
            "experiments": list(self.experiments),
            "coverage": asdict(self.coverage),
            "run": asdict(self.run),
            "policy_urls": list(self.policy_urls),
            "notify": self.notify,
        }
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        self.source = str(target)
        return target

    @staticmethod
    def home_config_path() -> Path:
        return Path(user_config_dir(APP_NAME, appauthor=False)) / "config.json"


def clamp_keep(value: Any) -> int:
    """A released build keeps between 1 and MAX_KEEP bookings; no fill mode."""
    try:
        keep = int(value)
    except (TypeError, ValueError):
        keep = 2
    return max(1, min(keep, MAX_KEEP))


def _pick_config_path(explicit: Optional[str]) -> Optional[Path]:
    explicit = explicit or os.environ.get("YCSY_CONFIG")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise SystemExit("配置文件不存在: %s" % path)
        return path
    local = Path("config.json")
    if local.exists():
        return local
    home = Config.home_config_path()
    if home.exists():
        return home
    return None


def _filter(data: dict, target: type) -> dict:
    names = {f.name for f in fields(target)}
    return {k: v for k, v in data.items() if k in names}


def _build(target: type, data: Any) -> Any:
    """Turn a plain dict into a dataclass, ignoring unknown keys."""
    instance = target()
    if not isinstance(data, dict):
        return instance
    for item in fields(target):
        if item.name in data and data[item.name] is not None:
            value = data[item.name]
            if is_dataclass(value):
                continue
            if item.name == "interval_sec":
                value = tuple(value)
            setattr(instance, item.name, value)
    return instance


def state_dir() -> Path:
    return _ensure_dir([
        Path(user_cache_dir(APP_NAME, appauthor=False)),
        Path.home() / ".ycsy-book",
        Path(tempfile.gettempdir()) / "ycsy-book",
    ])


def log_path() -> Path:
    directory = _ensure_dir([
        Path(user_log_dir(APP_NAME, appauthor=False)),
        Path(tempfile.gettempdir()) / "ycsy-book",
    ])
    return directory / "ycsy-book.log"


def _ensure_dir(candidates: List[Path]) -> Path:
    """First writable directory wins; a locked-down profile must not crash us."""
    last = None
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return path
        except OSError as exc:
            last = exc
    raise SystemExit("找不到可写的状态目录：%s" % last)
