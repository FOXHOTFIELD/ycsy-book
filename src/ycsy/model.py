"""Domain objects.

Kept deliberately dumb: parsing from the platform's JSON happens once,
in ``Experiment.from_api`` / ``Desk.from_api``, so the rest of the code
never has to guess field names again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def _first(raw: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return default


@dataclass(frozen=True)
class Experiment:
    id: int
    name: str
    course_id: Optional[int] = None
    lab_id: Optional[int] = None
    lab_name: str = ""
    lab_start: str = "08:00"
    lab_end: str = "22:00"
    limit_count: int = 0
    limit_time: int = 0
    student_id: Optional[int] = None

    @classmethod
    def from_api(cls, raw: dict, lab: Optional[dict] = None) -> "Experiment":
        lab = lab or {}
        return cls(
            id=int(_first(raw, "experimentId", "id")),
            name=str(_first(raw, "experimentName", "name", default="")),
            course_id=_as_int(_first(raw, "courseId")),
            lab_id=_as_int(_first(raw, "labId", "labID")),
            lab_name=str(_first(lab, "labName", "name", default="")),
            lab_start=clock(_first(lab, "startTime"), "08:00"),
            lab_end=clock(_first(lab, "endTime"), "22:00"),
            limit_count=_as_int(_first(raw, "limitCount")) or 0,
            limit_time=_as_int(_first(raw, "limitTime")) or 0,
            student_id=_as_int(_first(raw, "userId", "studentId")),
        )

    @property
    def slot_minutes(self) -> int:
        return self.limit_time or 20


@dataclass(frozen=True)
class Desk:
    id: int
    name: str
    flag: int  # 0 free, 1 taken by someone else, 2 taken by me

    @classmethod
    def from_api(cls, raw: dict) -> "Desk":
        return cls(
            id=int(_first(raw, "id")),
            name=str(_first(raw, "description", "deskNo", default="")),
            flag=_as_int(_first(raw, "flag")) or 0,
        )

    @property
    def is_free(self) -> bool:
        return self.flag == 0


@dataclass(frozen=True)
class Appointment:
    id: int
    experiment_id: int
    desk_id: int
    when: str  # "YYYY-MM-DD HH:MM"
    desk_name: str = ""

    @classmethod
    def from_api(cls, raw: dict) -> "Appointment":
        return cls(
            id=int(_first(raw, "id")),
            experiment_id=_as_int(_first(raw, "experimentId")) or 0,
            desk_id=_as_int(_first(raw, "deskId")) or 0,
            when=when(_first(raw, "appointmentTime")),
            desk_name=str(_first(raw, "description", default="")),
        )


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clock(value: Any, default: str) -> str:
    """'2026-10-18 09:00:00' / '09:00' -> '09:00'."""
    if not value:
        return default
    text = str(value).strip()
    if " " in text:
        text = text.split(" ")[-1]
    parts = text.split(":")
    if len(parts) < 2:
        return default
    try:
        return "%02d:%02d" % (int(parts[0]), int(parts[1]))
    except ValueError:
        return default


def when(value: Any) -> str:
    """'2026-10-18 19:00:00' -> '2026-10-18 19:00'."""
    if not value:
        return ""
    text = str(value).strip().replace("T", " ")
    head, _, tail = text.partition(" ")
    return "%s %s" % (head, clock(tail, "00:00"))
