"""Released-build limits: caps, runtime window, maintenance channel."""

from __future__ import annotations

from datetime import datetime

from ycsy.config import MAX_KEEP, MAX_RUNTIME_HOURS, Config, clamp_keep
from ycsy.model import Experiment
from ycsy.planner import rolling_targets
from ycsy.policy import Policy


def make_experiment(**kwargs) -> Experiment:
    base = dict(id=1, name="exp", limit_count=100, limit_time=20,
                lab_start="09:00", lab_end="22:00")
    base.update(kwargs)
    return Experiment(**base)


# ------------------------------------------------------------------ keep cap

def test_keep_cannot_be_zero():
    assert clamp_keep(0) == 1
    assert clamp_keep(-4) == 1


def test_keep_is_capped_at_max():
    assert clamp_keep(99) == MAX_KEEP
    assert MAX_KEEP == 3


def test_rolling_targets_never_exceed_cap():
    exp = make_experiment()
    now = datetime(2026, 10, 18, 8, 0)
    targets = rolling_targets(cfg_coverage(), [exp], 50, now)
    assert len(targets) == MAX_KEEP


def cfg_coverage():
    from ycsy.config import Coverage

    return Coverage(days_ahead=1)


def test_runtime_cap_cannot_be_raised_from_config():
    path = _config_with('{"run": {"max_runtime_hours": 48}}')
    cfg = Config.load(path)
    assert cfg.run.max_runtime_hours == MAX_RUNTIME_HOURS


def test_keep_from_config_is_clamped():
    path = _config_with('{"run": {"keep": 0}}')
    assert Config.load(path).run.keep == 1


def _config_with(text: str) -> str:
    import os
    import tempfile

    handle, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


# -------------------------------------------------------------- policy gate

def test_policy_off_by_default_makes_no_request():
    calls = []
    policy = Policy([], fetcher=lambda url: calls.append(url) or {})
    assert policy.check() == (True, "")
    assert calls == []


def test_policy_stops_when_disabled():
    policy = Policy(["https://example.invalid/p.json"],
                    fetcher=lambda url: {"status": "disabled", "msg": "已停止维护"})
    allowed, message = policy.check()
    assert allowed is False
    assert message == "已停止维护"


def test_policy_fails_open_on_error():
    def boom(url):
        raise RuntimeError("network down")

    policy = Policy(["https://example.invalid/p.json"], fetcher=boom)
    assert policy.check() == (True, "")


def test_policy_fails_open_on_unknown_payload():
    policy = Policy(["https://example.invalid/p.json"], fetcher=lambda url: {"foo": 1})
    assert policy.check() == (True, "")


def test_policy_tries_next_source():
    def fetcher(url):
        if "second" in url:
            return {"status": "disabled", "msg": "stop"}
        raise RuntimeError("down")

    policy = Policy(["https://a.invalid/second", "https://b.invalid/x"], fetcher=fetcher)
    allowed, _ = policy.check()
    assert allowed is False


# ------------------------------------------------------------ first-run gate

class _StubService:
    def __init__(self):
        self.cfg = Config()
        self.dry_run = False
        self.calls = []

    def watch(self, once=False, parent_pid=None):
        self.calls.append((self.dry_run, once))


def _args(go=False, dry_run=False, once=False):
    return type("Args", (), {"go": go, "dry_run": dry_run, "once": once})()


def test_first_run_only_rehearses(monkeypatch):
    import pathlib
    import tempfile

    from ycsy import cli

    home = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cli, "state_dir", lambda: home)
    service = _StubService()

    assert cli._run(service, _args()) == 0
    assert service.calls == [(True, True)]          # dry-run, one pass
    assert not (home / "first-run.done").exists()   # nothing recorded yet


def test_go_flag_records_first_run(monkeypatch):
    import pathlib
    import tempfile

    from ycsy import cli

    home = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(cli, "state_dir", lambda: home)
    service = _StubService()

    assert cli._run(service, _args(go=True)) == 0
    assert service.calls == [(False, False)]
    assert (home / "first-run.done").exists()


# ------------------------------------------------------------ account saving

def test_config_save_roundtrip():
    import tempfile

    cfg = Config(username="u123", password="p456")
    cfg.run.keep = 3
    path = cfg.save(tempfile.mktemp(suffix=".json"))

    again = Config.load(path)
    assert again.username == "u123"
    assert again.password == "p456"
    assert again.run.keep == 3
    assert again.coverage.days_ahead == cfg.coverage.days_ahead

# ------------------------------------------------------------- account prompt

def test_account_prompt_cancels_on_esc(monkeypatch):
    """Esc (or Ctrl+C) in the account screen must leave the config untouched."""
    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    tui = Tui(Cfg(username="u", password="p"))
    monkeypatch.setattr(tui, "_read_line", lambda *a, **k: None)
    saved = []
    monkeypatch.setattr(tui.cfg, "save", lambda *a, **k: saved.append(1))

    assert tui._prompt_account(force=True) is False
    assert saved == []

# ------------------------------------------------------------ account hotkey

def test_u_key_requests_account_screen():
    from ycsy.config import Config as Cfg
    from ycsy.keys import Key
    from ycsy.tui import Tui

    tui = Tui(Cfg(username="u", password="p"))
    assert tui._handle_main(Key("u")) == "relogin"


def test_relogin_actually_prompts(monkeypatch):
    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    tui = Tui(Cfg(username="u", password="p"))
    tui.service.login = lambda: None

    sequence = ["relogin", "quit"]
    monkeypatch.setattr(tui, "_live_once", lambda: sequence.pop(0) if sequence else None)
    prompted = []
    monkeypatch.setattr(tui, "_prompt_account",
                        lambda force=False: prompted.append(force) or True)

    assert tui.run() == 0
    assert prompted == [True]

# ------------------------------------------------------- experiment switching

def test_candidate_experiments_skips_quota_and_steps_forward():
    from ycsy import planner

    first = make_experiment(id=10)
    second = make_experiment(id=20)
    slot = "2026-10-18 10:00"
    candidates = planner.candidate_experiments(slot, [first, second], {10: 100, 20: 5}, {})
    assert [c.id for c in candidates] == [20]


def test_candidate_experiments_keeps_order():
    from ycsy import planner

    a = make_experiment(id=10)
    b = make_experiment(id=20)
    slot = "2026-10-18 10:00"
    candidates = planner.candidate_experiments(slot, [b, a], {}, {})
    assert [c.id for c in candidates] == [20, 10]


# ------------------------------------------------------------- override file

class _StubStore:
    def cache(self):
        return {}

    def save_cache(self, data):
        pass


def _service(monkeypatch):
    from ycsy.config import Config as Cfg
    from ycsy.service import Service

    svc = Service(Cfg(), quiet=True)
    monkeypatch.setattr(svc, "store", _StubStore())
    return svc


def test_override_pins_experiments(monkeypatch):
    svc = _service(monkeypatch)
    monkeypatch.setattr(svc, "override", lambda: {"experiments": [114]})
    asked = []
    monkeypatch.setattr(svc.provider, "experiment",
                        lambda exp_id: asked.append(exp_id) or make_experiment(id=exp_id))
    out = svc.experiments()
    assert asked == [114]
    assert [e.id for e in out] == [114]


def test_override_filters_lab(monkeypatch):
    svc = _service(monkeypatch)
    monkeypatch.setattr(svc, "override", lambda: {"lab_id": 12})
    monkeypatch.setattr(svc.provider, "experiments",
                        lambda: [make_experiment(id=1, lab_id=12),
                                 make_experiment(id=2, lab_id=9)])
    out = svc.experiments()
    assert [e.id for e in out] == [1]

# --------------------------------------------------------------- daemon start

def test_stale_lock_no_longer_blocks_start(monkeypatch):
    import pathlib
    import tempfile

    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    home = pathlib.Path(tempfile.mkdtemp())
    lock = home / "daemon.pid"
    lock.write_text("999999", encoding="utf-8")     # pid that cannot exist

    tui = Tui(Cfg())
    monkeypatch.setattr("ycsy.tui.state_dir", lambda: home)
    assert tui._daemon_pid() is None                # stale lock is dropped
    assert not lock.exists()


def test_daemon_start_reports_when_child_dies(monkeypatch):
    import pathlib
    import subprocess as sp
    import tempfile

    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    home = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr("ycsy.tui.state_dir", lambda: home)
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: None)
    monkeypatch.setattr("ycsy.tui.time.sleep", lambda s: None)

    tui = Tui(Cfg())
    tui._daemon(True)
    assert "没起来" in tui.message


def test_daemon_start_reports_pid(monkeypatch):
    import os
    import pathlib
    import subprocess as sp
    import tempfile

    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    home = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr("ycsy.tui.state_dir", lambda: home)

    def fake_popen(*args, **kwargs):
        (home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
        return None

    monkeypatch.setattr(sp, "Popen", fake_popen)
    tui = Tui(Cfg())
    tui._daemon(True)
    assert "值守已启动" in tui.message

def test_daemon_start_handles_config_path(monkeypatch):
    """Regression: Path() was used in _daemon without importing pathlib."""
    import os
    import pathlib
    import subprocess as sp
    import tempfile

    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    home = pathlib.Path(tempfile.mkdtemp())
    config = home / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("ycsy.tui.state_dir", lambda: home)

    seen = {}

    def fake_popen(args, **kwargs):
        seen["args"] = args
        seen["cwd"] = kwargs.get("cwd")
        (home / "daemon.pid").write_text(str(os.getpid()), encoding="utf-8")
        return None

    monkeypatch.setattr(sp, "Popen", fake_popen)
    cfg = Cfg()
    cfg.source = str(config)
    tui = Tui(cfg)
    tui._daemon(True)

    assert "值守已启动" in tui.message
    args = seen["args"]
    # --config is a global option: it must appear before the subcommand
    assert "--config" in args
    assert args.index("--config") < args.index("run")
    assert args[args.index("--config") + 1].endswith("config.json")
    assert "--parent-pid" in args
    assert args[args.index("--parent-pid") + 1] == str(os.getpid())
    import os as _os

    assert _os.path.normcase(_os.path.realpath(seen["cwd"])) == \
        _os.path.normcase(_os.path.realpath(str(home)))

# ------------------------------------------------------- orphan watchdog

def test_watch_stops_when_parent_is_gone(monkeypatch):
    import pathlib
    """Closing the TUI window must not leave a booking daemon running."""
    from ycsy.config import Config as Cfg
    from ycsy.service import Service

    svc = Service(Cfg(), quiet=True)
    import tempfile

    svc.store.dir = pathlib.Path(tempfile.mkdtemp())   # keep the real lock untouched
    calls = []
    monkeypatch.setattr(svc, "run_pass", lambda: calls.append(1) or 0)

    svc.watch(once=True, parent_pid=999999)   # parent that cannot exist
    assert calls == []

# ------------------------------------------------------------- single day only

def test_rolling_targets_never_cross_midnight():
    from datetime import datetime

    from ycsy import planner
    from ycsy.config import Coverage

    exp = make_experiment()
    cov = Coverage(days_ahead=3)
    late = datetime(2026, 10, 18, 21, 45)      # nothing left today
    assert planner.rolling_targets(cov, [exp], 3, late) == []

    dusk = datetime(2026, 10, 18, 20, 30)      # room for three more today
    targets = planner.rolling_targets(cov, [exp], 3, dusk)
    assert targets == ["2026-10-18 20:40", "2026-10-18 21:00", "2026-10-18 21:20"]
    assert all(t.startswith("2026-10-18") for t in targets)

# ------------------------------------------------- config-level pinning

def test_config_experiments_pin_the_list(monkeypatch):
    from ycsy.config import Config as Cfg
    from ycsy.service import Service

    cfg = Cfg()
    cfg.experiments = [114]
    svc = Service(cfg, quiet=True)
    import tempfile
    import pathlib

    svc.store.dir = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(svc, "override", lambda: {})
    asked = []
    monkeypatch.setattr(svc.provider, "experiment",
                        lambda exp_id: asked.append(exp_id) or make_experiment(id=exp_id))
    assert [e.id for e in svc.experiments()] == [114]
    assert asked == [114]


def test_config_lab_id_filters_discovery(monkeypatch):
    from ycsy.config import Config as Cfg
    from ycsy.service import Service

    cfg = Cfg()
    cfg.lab_id = 12
    svc = Service(cfg, quiet=True)
    import tempfile
    import pathlib

    svc.store.dir = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(svc, "override", lambda: {})
    monkeypatch.setattr(svc.provider, "experiments",
                        lambda: [make_experiment(id=1, lab_id=12),
                                 make_experiment(id=2, lab_id=9)])
    assert [e.id for e in svc.experiments()] == [1]

# ------------------------------------------------------------- console encoding

def test_force_utf8_survives_streams_without_reconfigure(monkeypatch):
    """A cp1252 console used to crash --help with UnicodeEncodeError."""
    import io
    import sys

    from ycsy import cli

    fake = io.StringIO()
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setattr(sys, "stderr", fake)
    cli._force_utf8()          # must not raise


def test_main_reports_version():
    import pytest

    from ycsy import cli

    with pytest.raises(SystemExit) as exc:      # argparse exits for --version
        cli.main(["--version"])
    assert exc.value.code == 0

# --------------------------------------------------- policy network guards

def _fake_headers(**kw):
    return kw


def test_policy_rejects_http_and_unknown_hosts():
    from ycsy.policy import PolicyUrlError, check_url

    bad = [
        "http://raw.githubusercontent.com/x/y/main/.policy.json",
        "https://evil.example/.policy.json",
        "https://raw.githubusercontent.com.evil.example/p.json",
        "https://raw.githubusercontent.com:8443/p.json",
        "https://u:p@raw.githubusercontent.com/p.json",
    ]
    for url in bad:
        try:
            check_url(url)
        except PolicyUrlError:
            continue
        raise AssertionError("should have been refused: %s" % url)


def test_policy_rejects_private_addresses(monkeypatch):
    import socket

    from ycsy.policy import PolicyUrlError, check_url

    for address in ("127.0.0.1", "10.0.0.7", "169.254.169.254", "::1", "fd00::1"):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))],
        )
        try:
            check_url("https://raw.githubusercontent.com/x/y/main/.policy.json")
        except PolicyUrlError as exc:
            assert "non-public" in str(exc), exc      # the address check, not DNS luck
            continue
        raise AssertionError("private address accepted: %s" % address)


def test_policy_blocks_redirect_to_other_host(monkeypatch):
    import requests

    from ycsy import policy

    monkeypatch.setattr(policy, "_resolve_public", lambda host: None)
    monkeypatch.setattr(policy, "_read_capped", lambda response: b'{"status": "ok"}')

    class Redirect:
        status_code = 302
        headers = {"Location": "https://evil.example/.policy.json"}

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: Redirect())
    try:
        policy.fetch("https://raw.githubusercontent.com/x/y/main/.policy.json")
    except policy.PolicyUrlError as exc:
        assert "allowlist" in str(exc)
        return
    raise AssertionError("redirect to another host was followed")


def test_policy_caps_response_size(monkeypatch):
    from ycsy import policy

    class Huge:
        status_code = 200
        headers: dict = {}

        def iter_content(self, size):
            for _ in range(64):
                yield b"x" * 4096

        def close(self):
            pass

    monkeypatch.setattr(policy.requests, "get", lambda *a, **k: Huge())
    monkeypatch.setattr(policy, "_resolve_public", lambda host: None)
    try:
        policy.fetch("https://raw.githubusercontent.com/x/y/main/.policy.json")
    except policy.PolicyUrlError as exc:
        assert "larger than" in str(exc)
        return
    raise AssertionError("oversized body was accepted")

# --------------------------------------------------------- frozen launch args

def test_daemon_args_drop_module_flag_when_frozen(monkeypatch):
    """In a frozen build sys.executable is ycsy.exe, so "-m ycsy" must go."""
    import sys as _sys

    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    cfg = Cfg()
    cfg.source = "C:/tmp/config.json"
    tui = Tui(cfg)

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    frozen = tui._daemon_args()
    assert frozen[0] == _sys.executable
    assert "-m" not in frozen
    assert frozen[frozen.index("--config") + 1].endswith("config.json")
    assert "run" in frozen and "--go" in frozen and "--parent-pid" in frozen

    monkeypatch.delattr(_sys, "frozen", raising=False)
    source = tui._daemon_args()
    assert source[:3] == [_sys.executable, "-m", "ycsy"]


def test_grid_columns_adapt_to_width():
    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    tui = Tui(Cfg())
    narrow = tui.__class__._per_row
    widths = {}
    for width in (60, 80, 100, 160):
        tui._width = lambda w=width: w - 1
        widths[width] = narrow(tui)
    assert widths[60] < widths[80] <= widths[100] <= widths[160]
    assert widths[160] == 12                 # capped, never absurdly wide
    assert min(widths.values()) >= 3

# ------------------------------------------------------------ corner mascot

def test_whale_art_is_colourful_and_compact():
    from ycsy.config import Config as Cfg
    from ycsy.tui import Tui

    picture = Tui(Cfg())._whale()
    lines = picture.plain.split("\n")
    assert len(lines) == 8
    assert len(lines[0]) == 28
    colours = {str(span.style) for span in picture.spans}
    assert any("#556ef6" in style for style in colours)      # body blue
    assert any("#18255d" in style for style in colours)      # outline
