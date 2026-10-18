"""Pure scheduling logic: slot grid, rolling window, experiment choice.

Nothing in here touches the network or the filesystem, which is what makes it
easy to unit-test and easy to reason about.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Set

from .config import MAX_KEEP, Coverage
from .model import Experiment

FMT = "%Y-%m-%d %H:%M"


def parse_dt(text: str) -> datetime:
    try:
        return datetime.strptime(text, FMT)
    except (ValueError, TypeError):
        return datetime(1970, 1, 1)


def minutes(text: str) -> int:
    hour, _, minute = str(text).partition(":")
    return int(hour) * 60 + int(minute)


def clock(total: int) -> str:
    return "%02d:%02d" % (total // 60, total % 60)


def coverage_days(coverage: Coverage, now: Optional[datetime] = None) -> List[str]:
    now = now or datetime.now()
    return [(now + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(max(1, coverage.days_ahead))]


def slot_grid(experiment: Experiment, coverage: Coverage, day: str) -> List[str]:
    """Bookable start times for one experiment on one day.

    Mirrors the web UI: start at the lab's opening time, end at closing time,
    and step by the experiment's own ``limitTime``.
    """
    start = max(experiment.lab_start, coverage.day_start)
    end = min(experiment.lab_end, coverage.day_end)
    step = coverage.slot_minutes or experiment.slot_minutes
    if step <= 0:
        return []
    out: List[str] = []
    cursor = minutes(start)
    limit = minutes(end)
    while cursor + step <= limit:
        out.append("%s %s" % (day, clock(cursor)))
        cursor += step
    return out


def all_slots(coverage: Coverage, experiments: Sequence[Experiment],
              now: Optional[datetime] = None) -> List[str]:
    seen: Set[str] = set()
    for day in coverage_days(coverage, now):
        for experiment in experiments:
            seen.update(slot_grid(experiment, coverage, day))
    return sorted(seen)


def rolling_targets(coverage: Coverage, experiments: Sequence[Experiment], keep: int,
                    now: Optional[datetime] = None) -> List[str]:
    """The next ``keep`` slots that can still be booked **today**.

    Two rules:

    * only slots that have not started yet count - a started slot can never be
      reserved, so including it would make ``keep=1`` sit on the current slot
      and never book anything at all;
    * booking never crosses midnight: tomorrow's slots only become candidates
      once tomorrow actually starts.

    ``keep`` is clamped to ``MAX_KEEP`` here as well, so no caller can ask for a
    whole-day reservation even by mistake.
    """
    keep = max(1, min(int(keep or 1), MAX_KEEP))
    if not experiments:
        return []
    now = now or datetime.now()
    cutoff = now + timedelta(minutes=coverage.lead_minutes)
    today = now.strftime("%Y-%m-%d")
    slots = all_slots(coverage, experiments, now)
    return [s for s in slots if s.startswith(today) and parse_dt(s) > cutoff][:keep]


def bookable(slots: Iterable[str], coverage: Coverage,
             now: Optional[datetime] = None) -> List[str]:
    """Drop slots whose start time has already passed: booking them wastes quota."""
    now = now or datetime.now()
    cutoff = now + timedelta(minutes=coverage.lead_minutes)
    return [s for s in slots if parse_dt(s) > cutoff]


def candidate_experiments(slot: str, experiments: Sequence[Experiment],
                          used: Dict[int, int],
                          occupied: Dict[str, Set[int]]) -> List[Experiment]:
    """Every experiment that could legally take this slot, in preference order.

    The watcher walks this list until one of them actually has a free bench, so
    a full quota (or a lab with no spare desk) moves you to the next experiment
    automatically.
    """
    at = minutes(slot.split(" ")[1])
    out: List[Experiment] = []
    for experiment in experiments:
        if experiment.limit_count and used.get(experiment.id, 0) >= experiment.limit_count:
            continue
        if experiment.id in occupied.get(slot, set()):
            continue
        if not (minutes(experiment.lab_start) <= at < minutes(experiment.lab_end)):
            continue
        out.append(experiment)
    return out


def pick_experiment(slot: str, experiments: Sequence[Experiment],
                    used: Dict[int, int], occupied: Dict[str, Set[int]]) -> Optional[Experiment]:
    """First experiment that still has quota and is open at that time."""
    candidates = candidate_experiments(slot, experiments, used, occupied)
    return candidates[0] if candidates else None


def remaining_quota(experiments: Sequence[Experiment],
                    used: Dict[int, int]) -> List[Dict[str, int]]:
    """How many bookings each experiment still allows (for the status line)."""
    out = []
    for experiment in experiments:
        used_count = used.get(experiment.id, 0)
        out.append({
            "experiment": experiment.id,
            "name": experiment.name,
            "limit": experiment.limit_count,
            "used": used_count,
            "left": max(0, experiment.limit_count - used_count) if experiment.limit_count else 0,
        })
    return out
