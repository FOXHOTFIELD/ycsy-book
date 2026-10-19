"""Terminal UI built on rich.

Layout is a plain stacked renderable, redrawn on every key press or timer tick;
keys come from :mod:`ycsy.keys`, so it behaves the same in Windows Terminal,
conhost, and any POSIX terminal.

The UI never books anything itself: it calls :class:`ycsy.service.Service`,
exactly like the CLI does.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .config import MAX_KEEP, Config, log_path, state_dir
from .keys import Key, KeyReader
from .providers import ApiError
from .service import Service

MENU = [
    ("start_daemon", "启动值守"),
    ("stop_daemon", "停止值守"),
    ("start_now", "打开客户端"),
    ("reconnect", "重连客户端"),
    ("cancel", "取消预约"),
    ("refresh", "刷新"),
    ("log", "打开日志"),
    ("close_client", "关闭客户端"),
    ("quit", "退出"),
]

EXIT_OPTIONS = [
    ("clean", "退出并清理", "停值守 · 关客户端 · 取消剩余预约"),
    ("plain", "直接退出", "保留预约和客户端"),
    ("cancel", "取消", "留在界面"),
]

COLORS = {"booked": "green", "started": "cyan", "free": "yellow", "past": "grey50"}


class Tui:
    def __init__(self, cfg: Config, refresh_sec: int = 20) -> None:
        self.cfg = cfg
        self.service = Service(cfg)
        self.refresh_sec = refresh_sec
        self.state: Dict = {}
        self.selected = 0
        self.mode = "main"
        self.message = ""
        self.checked: Set[int] = set()
        self.cancel_index = 0
        self.exit_index = 0
        self._next_refresh = 0.0
        self._child: Optional[subprocess.Popen] = None
        self.console = Console()

    # ---------------------------------------------------------------- loop
    def run(self) -> int:
        """Outer loop: the account screen needs the terminal back, so leaving
        the full-screen view and re-entering it is a normal transition."""
        while True:
            if not self._ensure_account():
                return 1
            try:
                self.service.login()
            except ApiError as exc:
                print("登录失败：%s" % exc)
                if not self._prompt_account(force=True):
                    return 1
                continue
            result = self._live_once()
            if result == "relogin":
                self._prompt_account(force=True)
                # the account screen prints above the live view; wipe it so the
                # main screen does not sit under a pile of leftover lines
                self.console.clear()
                continue
            return 0

    def _live_once(self) -> Optional[str]:
        # whatever printed before (account screen, login error) must not stay
        # above the full-screen view
        self.console.clear()
        self.state = self._safe_snapshot()
        expired = False
        with KeyReader() as reader, Live(self._view(), screen=True, auto_refresh=False) as live:
            live.refresh()
            while True:
                # same 4h cap as the watcher: the UI does not outlive it either
                if self.service.expired:
                    expired = True
                    break
                key = reader.read(0.15)
                if key is None:
                    if time.time() >= self._next_refresh:
                        self.state = self._safe_snapshot()
                        live.update(self._view())
                        live.refresh()
                        self._next_refresh = time.time() + self.refresh_sec
                    continue
                action = self._handle(key)
                if action == "quit":
                    break
                if action == "relogin":
                    return "relogin"
                # repaint at once: refreshing the snapshot talks to the API and
                # takes seconds, and the user should see the action land
                live.update(self._view())
                live.refresh()
                self.state = self._safe_snapshot()
                live.update(self._view())
                live.refresh()
                self._next_refresh = time.time() + self.refresh_sec
        if expired:
            print("已运行满 %.1f 小时，界面自动退出" % self.cfg.run.max_runtime_hours)
        return None

    # ------------------------------------------------------------- account
    def _ensure_account(self) -> bool:
        if self.cfg.username and self.cfg.password:
            return True
        return self._prompt_account(force=True)

    def _prompt_account(self, force: bool = False) -> bool:
        """Ask for the credentials, verify them, then persist to the user config."""
        console = self.console
        rule = self._rule_text(console)
        console.clear()
        console.print(self._heading("配置账号"))
        console.print(rule)
        me = self.service.me() or {}
        who = " ".join(x for x in (me.get("userName"), me.get("nickName")) if x) or "（未登录）"
        console.print(Text.assemble(("  配置文件  ", "grey50"),
                                    (str(Config.home_config_path()), "grey70")))
        console.print(Text.assemble(("  当前账号  ", "grey50"), (who, "grey70")))
        console.print(Text.assemble(("  服务器    ", "grey50"), (self.cfg.api_base, "grey70")))
        console.print(rule)
        console.print(Text.assemble(("  留空回车 = 保持原值", "grey37"),
                                    ("    Esc = 不做修改返回", "grey37")))
        console.print(Text.assemble(("  新账号初始密码 ", "grey37"), ("123456", "grey70"),
                                    ("（没改过的就是它）", "grey37")))
        console.print("")

        while True:
            default = self.cfg.username
            label = "  学号/用户名" + (" [%s]" % default if default else "") + ": "
            user = self._read_line(label)
            if user is None:
                return False
            user = user or default
            if not user:
                console.print(Text("  学号不能为空", style="red"))
                continue

            password = self._read_line("  密码: ", password=True)
            if password is None:
                return False
            if not password:
                console.print(Text("  密码不能为空", style="red"))
                continue

            self.cfg.username, self.cfg.password = user, password
            self.service.provider.token = ""
            self.service.store.token = ""
            console.print(Text("  正在验证…", style="grey50"))
            try:
                self.service.provider.login(user, password)
            except ApiError as exc:
                console.print(Text.assemble(("  ✗ 登录失败：", "red"), (str(exc), "grey70")))
                console.print("")
                continue

            self.service.forget_profile()
            path = self.cfg.save()
            console.print(Text.assemble(("  ✓ 已保存  ", "green"), (str(path), "grey70")))
            console.print(Text("  按任意键返回界面", style="grey50"))
            try:
                with KeyReader() as reader:
                    reader.read(30)
            except Exception:
                pass
            return True

    def _heading(self, title: str) -> Text:
        return Text.assemble(("▍ ", "cyan"), (title, "bold white"))

    def _read_line(self, label: str, password: bool = False) -> Optional[str]:
        """One line of input that understands Esc (cancel) and Backspace.

        ``console.input`` cannot see a lone Esc, and the account screen needs it
        to mean "leave everything as it is".
        """
        self.console.print(Text(label, style="grey50"), end="")
        buf = ""
        with KeyReader() as reader:
            while True:
                key = reader.read(0.5)
                if key is None:
                    continue
                if key.name in ("esc", "ctrl-c"):
                    self.console.print("")
                    return None
                if key.name == "enter":
                    self.console.print("")
                    return buf or ""
                if key.name == "backspace":
                    if buf:
                        buf = buf[:-1]
                        self.console.print("\b \b", end="")
                    continue
                char = key.char
                if char and char.isprintable():
                    buf += char
                    self.console.print("*" if password else char, end="")

    def _rule_text(self, console) -> Text:
        width = max(50, min(getattr(console, "width", 80) - 1, 78))
        return Text("─" * width, style="grey37")

    def _safe_snapshot(self) -> Dict:
        try:
            return self.service.snapshot()
        except ApiError as exc:
            self.message = "刷新失败：%s" % exc
            return self.state or {}

    # --------------------------------------------------------------- input
    def _handle(self, key: Key) -> Optional[str]:
        if self.mode == "cancel":
            return self._handle_cancel(key)
        if self.mode == "exit":
            return self._handle_exit(key)
        return self._handle_main(key)

    def _handle_main(self, key: Key) -> Optional[str]:
        name = key.name
        if name in ("down", "j", "right", "l"):
            step = 3 if name in ("down", "j") else 1
            self.selected = min(self.selected + step, len(MENU) - 1)
        elif name in ("up", "k", "left", "h"):
            step = 3 if name in ("up", "k") else 1
            self.selected = max(self.selected - step, 0)
        elif name in ("space", "enter"):
            return self._activate(MENU[self.selected][0])
        elif name == "m":
            self._cycle_keep()
        elif name == "u":
            return "relogin"
        elif name == "q":
            self.mode = "exit"
        elif name == "ctrl-c":
            return "quit"
        return None

    def _activate(self, action: str) -> Optional[str]:
        if action == "quit":
            self.mode = "exit"
        elif action == "refresh":
            self.message = "已刷新"
        elif action == "account":
            return "relogin"
        elif action == "start_daemon":
            self._daemon(True)
        elif action == "stop_daemon":
            self._daemon(False)
        elif action == "start_now":
            self._start_now()
        elif action == "reconnect":
            self._reconnect()
        elif action == "cancel":
            if not self.state.get("appointments"):
                self.message = "没有可取消的预约"
            else:
                self.mode = "cancel"
                self.checked = {a["id"] for a in self.state["appointments"]}
                self.cancel_index = 0
        elif action == "log":
            self._open_log()
        elif action == "close_client":
            ok = self.service.close_client()
            self.message = "客户端已关闭" if ok else "客户端没在运行"
        return None

    # -------------------------------------------------------------- actions
    def _cycle_keep(self) -> None:
        """Cycle 1 -> 2 -> 3 -> 1 and persist it to the user config."""
        order = [1, 2, 3]
        current = int(self.cfg.run.keep or 2)
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else 2
        self.cfg.run.keep = nxt
        try:
            self.cfg.save()
            self.message = "模式：保持 %d 格（已保存，重启值守生效）" % nxt
        except OSError as exc:
            self.message = "模式：保持 %d 格（保存失败：%s）" % (nxt, exc)

    def _daemon(self, start: bool) -> None:
        pid = self._daemon_pid()
        if start and pid:
            self.message = "值守已在运行"
            return
        if not start:
            if not pid:
                self.message = "值守没在运行"
                return
            try:
                # our own child first: terminate() also works when the pid file
                # points somewhere the lock cannot be opened
                if self._child is not None and self._child.poll() is None:
                    self._child.terminate()
                else:
                    os.kill(pid, 15)
                self._forget_daemon(pid)
                self.message = "已停止值守"
            except OSError as exc:
                self.message = "停止失败：%s" % exc
            return

        log_file = state_dir() / "daemon.log"
        args = self._daemon_args()
        cwd = str(Path(self.cfg.source).resolve().parent) if self.cfg.source else None
        # keep the child's stdout in UTF-8: a pipe defaults to GBK on Windows and
        # would mix encodings inside daemon.log
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            with open(log_file, "a", encoding="utf-8") as stream:
                self._child = subprocess.Popen(args, cwd=cwd, env=env, close_fds=True,
                                               stdout=stream, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.message = "启动失败：%s" % exc
            return

        # give it a moment: a child that dies immediately used to look exactly
        # like "nothing happened"
        for _ in range(15):
            time.sleep(0.2)
            if self._child is not None and self._child.poll() is not None:
                break
            pid = self._daemon_pid()
            if pid:
                self.message = "值守已启动"
                return
        self.message = "值守没起来，看日志 %s" % log_file

    def _daemon_args(self) -> List[str]:
        """Build the child command line.

        ``--config`` is a *global* option: it has to come before the
        subcommand, otherwise argparse rejects the whole invocation.
        """
        args = [sys.executable, "-m", "ycsy"]
        if self.cfg.source:
            args += ["--config", str(Path(self.cfg.source).resolve())]
        args += ["run", "--go", "--parent-pid", str(os.getpid())]
        return args

    def _start_now(self) -> None:
        """打开客户端：当前时段有预约就直接开始；否则退回上次的台位。"""
        current = (self.state.get("current") or {}).get("time")
        for appointment in self.state.get("appointments", []):
            if appointment["time"] != current:
                continue
            experiment = next((e for e in self.service.experiments()
                               if e.id == appointment["experiment"]), None)
            if experiment is None:
                continue
            from .model import Appointment

            row = Appointment(id=appointment["id"], experiment_id=experiment.id,
                              desk_id=appointment["desk"], when=appointment["time"])
            self.message = "已开始实验" if self.service.start(row, experiment) else "开始失败"
            return
        # no appointment running right now: fall back to the last desk
        if (self.state.get("session") or {}).get("desk_id"):
            self._reconnect()
            return
        self.message = "当前时段没有预约，也没有可重连的台位"

    def _reconnect(self) -> None:
        from .client import open_remote

        session = self.state.get("session") or {}
        desk_id = session.get("desk_id")
        if not desk_id:
            self.message = "没有可重连的台位（先开始一次实验）"
            return
        url, _ = self.service.provider.remote_link(int(desk_id))
        if not url:
            self.message = "没拿到远程地址"
            return
        open_remote(url, self.service.log)
        self.message = "已重新拉起客户端"

    def _open_log(self) -> None:
        path = str(log_path())
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.message = "已打开日志"
        except OSError as exc:
            self.message = "打开日志失败：%s" % exc

    def _daemon_pid(self) -> Optional[int]:
        # the child we spawned ourselves is the most reliable source
        if self._child is not None and self._child.poll() is None:
            return self._child.pid
        from .state import live_pid

        # fallback for a watcher started outside this UI (stale locks are cleaned)
        return live_pid(state_dir() / "daemon.pid")

    # -------------------------------------------------------------- cancel
    def _handle_cancel(self, key: Key) -> Optional[str]:
        rows = self.state.get("appointments", [])
        if not rows:
            self.mode = "main"
            return None
        if key.name in ("down", "j"):
            self.cancel_index = min(self.cancel_index + 1, len(rows) - 1)
        elif key.name in ("up", "k"):
            self.cancel_index = max(self.cancel_index - 1, 0)
        elif key.name == "x":
            appt = rows[self.cancel_index]["id"]
            self.checked.symmetric_difference_update({appt})
        elif key.name == "a":
            if len(self.checked) == len(rows):
                self.checked.clear()
            else:
                self.checked = {r["id"] for r in rows}
        elif key.name == "esc":
            self.mode = "main"
        elif key.name == "enter":
            self._do_cancel()
        return None

    def _do_cancel(self) -> None:
        if not self.checked:
            self.message = "没有勾选任何预约"
            self.mode = "main"
            return
        done = 0
        for appointment in self.state.get("appointments", []):
            if appointment["id"] not in self.checked:
                continue
            try:
                self.service.provider.cancel(appointment["id"])
                done += 1
            except ApiError as exc:
                self.message = "取消失败：%s" % exc
                break
        self.message = "已取消 %d 条" % done
        self.checked.clear()
        self.mode = "main"

    # ---------------------------------------------------------------- exit
    def _handle_exit(self, key: Key) -> Optional[str]:
        if key.name in ("esc", "q"):
            self.mode = "main"
            return None
        if key.name in ("up", "k"):
            self.exit_index = max(self.exit_index - 1, 0)
        elif key.name in ("down", "j"):
            self.exit_index = min(self.exit_index + 1, len(EXIT_OPTIONS) - 1)
        elif key.name in ("enter", "space"):
            choice = EXIT_OPTIONS[self.exit_index][0]
            if choice == "cancel":
                self.mode = "main"
                return None
            if choice == "clean":
                self._cleanup()
            return "quit"
        return None

    def _cleanup(self) -> None:
        pid = self._daemon_pid()
        if pid:
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        self.service.close_client()
        rows = [a for a in self.state.get("appointments", [])
                if a["time"] >= dt.datetime.now().strftime("%Y-%m-%d %H:%M")]
        for appointment in rows:
            try:
                self.service.provider.cancel(appointment["id"])
            except ApiError:
                pass

    # ---------------------------------------------------------------- view
    def _view(self) -> RenderableType:
        if self.mode == "cancel":
            return self._view_cancel()
        if self.mode == "exit":
            return self._view_exit()
        return self._view_main()

    def _width(self) -> int:
        try:
            return max(60, shutil.get_terminal_size((100, 30)).columns - 1)
        except OSError:
            return 99

    def _rule(self) -> Text:
        return Text("─" * self._width(), style="grey37")

    def _view_main(self) -> RenderableType:
        state = self.state or {}
        runtime = state.get("runtime") or {}
        left = int(runtime.get("left_seconds") or 0)
        user = state.get("user") or {}

        # ▍ 远程实验预约值守   学号 姓名   时间
        head = Text()
        head.append("▍ ", style="cyan")
        head.append("远程实验预约值守", style="bold white")
        who = " ".join(x for x in (user.get("login"), user.get("name")) if x)
        if who:
            head.append("   ")
            head.append(who, style="grey70")
        head.append("   ")
        head.append(state.get("now", ""), style="grey50")

        pid = self._daemon_pid()
        status = Text()
        status.append("○ " if not pid else "● ", style="grey50" if not pid else "green")
        status.append("未运行" if not pid else "运行中",
                      style="grey50" if not pid else "green")
        status.append("    模式 保持 %d 格" % state.get("keep", 0), style="grey50")
        status.append("    占用 %d/%d 条" % (state.get("active", 0), MAX_KEEP), style="white")

        override = state.get("override") or {}
        pinned = override.get("experiments") or []
        if pinned or override.get("lab_id") is not None:
            if pinned:
                status.append("    手动: 实验 %s" % ",".join(str(x) for x in pinned),
                              style="yellow")
            else:
                status.append("    手动: 实验室 %s" % override.get("lab_id"), style="yellow")

        lab = state.get("lab") or {}
        pinned_lab = override.get("lab_id") is not None
        labline = Text()
        labline.append("实验室 ", style="grey50")
        labline.append("%s  %s–%s  (%s min)" % (lab.get("name", ""), lab.get("start", ""),
                                                lab.get("end", ""), state.get("step", "")),
                       style="grey70")
        labline.append("    ")
        labline.append("手动选择" if pinned_lab else "自动选择",
                       style="yellow" if pinned_lab else "grey50")

        current = state.get("current") or {}
        upcoming = state.get("next") or {}
        rows = Text()
        rows.append("当前 ", style="grey50")
        rows.append(_slot(current) or "—", style="cyan")
        rows.append("      下一 ", style="grey50")
        rows.append(_slot(upcoming) or "—", style="yellow")
        rows.append("      剩余 %s / %.1f 小时" % (_mmss(left), self.cfg.run.max_runtime_hours),
                    style="grey50")

        grid = self._grid(state.get("cells") or [])
        menu = self._menu()
        # the hint line is permanent; status messages stack underneath it
        hint = Text("hjkl 或 ↑↓←→ 移动 · 空格/Enter 选择 · m 切换模式 · u 配置账号 · q 退出",
                    style="grey50")
        parts = [head, self._rule(), status, labline, rows, self._rule(),
                 Text("今日时段 (%d)" % len(state.get("cells") or []), style="white"),
                 grid, self._rule(), menu, self._rule(), hint]
        if self.message:
            parts.append(Text(self.message, style="yellow"))
        return Group(*parts)

    def _grid(self, cells: List[dict]) -> Table:
        table = Table.grid(padding=(0, 1))
        per_row = 8
        for start in range(0, len(cells), per_row):
            chunk = cells[start:start + per_row]
            row = []
            for cell in chunk:
                label = cell["time"][11:16]
                mark = {"booked": "约", "started": "用", "free": "空", "past": "过"}[cell["state"]]
                style = COLORS[cell["state"]]
                if cell.get("past"):
                    style += " strike"
                row.append(Text("%s %s" % (label, mark), style=style))
            table.add_row(*row)
        return table

    def _menu(self) -> Table:
        table = Table.grid(padding=(0, 3))
        for start in range(0, len(MENU), 3):
            row = []
            for index in range(start, min(start + 3, len(MENU))):
                label = MENU[index][1]
                if index == self.selected:
                    row.append(Text("> %s" % label, style="reverse"))
                else:
                    row.append(Text("  %s" % label, style="white"))
            table.add_row(*row)
        return table

    def _view_cancel(self) -> RenderableType:
        rows = self.state.get("appointments", [])
        table = Table.grid(padding=(0, 1))
        for index, appointment in enumerate(rows):
            box = "[x]" if appointment["id"] in self.checked else "[ ]"
            style = "reverse" if index == self.cancel_index else "white"
            table.add_row(Text("%s %s 实验#%s 台位 %s" % (box, appointment["time"],
                                                          appointment["experiment"],
                                                          appointment["desk"]), style=style))
        foot = Text("空格/x 勾选 · a 全选 · Enter 执行 · Esc 返回", style="grey50")
        picked = len(self.checked)
        return Group(
            self._heading("取消预约"),
            self._rule(),
            Text("已勾选 %d 条" % picked, style="grey50"),
            Text(""),
            table,
            self._rule(),
            foot,
        )

    def _view_exit(self) -> RenderableType:
        count = len(self.state.get("appointments", []))
        items = [(key, title, detail.replace("取消剩余预约", "取消剩余 %d 条预约" % count))
                 for key, title, detail in EXIT_OPTIONS]
        table = Table.grid(padding=(0, 2))
        for index, (_, title, detail) in enumerate(items):
            marker = "> " if index == self.exit_index else "  "
            style = "reverse" if index == self.exit_index else "white"
            table.add_row(Text(marker + title, style=style), Text(detail, style="grey50"))
        return Group(
            self._heading("退出"),
            self._rule(),
            Text(""),
            table,
            self._rule(),
            Text("↑↓ 或 j k 选择 · 空格/Enter 确认 · Esc 返回", style="grey50"),
        )


def _slot(cell: dict) -> str:
    if not cell:
        return ""
    desk = cell.get("desk")
    text = cell.get("time", "")[11:16]
    return "%s 台位 %s" % (text, desk) if desk else text


def _mmss(seconds: int) -> str:
    seconds = max(0, seconds)
    return "%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60)


def run_tui(cfg: Config) -> int:
    try:
        return Tui(cfg).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    from .config import Config as _Config

    sys.exit(run_tui(_Config.load()))

    def _forget_daemon(self, pid: int) -> None:
        self._child = None
        for name in ("daemon.pid", "daemon.lock"):
            try:
                (state_dir() / name).unlink(missing_ok=True)
            except OSError:
                pass
