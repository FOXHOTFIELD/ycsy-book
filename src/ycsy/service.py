"""Orchestration: ties the provider, the planner and local state together.

The CLI and the TUI both talk to this class, which is why neither of them
contains any booking rules.

Three limits of a released build live here:

* never hold more than ``MAX_KEEP`` bookings at the same time
* never run longer than ``MAX_RUNTIME_HOURS`` in one session
* stop immediately when the optional maintenance channel says so
"""

from __future__ import annotations

import datetime as dt
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import planner
from .client import close_remote, open_remote
from .config import MAX_KEEP, MAX_RUNTIME_HOURS, Config, log_path, state_dir
from .model import Appointment, Experiment
from .policy import Policy
from .providers import ApiError, RemoteLab
from .state import FileLock, Store, pid_alive, redact


@dataclass
class Plan:
    slots: List[str]
    covered: Set[str]
    used: Dict[int, int]
    occupied: Dict[str, Set[int]]
    experiments: List[Experiment]
    active: int = 0
    appointments: List[Appointment] = field(default_factory=list)
    # appointments grouped by the experiment they belong to; the flat list
    # above loses that association and pairing it back up is how the UI ended
    # up showing every appointment once per experiment
    rows: Dict[int, List[Appointment]] = field(default_factory=dict)

    @property
    def pending(self) -> List[str]:
        return [s for s in self.slots if s not in self.covered]

    @property
    def room(self) -> int:
        """How many more bookings this session is allowed to create."""
        return max(0, MAX_KEEP - self.active)


class Service:
    def __init__(self, cfg: Config, dry_run: bool = False, quiet: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.quiet = quiet
        self.store = Store(state_dir())
        self.provider = RemoteLab(
            cfg.api_base,
            cfg.web_base,
            self.store,
            log=self.log,
            seed_experiment=cfg.experiments[0] if cfg.experiments else 206,
        )
        self.policy = Policy(cfg.policy_urls, self.log)
        self.started_at = dt.datetime.now()
        hours = min(float(cfg.run.max_runtime_hours or MAX_RUNTIME_HOURS), MAX_RUNTIME_HOURS)
        self.deadline = self.started_at + dt.timedelta(hours=hours)
        self._me: Optional[dict] = None
        self._recent: Dict[str, float] = {}

    # ------------------------------------------------------------- profile
    def forget_profile(self) -> None:
        """Drop the cached account profile (after switching accounts)."""
        self._me = None

    def me(self) -> dict:
        """Account profile, fetched once per session (shown in the TUI header)."""
        if self._me is None:
            try:
                self._me = self.provider.me()
            except ApiError:
                self._me = {}
        return self._me or {}

    # ------------------------------------------------------------- plumbing
    def log(self, message: str) -> None:
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = "[%s] %s" % (stamp, redact(str(message)))
        if not self.quiet:
            print(line, flush=True)
        try:
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def login(self) -> None:
        self.cfg.require_credentials()
        if self.store.token:
            return
        self.provider.login(self.cfg.username, self.cfg.password)
        self.log("登录成功")

    # ---------------------------------------------------------------- policy
    def guard(self, force: bool = False) -> bool:
        """False means: the maintenance channel asked us to stop."""
        allowed, message = self.policy.check(force=force)
        if not allowed:
            self.log(message or "本项目已停止维护，请卸载")
            return False
        return True

    @property
    def time_left(self) -> dt.timedelta:
        return self.deadline - dt.datetime.now()

    @property
    def expired(self) -> bool:
        return dt.datetime.now() >= self.deadline

    # ----------------------------------------------------------- experiments
    # Manual override: a plain JSON file the user can edit while the tool runs.
    #   {"lab_id": 12, "experiments": [206, 114]}
    # It wins over automatic discovery, so switching lab or experiment never
    # requires touching the TUI.
    def override_path(self) -> Path:
        """Lives next to config.json so it is easy to find and edit."""
        return Config.home_config_path().with_name("override.json")

    def override(self) -> Dict:
        try:
            data = json.loads(self.override_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def experiments(self) -> List[Experiment]:
        override = self.override()
        pinned = [int(x) for x in (override.get("experiments") or [])] or list(self.cfg.experiments)
        if pinned:
            return [self.provider.experiment(exp_id) for exp_id in pinned]

        today = dt.date.today().isoformat()
        cache = self.store.cache()
        if cache.get("day") == today and cache.get("experiments"):
            items = [Experiment(**row) for row in cache["experiments"]]
        else:
            items = self.provider.experiments()
            self.store.save_cache(
                {"day": today, "experiments": [item.__dict__ for item in items]}
            )

        lab_id = override.get("lab_id")
        if lab_id is None and self.cfg.lab_id:
            lab_id = self.cfg.lab_id
        if lab_id is not None:
            items = [item for item in items if item.lab_id == int(lab_id)]
        return items

    # ---------------------------------------------------------------- plan
    def plan(self) -> Plan:
        experiments = self.experiments()
        covered: Set[str] = set()
        occupied: Dict[str, Set[int]] = {}
        used: Dict[int, int] = {}
        appointments: List[Appointment] = []
        rows_by_experiment: Dict[int, List[Appointment]] = {}
        for experiment in experiments:
            rows = self.provider.appointments(experiment)
            used[experiment.id] = len(rows)
            rows_by_experiment[experiment.id] = rows
            appointments.extend(rows)
            for row in rows:
                if not row.when:
                    continue
                covered.add(row.when)
                occupied.setdefault(row.when, set()).add(experiment.id)

        targets = planner.rolling_targets(self.cfg.coverage, experiments, self.cfg.run.keep)
        slots = planner.bookable(targets, self.cfg.coverage)

        # quota guard: count expired history too, otherwise "remaining" lies
        student_id = next((e.student_id for e in experiments if e.student_id), None)
        if student_id:
            try:
                history = self.provider.history(int(student_id))
            except ApiError as exc:
                history = {}
                self.log("历史次数读取失败（按现有记录计）：%s" % exc)
            for exp_id, count in history.items():
                used[exp_id] = used.get(exp_id, 0) + count

        return Plan(slots=slots, covered=covered, used=used, occupied=occupied,
                    experiments=experiments, active=len(appointments),
                    appointments=appointments, rows=rows_by_experiment)

    # -------------------------------------------------------------- booking
    def run_pass(self) -> int:
        if not self.guard():
            raise SystemExit(0)
        plan = self.plan()
        room = plan.room
        if room <= 0:
            self.log("已达同时预约上限 %d 条，本轮不再下单" % MAX_KEEP)
            return 0
        pending = plan.pending[:room]
        if not pending:
            self.log("本轮没有待补的时段（目标 %d 格，已有 %d 条）"
                     % (len(plan.slots), plan.active))
        limit = int(self.cfg.run.max_book_per_pass or 0)
        booked = 0
        for slot in pending:
            if limit and booked >= limit:
                self.log("达到单轮上限 %d" % limit)
                break
            candidates = planner.candidate_experiments(
                slot, plan.experiments, plan.used, plan.occupied)
            if not candidates:
                self.log("%s 没有可用实验（次数或时段受限）" % slot)
                continue
            # walk the candidates: a spent quota or a fully booked lab moves us
            # to the next experiment for the same slot
            for experiment in candidates:
                if self._book(experiment, slot, plan):
                    booked += 1
                    break
                self.log("%s 换下一个实验" % slot)
        if self.cfg.run.auto_start and not self.dry_run:
            booked += self._auto_start(plan)
        return booked

    def _book(self, experiment: Experiment, slot: str, plan: Plan) -> bool:
        try:
            desks = self.provider.desks(experiment, slot)
        except ApiError as exc:
            self.log("%s 台位查询失败：%s" % (slot, exc))
            return False
        free = [d for d in desks if d.is_free]
        if not free:
            self.log("%s 没有空闲台位" % slot)
            return False
        desk = free[0]
        if self._recent.get(slot) and time.time() - self._recent[slot] < 600:
            self.log("%s 刚下过单，先跳过（防重复预约）" % slot)
            return False
        if self.dry_run:
            self.log("[dry-run] %s 实验#%s 台位 %s" % (slot, experiment.id, desk.name))
            plan.covered.add(slot)
            return True
        try:
            self.provider.book(experiment, desk, slot)
        except ApiError as exc:
            self.log("%s 下单失败：%s" % (slot, exc))
            return False
        if self.cfg.run.verify:
            rows = self.provider.appointments(experiment)
            if not any(r.when == slot and r.desk_id == desk.id for r in rows):
                self.log("%s 下单后未确认，按失败处理" % slot)
                return False
        plan.covered.add(slot)
        plan.used[experiment.id] = plan.used.get(experiment.id, 0) + 1
        self._recent[slot] = time.time()
        self.log("已预约 %s 实验#%s 台位 %s" % (slot, experiment.id, desk.name))
        return True

    def _auto_start(self, plan: Plan) -> int:
        started = 0
        now = dt.datetime.now()
        known = {row.get("id") for row in self.store.started()}
        for experiment in plan.experiments:
            for row in self.provider.appointments(experiment):
                start = planner.parse_dt(row.when)
                if not (start <= now < start + dt.timedelta(minutes=experiment.slot_minutes)):
                    continue
                if row.id in known:
                    continue
                if self.start(row, experiment):
                    started += 1
        return started

    def start(self, appointment: Appointment, experiment: Experiment) -> bool:
        try:
            self.provider.start(appointment.id)
            url, desk = self.provider.remote_link(appointment.desk_id)
        except ApiError as exc:
            self.log("开始实验失败：%s" % exc)
            return False
        self.store.save_session(
            {"appointment_id": appointment.id, "experiment_id": experiment.id,
             "desk_id": appointment.desk_id, "url": url,
             "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )
        self.store.record_started(
            {"id": appointment.id, "time": appointment.when,
             "experiment": experiment.id, "desk": appointment.desk_id}
        )
        self.log("已开始实验 %s 台位 %s（%s）"
                 % (appointment.when, desk.get("deskNo") or appointment.desk_id, redact(url)))
        if self.cfg.run.auto_open:
            open_remote(url, self.log)
        return True

    # ------------------------------------------------------------- watching
    def close_client(self) -> bool:
        return close_remote(self.log, name=self.cfg.client_process)

    def watch(self, once: bool = False, parent_pid: Optional[int] = None) -> None:
        """Loop until done.

        ``parent_pid`` is the UI that spawned us. If that process disappears
        (window closed with the X button, console killed, ...) this watcher
        exits too, so closing the window never leaves a booking daemon behind.
        """
        lock = FileLock(self.store.dir / "daemon.lock", pid_path=self.store.dir / "daemon.pid")
        with lock:
            self.log("值守开始，最长运行 %.1f 小时，最多同时 %d 条预约"
                     % (self.cfg.run.max_runtime_hours, MAX_KEEP))
            while True:
                if self.expired:
                    self.log("已运行满 %.1f 小时，自动退出" % self.cfg.run.max_runtime_hours)
                    return
                if parent_pid and not pid_alive(parent_pid):
                    self.log("启动它的界面已经退出了，值守随之结束")
                    return
                try:
                    booked = self.run_pass()
                except ApiError as exc:
                    self.log("本轮失败：%s" % exc)
                    booked = 0
                if once:
                    return
                if booked:
                    low, high = self.cfg.run.interval_sec
                    wait = random.randint(int(low), int(high))
                else:
                    wait = int(self.cfg.run.keep_poll_sec)
                time.sleep(max(1, min(wait, int(self.time_left.total_seconds()) or 1)))

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        plan = self.plan()
        experiments = plan.experiments
        step = min((e.slot_minutes for e in experiments), default=20)
        now = dt.datetime.now()
        day = now.strftime("%Y-%m-%d")
        started = self.store.started_today(day)
        cells = []
        for slot in planner.all_slots(self.cfg.coverage, experiments):
            if not slot.startswith(day):
                continue
            begin = planner.parse_dt(slot)
            end = begin + dt.timedelta(minutes=step)
            state = "free"
            desk = None
            if slot in plan.covered:
                state = "booked"
            elif slot in started:
                state = "started"
                desk = started[slot].get("desk")
            elif begin <= now:
                # a slot that already started can no longer be booked:
                # unbooked means missed, not "free"
                state = "past"
            # strike through anything that is over, or that we can no longer book
            strike = end <= now or state == "past"
            cells.append({"time": slot, "state": state, "past": strike, "desk": desk})
        current = next((c for c in cells
                        if planner.parse_dt(c["time"]) <= now <
                        planner.parse_dt(c["time"]) + dt.timedelta(minutes=step)), None)
        upcoming = next((c for c in cells if planner.parse_dt(c["time"]) > now), None)
        names = {e.id: e.name for e in experiments}
        appointments = sorted(
            ({"id": r.id, "time": r.when, "experiment": exp_id,
              "name": names.get(exp_id, ""), "desk": r.desk_id}
             for exp_id, rows in plan.rows.items() for r in rows),
            key=lambda item: item["time"])
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "user": {"login": str(self.me().get("userName") or ""),
                     "name": str(self.me().get("nickName") or "")},
            "auto_start": bool(self.cfg.run.auto_start),
            "keep": self.cfg.run.keep,
            "active": plan.active,
            "max_keep": MAX_KEEP,
            "step": step,
            "lab": {"name": experiments[0].lab_name if experiments else "",
                    "start": experiments[0].lab_start if experiments else "",
                    "end": experiments[0].lab_end if experiments else ""},
            "cells": cells,
            "current": current,
            "next": upcoming,
            "appointments": appointments,
            "quota": planner.remaining_quota(experiments, plan.used),
            "override": self.override(),
            "override_path": str(self.override_path()),
            "runtime": {
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "deadline": self.deadline.strftime("%Y-%m-%d %H:%M:%S"),
                "left_seconds": max(0, int(self.time_left.total_seconds())),
            },
            "session": self.store.session(),
        }
