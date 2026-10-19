"""Command line entry point: ``ycsy <command>``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import sys
from typing import List, Optional

from . import __version__, planner
from .client import open_remote
from .config import MAX_KEEP, Config, clamp_keep, state_dir
from .providers import ApiError
from .service import Service
from .state import read_pid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ycsy",
        description="远程实验台预约工具",
    )
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--version", action="version", version=__version__)
    # no subcommand -> the TUI, so a double-clicked executable just works
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("login", help="校验账号并缓存 token")
    sub.add_parser("account", help="交互式设置账号密码（保存到用户配置目录）")
    sub.add_parser("survey", help="只读：实验 / 开放时段 / 配额")
    sub.add_parser("status", help="列出我的预约")
    sub.add_parser("tui", help="终端界面")

    plan = sub.add_parser("plan", help="只读：算出还要补哪些时段")
    plan.add_argument("--keep", type=int, help="同时保持几条（1-%d）" % MAX_KEEP)

    run = sub.add_parser("run", help="值守：补齐并持续巡检")
    run.add_argument("--keep", type=int, help="同时保持几条（1-%d）" % MAX_KEEP)
    run.add_argument("--once", action="store_true", help="只跑一轮")
    run.add_argument("--dry-run", action="store_true", help="只报告，不下单")
    run.add_argument("--no-open", action="store_true", help="自动开始时不拉起客户端")
    run.add_argument("--go", action="store_true", help="首次运行也必须加这个参数才会真正下单")
    run.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)

    sub.add_parser("start", help="开始当前这一格实验")

    connect = sub.add_parser("connect", help="重新拉起上次实验的客户端")
    connect.add_argument("--desk", type=int, help="台位 id（默认用上次记录）")

    sub.add_parser("close", help="关闭远程客户端进程")

    cancel = sub.add_parser("cancel", help="取消预约")
    cancel.add_argument("--ids", help="逗号分隔的预约 id")
    cancel.add_argument("--all", action="store_true", help="取消所有还没开始的预约")
    cancel.add_argument("--dry-run", action="store_true", help="只列出会取消哪些")

    sub.add_parser("state", help="输出 JSON 快照（给界面用）")
    sub.add_parser("stop", help="停掉后台值守进程")

    font = sub.add_parser("font", help="查看或设置控制台字体（仅 Windows）")
    font.add_argument("name", nargs="?", help="要使用的字体名，例如 \"Cascadia Mono\"")
    font.add_argument("--show", action="store_true", help="只显示当前字体")
    return parser


def _force_utf8() -> None:
    """Make Chinese output survive a non-UTF-8 console.

    A Windows install whose code page is 1252 raises UnicodeEncodeError as soon
    as help text or a status line is printed, which kills the frozen exe on
    anybody else's machine.
    """
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8()
    args = build_parser().parse_args(argv)
    if not args.command:
        args.command = "tui"
    cfg = Config.load(args.config)

    if args.command == "tui":
        from .tui import run_tui

        return run_tui(cfg)

    if args.command == "account":
        return _account(cfg)

    if args.command == "font":
        return _font(args, cfg)

    service = Service(cfg, dry_run=getattr(args, "dry_run", False))
    try:
        if args.command == "login":
            cfg.require_credentials()
            service.login()
            me = service.provider.me()
            print("用户：%s  角色：%s" % (me.get("userName"), me.get("jobType")))
            return 0

        if args.command == "stop":
            return _stop()

        service.login()

        if args.command == "state":
            print(json.dumps(service.snapshot(), ensure_ascii=False))
            return 0
        if args.command == "survey":
            return _survey(service)
        if args.command == "status":
            return _status(service)
        if args.command == "plan":
            _apply_keep(cfg, args)
            return _plan(service)
        if args.command == "run":
            _apply_keep(cfg, args)
            if args.no_open:
                cfg.run.auto_open = False
            return _run(service, args)
        if args.command == "start":
            return _start(service)
        if args.command == "connect":
            return _connect(service, args.desk)
        if args.command == "close":
            return 0 if service.close_client() else 1
        if args.command == "cancel":
            return _cancel(service, args)

    except ApiError as exc:
        print("失败：%s" % exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


# --------------------------------------------------------------------- commands

def _apply_keep(cfg: Config, args) -> None:
    if getattr(args, "keep", None) is None:
        return
    want = int(args.keep)
    if want <= 0:
        raise SystemExit("发布版不支持一次性铺满：--keep 最小为 1（最多 %d）" % MAX_KEEP)
    clamped = clamp_keep(want)
    if clamped != want:
        print("提示：单次最多保持 %d 条，已按 %d 处理" % (MAX_KEEP, clamped))
    cfg.run.keep = clamped


def _font(args, cfg: Config) -> int:
    from . import console

    current = console.font_name()
    print("当前字体：%s" % (current or "（读不到；Windows Terminal 等伪终端没有字体概念）"))
    print("")
    print("推荐字体（支持中英混排，方块字符不会错位）：")
    for name in console.FONT_CANDIDATES:
        print("  %s%s" % (name, "   ← 当前" if name == current else ""))
    print("")
    print("怎么换：")
    print("  Windows Terminal：设置 → 配置文件 → 外观 → 字体 → 选上面任意一个")
    print("                   （没有的话先装：winget install Sarasa.Gothic.Mono 或手动装字体）")
    print("  旧版控制台：右键标题栏 → 属性 → 字体 → 选 Consolas / Lucida Console")
    print("")
    print("程序不会替你改字体：伪终端下系统接口是空操作，改了也只影响当前窗口一次。")
    return 0


def _account(cfg: Config) -> int:
    """Interactive credential setup; verifies before saving."""
    import getpass

    target = Config.home_config_path()
    print("账号设置（保存到 %s）" % target)
    default = cfg.username
    prompt = "  学号/用户名%s: " % (" [%s]" % default if default else "")
    user = (input(prompt).strip() or default)
    if not user:
        print("已取消")
        return 1
    password = getpass.getpass("  密码: ").strip()
    if not password:
        print("已取消")
        return 1

    cfg.username, cfg.password = user, password
    probe = Service(cfg, quiet=True)
    probe.store.token = ""
    try:
        probe.provider.login(user, password)
    except ApiError as exc:
        print("登录失败：%s" % exc)
        return 1
    print("已保存：%s" % cfg.save())
    return 0


def _survey(service: Service) -> int:
    experiments = service.experiments()
    print("== 实验（%d 个）==" % len(experiments))
    for exp in experiments:
        print("  #%-5s %-26s 实验室 %s(%s) %s-%s  每次 %s 分钟  可预约 %s 次"
              % (exp.id, exp.name[:26], exp.lab_name, exp.lab_id,
                 exp.lab_start, exp.lab_end, exp.limit_time, exp.limit_count))
    return 0


def _status(service: Service) -> int:
    rows = []
    for exp in service.experiments():
        for item in service.provider.appointments(exp):
            rows.append((item.when, exp.id, item.desk_id))
    for when, exp_id, desk in sorted(rows):
        print("%-16s 实验#%-5s 台位 %s" % (when, exp_id, desk))
    print("共 %d 条有效预约（上限 %d 条）" % (len(rows), MAX_KEEP))
    return 0


def _plan(service: Service) -> int:
    plan = service.plan()
    pending = plan.pending
    for item in planner.remaining_quota(plan.experiments, plan.used):
        print("  #%-5s %-24s 剩余 %s/%s" % (item["experiment"], item["name"][:24],
                                            item["left"], item["limit"]))
    print("== 已覆盖 %d 格 ==" % len(plan.covered))
    print("== 待补 %d 格（同时最多 %d 条，当前 %d 条）=="
          % (len(pending), MAX_KEEP, plan.active))
    for slot in pending[:40]:
        exp = planner.pick_experiment(slot, plan.experiments, plan.used, plan.occupied)
        print("  %s  ->  %s" % (slot, ("实验#%s" % exp.id) if exp else "（没有可用实验）"))
    if len(pending) > 40:
        print("  …还有 %d 格" % (len(pending) - 40))
    return 0


def _run(service: Service, args) -> int:
    """Run the watcher, with a mandatory rehearsal on the very first start."""
    cfg = service.cfg
    marker = state_dir() / "first-run.done"
    first = not marker.exists()

    if first and not args.go and not args.dry_run:
        print("== 首次运行：先预演一轮，不会下单 ==")
        print("确认没问题后，再加 --go 正式值守：ycsy run --go")
        service.dry_run = True
        service.watch(once=True, parent_pid=getattr(args, "parent_pid", None))
        return 0

    if args.go and first:
        try:
            marker.write_text("started %s" % dt.datetime.now().isoformat(timespec="seconds"),
                              encoding="utf-8")
        except OSError:
            pass

    print("值守开始：保持 %d 条 / 最多 %d 条 / 单次最长 %.1f 小时"
          % (cfg.run.keep, MAX_KEEP, cfg.run.max_runtime_hours))
    service.watch(once=args.once, parent_pid=getattr(args, "parent_pid", None))
    return 0


def _start(service: Service) -> int:
    now = dt.datetime.now()
    for exp in service.experiments():
        for item in service.provider.appointments(exp):
            begin = planner.parse_dt(item.when)
            if begin <= now < begin + dt.timedelta(minutes=exp.slot_minutes):
                return 0 if service.start(item, exp) else 1
    print("现在没有处于预约时段内的记录")
    return 1


def _connect(service: Service, desk_id: Optional[int]) -> int:
    session = service.store.session()
    desk_id = desk_id or session.get("desk_id")
    if not desk_id:
        print("不知道该连哪个台位：用 --desk 指定，或先跑一次 start")
        return 1
    url, _ = service.provider.remote_link(int(desk_id))
    if not url:
        print("没拿到远程地址")
        return 1
    open_remote(url, service.log)
    return 0


def _cancel(service: Service, args) -> int:
    wanted = None
    if args.ids:
        wanted = {int(x) for x in str(args.ids).split(",") if x.strip().isdigit()}
    now = dt.datetime.now()
    targets = []
    for exp in service.experiments():
        for item in service.provider.appointments(exp):
            if wanted is not None:
                if item.id in wanted:
                    targets.append(item)
            elif args.all and planner.parse_dt(item.when) >= now:
                targets.append(item)
    if not targets:
        print("没有匹配的预约")
        return 0
    for item in sorted(targets, key=lambda x: x.when):
        if getattr(args, "dry_run", False):
            print("[dry-run] 会取消 %s id=%s" % (item.when, item.id))
            continue
        service.provider.cancel(item.id)
        print("已取消 %s id=%s" % (item.when, item.id))
    return 0


def _stop() -> int:
    pid = read_pid(state_dir() / "daemon.pid")
    if not pid:
        print("没有记录到值守进程")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print("停止失败：%s" % exc)
        return 1
    for name in ("daemon.pid", "daemon.lock"):
        try:
            (state_dir() / name).unlink(missing_ok=True)
        except OSError:
            pass
    print("已停止值守进程 pid=%s" % pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
