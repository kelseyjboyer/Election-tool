"""Alert policy: priority, throttling, and turning events into Alert records.

Rules:
- Calls, retractions, call changes, lead changes, anomalies, status flags, milestones
  and chamber events always go through.
- New-batch alerts are throttled per race (window depends on critical / normal / low).
  A suppressed batch does not move the baseline, so the next one reports the full change.
- Races flagged test_data never alert.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import detect as d
from .config import RaceConfig
from .format import render
from .model import Snapshot

# ntfy priorities: 1 min, 2 low, 3 default, 4 high, 5 urgent
BASE_PRIORITY = {
    d.CALL_RETRACTED: 5,
    d.CALL_CHANGED: 5,
    d.CHAMBER_MAJORITY: 5,
    d.CALL: 4,
    d.RUNOFF: 4,
    d.LEAD_CHANGE: 4,
    d.ANOMALY: 4,
    d.STATUS_FLAG: 4,
    d.CHAMBER_SEATS: 3,
    d.MILESTONE: 3,
    d.FIRST_RESULTS: 3,
    d.NEW_BATCH: 2,
}

TAGS = {
    d.CALL_RETRACTED: ["rotating_light", "warning"],
    d.CALL_CHANGED: ["rotating_light", "warning"],
    d.CALL: ["ballot_box"],
    d.RUNOFF: ["repeat"],
    d.LEAD_CHANGE: ["arrows_counterclockwise"],
    d.ANOMALY: ["warning"],
    d.STATUS_FLAG: ["triangular_flag_on_post"],
    d.MILESTONE: ["bar_chart"],
    d.FIRST_RESULTS: ["bar_chart"],
    d.NEW_BATCH: ["bar_chart"],
    d.CHAMBER_SEATS: ["classical_building"],
    d.CHAMBER_MAJORITY: ["classical_building", "rotating_light"],
}


@dataclass
class Alert:
    alert_id: str
    race_id: int
    kind: str
    priority: int
    title: str
    body: str
    tags: list[str] = field(default_factory=list)
    last_updated: str | None = None
    critical: bool = False


@dataclass
class Decision:
    alerts: list[Alert]
    suppressed: list[d.Event]
    advance_baseline: bool   # true when a batch alert was emitted


def priority_for(kind: str, race: RaceConfig) -> int:
    p = BASE_PRIORITY.get(kind, 3)
    if race.critical and kind in (d.CALL, d.LEAD_CHANGE, d.NEW_BATCH, d.FIRST_RESULTS, d.MILESTONE):
        p += 1
    if race.priority == "low" and kind in (d.NEW_BATCH, d.FIRST_RESULTS, d.MILESTONE):
        p -= 1
    return max(1, min(5, p))


def decide(events: list[d.Event], snap: Snapshot, race: RaceConfig, *, now: float,
           last_batch_alert_at: float | None, throttle_seconds: float,
           tz: str = "America/Los_Angeles", only_kinds=None,
           extra_lines: list[str] | None = None) -> Decision:
    """only_kinds: if set, events of other kinds are suppressed.
    extra_lines: appended to call and chamber alerts (used for the chamber seat count)."""
    alerts: list[Alert] = []
    if only_kinds:
        suppressed_kinds = [e for e in events if e.kind not in only_kinds]
        events = [e for e in events if e.kind in only_kinds]
    else:
        suppressed_kinds = []
    suppressed: list[d.Event] = []
    advance = False

    if snap.test_data:
        return Decision([], list(events), False)

    label = race.label or snap.name
    batch_allowed = (last_batch_alert_at is None
                     or now - last_batch_alert_at >= throttle_seconds)

    batch_sent = any(e.kind == d.FIRST_RESULTS or (e.kind == d.NEW_BATCH and batch_allowed)
                     for e in events)
    milestone = next((e for e in events if e.kind == d.MILESTONE), None)

    for ev in events:
        if ev.kind in d.BATCH_KINDS:
            # First results always get through; later batches respect the window.
            if ev.kind == d.NEW_BATCH and not batch_allowed:
                suppressed.append(ev)
                continue
            advance = True
            if milestone is not None:
                ev.details["milestones"] = milestone.details["milestones"]
        elif ev.kind == d.MILESTONE and batch_sent:
            continue  # folded into the batch alert so one poll sends one push
        extra = extra_lines if ev.kind in d.CALL_KINDS or ev.kind == d.CHAMBER_MAJORITY else None
        title, body = render(ev, snap, label, tz, extra)
        tags = list(TAGS.get(ev.kind, []))
        if race.critical:
            tags.append("star")
        alerts.append(Alert(
            alert_id=ev.alert_id, race_id=snap.race_id, kind=ev.kind,
            priority=priority_for(ev.kind, race), title=title, body=body, tags=tags,
            last_updated=snap.last_updated, critical=race.critical,
        ))
    return Decision(alerts, suppressed + suppressed_kinds, advance)
