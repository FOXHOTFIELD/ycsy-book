"""The one provider implemented so far: the remote-lab platform.

Notes learned the hard way, kept here so they never have to be rediscovered:

* ``/login`` only accepts a JSON body; other endpoints take form data.
* ``/api/appointment/getAppointmentInfo`` still lists *cancelled* records,
  so coverage is always computed from ``getAppinmentList`` (sic).
* ``/exper/experiment/list`` blows up server-side when ``pageSize > 10``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import requests

from ..model import Appointment, Desk, Experiment
from ..state import Store, redact


class ApiError(RuntimeError):
    pass


class RemoteLab:
    def __init__(self, api_base: str, web_base: str, store: Store, log=None,
                 seed_experiment: int = 206) -> None:
        self.base = api_base.rstrip("/")
        self.store = store
        self.log = log or (lambda message: None)
        self.seed_experiment = int(seed_experiment)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "ycsy-book/0.1 (+https://github.com/)",
                "language": "CN",
                "Origin": web_base.rstrip("/"),
                "Referer": web_base.rstrip("/") + "/",
            }
        )
        self.token = store.token

    # ------------------------------------------------------------- transport
    def login(self, username: str, password: str) -> str:
        response = self.session.post(
            self.base + "/login",
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        body = self._json(response, "/login")
        if int(body.get("code") or 0) != 200:
            raise ApiError("登录失败：%s" % body.get("msg"))
        token = body.get("token") or ""
        if not token:
            raise ApiError("登录返回里没有 token")
        self.token = token
        self.store.token = token
        return token

    def call(self, method: str, path: str, data: Optional[dict] = None,
             params: Optional[dict] = None, json_body: bool = False,
             retry_auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"} if json_body else {}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        kwargs: Dict[str, Any] = {"headers": headers, "timeout": 30}
        if params:
            kwargs["params"] = params
        if data is not None:
            if json_body:
                kwargs["json"] = data
            else:
                kwargs["data"] = {k: v for k, v in data.items() if v is not None}
        try:
            response = self.session.request(method, self.base + path, **kwargs)
        except requests.RequestException as exc:
            raise ApiError("网络错误 %s %s：%s" % (method, path, exc))

        body = self._json(response, path)
        code = int(body.get("code") or 0)
        if code == 401 and retry_auth:
            raise ApiError("认证失败，token 可能已过期，请重新登录")
        if code != 200:
            message = str(body.get("msg") or "")
            if not json_body and "Content type" in message:
                return self.call(method, path, data, params, json_body=True,
                                 retry_auth=retry_auth)
            raise ApiError("接口 %s 失败：code=%s msg=%s" % (path, code, redact(message)))
        return body

    @staticmethod
    def _json(response: requests.Response, path: str) -> dict:
        try:
            body = response.json()
        except ValueError:
            raise ApiError("接口 %s 返回的不是 JSON（HTTP %d）" % (path, response.status_code))
        if not isinstance(body, dict):
            raise ApiError("接口 %s 返回结构异常" % path)
        return body

    # -------------------------------------------------------------- provider
    def me(self) -> dict:
        body = self.call("GET", "/getInfo")
        return body.get("data") or body.get("user") or {}

    def experiment(self, experiment_id: int) -> Experiment:
        raw = self.call("GET", "/exper/experiment/%s" % experiment_id).get("data") or {}
        lab = {}
        lab_id = raw.get("labId")
        if lab_id is not None:
            try:
                lab = self.call("GET", "/schoolAffairs/lab/%s" % lab_id).get("data") or {}
            except ApiError as exc:
                self.log("实验室 %s 详情读取失败：%s" % (lab_id, exc))
        return Experiment.from_api(raw, lab)

    def experiments(self) -> List[Experiment]:
        seed = self.experiment(self.seed_experiment)
        course_id = seed.course_id
        rows: List[dict] = []
        page = 1
        while page <= 20:
            body = self.call("GET", "/exper/experiment/list",
                             params={"pageNum": page, "pageSize": 10, "courseId": course_id})
            chunk = body.get("rows") or []
            rows.extend(chunk)
            if len(chunk) < 10:
                break
            page += 1
        out = [seed]
        for row in rows:
            exp_id = row.get("experimentId")
            if exp_id is None or int(exp_id) == seed.id:
                continue
            if seed.lab_id is not None and row.get("labId") not in (None, seed.lab_id):
                continue
            out.append(self.experiment(int(exp_id)))
        return out

    def desks(self, experiment: Experiment, slot: str) -> List[Desk]:
        body = self.call(
            "POST",
            "/api/desk/getOrderDesklistByTimeAndExperimentId",
            data={
                "studentId": experiment.student_id,
                "experimentId": experiment.id,
                "time": slot,
                "minute": experiment.slot_minutes,
            },
        )
        rows = body.get("data") or []
        return [Desk.from_api(row) for row in rows if isinstance(row, dict)]

    def book(self, experiment: Experiment, desk: Desk, slot: str) -> None:
        self.call(
            "POST",
            "/api/appointment/experimentAppointment",
            data={"appointmentTime": slot, "experimentId": experiment.id, "deskId": desk.id},
        )

    def appointments(self, experiment: Experiment) -> List[Appointment]:
        body = self.call("POST", "/api/appointment/getAppoinmentList",
                         data={"experimentId": experiment.id})
        rows = body.get("data") or []
        return [Appointment.from_api(row) for row in rows if isinstance(row, dict)]

    def cancel(self, appointment_id: int) -> None:
        self.call("POST", "/api/appointment/cancelAppoinment", data={"id": appointment_id})

    def history(self, student_id: int) -> Dict[int, int]:
        """Appointments that already expired, per experiment.

        The platform's own list still contains cancelled records, so this is an
        upper bound - being conservative is fine for a quota guard.
        """
        counts: Dict[int, int] = {}
        seen = 0
        page = 1
        while page <= 10:
            body = self.call("GET", "/api/appointment/getAppointmentInfo",
                             params={"pageNum": page, "pageSize": 100,
                                     "studentId": student_id, "isOverdue": "true"})
            rows = body.get("rows") or []
            seen += len(rows)
            for row in rows:
                exp_id = row.get("experimentId")
                if exp_id is not None:
                    counts[int(exp_id)] = counts.get(int(exp_id), 0) + 1
            total = int(body.get("total") or 0)
            if len(rows) < 100 or seen >= total:
                break
            page += 1
        return counts

    def remote_link(self, desk_id: int) -> Tuple[str, dict]:
        desk = self.call("GET", "/exper/desk/%s" % desk_id).get("data") or {}
        if int(desk.get("conn") or 0) == 0:
            body = self.call(
                "POST",
                "/api/appointment/getVaResponseData",
                data={
                    "url": desk.get("remoteUrl"),
                    "userName": desk.get("userName"),
                    "passWord": desk.get("password"),
                },
            )
            data = body.get("data") or {}
            apps = data.get("AppList") or []
            tail = apps[0][3] if apps and len(apps[0]) > 3 else ""
            return str(data.get("RaUrlHead") or "") + str(tail), desk
        return str(desk.get("remoteUrl") or ""), desk

    def server_time(self) -> str:
        return str(self.call("GET", "/exper/appointment/getDate").get("msg") or "")

    def start(self, appointment_id: int) -> None:
        """Same as clicking 开始实验: marks the appointment as started."""
        self.call("PUT", "/exper/appointment",
                  data={"id": appointment_id, "isOverdue": True}, json_body=True)
