"""Scheduling logic tests. No network, no credentials, no filesystem."""

from __future__ import annotations

from datetime import datetime

from ycsy import planner
from ycsy.config import Coverage
from ycsy.model import Experiment


def make_experiment(**kwargs) -> Experiment:
    base = dict(id=1, name="exp", limit_count=100, limit_time=20,
                lab_start="09:00", lab_end="22:00")
    base.update(kwargs)
    return Experiment(**base)


def test_slot_grid_steps_by_limit_time():
    exp = make_experiment()
    slots = planner.slot_grid(exp, Coverage(), "2026-10-18")
    assert slots[0] == "2026-10-18 09:00"
    assert slots[1] == "2026-10-18 09:20"
    assert slots[-1] == "2026-10-18 21:40"
    assert len(slots) == 39


def test_slot_grid_respects_coverage_window():
    exp = make_experiment()
    cov = Coverage(day_start="19:00", day_end="20:00")
    assert planner.slot_grid(exp, cov, "2026-10-18") == [
        "2026-10-18 19:00",
        "2026-10-18 19:20",
        "2026-10-18 19:40",
    ]


def test_bookable_drops_slots_already_started():
    cov = Coverage(lead_minutes=0)
    now = datetime(2026, 10, 18, 19, 5)
    slots = ["2026-10-18 18:40", "2026-10-18 19:00", "2026-10-18 19:20"]
    assert planner.bookable(slots, cov, now) == ["2026-10-18 19:20"]


def test_rolling_targets_only_returns_bookable_slots():
    """A slot that already started can never be reserved, so it is not a target.

    Regression: with keep=1 the window used to sit on the current slot, so the
    watcher found nothing to book and stayed silent forever.
    """
    exps = [make_experiment()]
    cov = Coverage(days_ahead=1)
    now = datetime(2026, 10, 18, 19, 5)
    assert planner.rolling_targets(cov, exps, 2, now) == [
        "2026-10-18 19:20",
        "2026-10-18 19:40",
    ]
    assert planner.rolling_targets(cov, exps, 1, now) == ["2026-10-18 19:20"]


def test_rolling_targets_keep_one_mid_slot():
    exps = [make_experiment()]
    now = datetime(2026, 10, 18, 21, 16, 40)   # 21:00 slot already running
    targets = planner.rolling_targets(Coverage(days_ahead=1), exps, 1, now)
    assert targets == ["2026-10-18 21:20"]


def test_pick_experiment_skips_exhausted_quota():
    first = make_experiment(id=10)
    second = make_experiment(id=20)
    slot = "2026-10-18 10:00"
    used = {10: 100, 20: 0}
    assert planner.pick_experiment(slot, [first, second], used, {}) == second


def test_pick_experiment_skips_slot_owned_elsewhere():
    exp = make_experiment(id=10)
    slot = "2026-10-18 10:00"
    assert planner.pick_experiment(slot, [exp], {10: 0}, {slot: {10}}) is None


def test_pick_experiment_checks_lab_hours():
    exp = make_experiment(id=10, lab_start="09:00", lab_end="18:00")
    assert planner.pick_experiment("2026-10-18 20:00", [exp], {}, {}) is None
    assert planner.pick_experiment("2026-10-18 17:40", [exp], {}, {}) is not None
