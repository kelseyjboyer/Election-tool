"""Change detection. Pure functions: (previous snapshot, current snapshot) -> events.

No network, no clock, no delivery. Throttling and wording live elsewhere.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .model import Candidate, Snapshot, cand_key

# Event kinds
ANOMALY = "anomaly"
FIRST_RESULTS = "first_results"
NEW_BATCH = "new_batch"
LEAD_CHANGE = "lead_change"
CALL = "call"
CALL_RETRACTED = "call_retracted"
CALL_CHANGED = "call_changed"
STATUS_FLAG = "status_flag"
MILESTONE = "milestone"
CHAMBER_SEATS = "chamber_seats"
CHAMBER_MAJORITY = "chamber_majority"

CALL_KINDS = {CALL, CALL_RETRACTED, CALL_CHANGED}
BATCH_KINDS = {FIRST_RESULTS, NEW_BATCH}

PERCENT_SUM_TOLERANCE = 2.0      # percents may be rounded
PERCENT_VS_VOTES_TOLERANCE = 3.0
MIN_VOTES_FOR_PERCENT_CHECK = 100


@dataclass
class Event:
    kind: str
    race_id: int
    key: str                      # stable dedup key within (race, kind)
    details: dict = field(default_factory=dict)

    @property
    def alert_id(self) -> str:
        raw = f"{self.race_id}|{self.kind}|{self.key}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]


# ---------------------------------------------------------------- anomalies
def find_anomalies(prev: Snapshot | None, cur: Snapshot) -> list[str]:
    issues: list[str] = []
    if not cur.candidates:
        issues.append("Race has no candidates in the response.")
    pr = cur.percent_reporting
    if pr is not None and not (0 <= pr <= 100):
        issues.append(f"percent_reporting is {pr}, outside 0 to 100.")
    for c in cur.candidates:
        if c.votes < 0:
            issues.append(f"{c.name} has negative votes ({c.votes}).")

    total = cur.total_votes
    if not cur.is_chamber and total > 0:
        s = sum(c.percent for c in cur.candidates)
        if abs(s - 100.0) > PERCENT_SUM_TOLERANCE:
            issues.append(f"Candidate percents add up to {s:.1f}, not about 100.")
        if total >= MIN_VOTES_FOR_PERCENT_CHECK:
            for c in cur.candidates:
                expected = 100.0 * c.votes / total
                if abs(expected - c.percent) > PERCENT_VS_VOTES_TOLERANCE:
                    issues.append(f"{c.name}: percent {c.percent:.1f} does not match votes "
                                  f"({expected:.1f} expected).")
    if not cur.is_chamber and len(cur.winners) > 1:
        names = ", ".join(c.name for c in cur.winners)
        issues.append(f"More than one winner flagged ({names}).")

    if prev is not None:
        prev_c = prev.candidate_by_key()
        cur_c = cur.candidate_by_key()
        missing = [prev_c[k].name for k in prev_c if k not in cur_c]
        if missing:
            issues.append("Candidate(s) missing since last poll: " + ", ".join(missing) + ".")
        if not cur.is_chamber:
            if total < prev.total_votes:
                issues.append(f"Total votes went down: {prev.total_votes:,} to {total:,}.")
            for k, c in cur_c.items():
                p = prev_c.get(k)
                if p is not None and c.votes < p.votes:
                    issues.append(f"{c.name} votes went down: {p.votes:,} to {c.votes:,}.")
        if (prev.percent_reporting is not None and pr is not None
                and pr < prev.percent_reporting - 0.5):
            issues.append(f"percent_reporting went down: {prev.percent_reporting} to {pr}.")
    return issues


# ---------------------------------------------------------------- helpers
def _winner_ids(s: Snapshot | None) -> tuple:
    if s is None:
        return ()
    return tuple(sorted(str(cand_key(c)) for c in s.winners))


def _cand_summary(c: Candidate | None) -> dict | None:
    if c is None:
        return None
    return {"name": c.name, "party": c.party, "votes": c.votes, "percent": c.percent}


def _margin(s: Snapshot) -> dict | None:
    r = sorted(s.candidates, key=lambda c: c.votes, reverse=True)
    if len(r) < 2 or s.total_votes <= 0:
        return None
    return {"votes": r[0].votes - r[1].votes,
            "points": round(100.0 * (r[0].votes - r[1].votes) / s.total_votes, 2)}


def _call_events(prev: Snapshot | None, cur: Snapshot) -> list[Event]:
    before, after = _winner_ids(prev), _winner_ids(cur)
    if before == after:
        return []
    stamp = f"{cur.last_updated}|{cur.total_votes}"
    prev_names = [c.name for c in prev.winners] if prev else []
    cur_names = [c.name for c in cur.winners]
    details = {"previous_winners": prev_names, "winners": cur_names,
               "winner_detail": [_cand_summary(c) for c in cur.winners]}
    if not before:
        return [Event(CALL, cur.race_id, f"{','.join(after)}|{stamp}", details)]
    if not after:
        return [Event(CALL_RETRACTED, cur.race_id, f"{','.join(before)}|{stamp}", details)]
    return [Event(CALL_CHANGED, cur.race_id, f"{','.join(before)}>{','.join(after)}|{stamp}", details)]


def _chamber_events(prev: Snapshot, cur: Snapshot, majority: int | None) -> list[Event]:
    events = []
    before = {c.name: c.seats or 0 for c in prev.candidates}
    after = {c.name: c.seats or 0 for c in cur.candidates}
    if before != after:
        changes = {n: (before.get(n, 0), after.get(n, 0))
                   for n in set(before) | set(after) if before.get(n, 0) != after.get(n, 0)}
        key = ",".join(f"{n}={after.get(n, 0)}" for n in sorted(after))
        events.append(Event(CHAMBER_SEATS, cur.race_id, key, {"changes": changes, "seats": after,
                                                             "majority": majority}))
    if majority:
        for name, seats in after.items():
            was = before.get(name, 0)
            if was < majority <= seats:
                events.append(Event(CHAMBER_MAJORITY, cur.race_id, f"{name}|reached|{majority}",
                                    {"party": name, "seats": seats, "majority": majority,
                                     "reached": True}))
            elif seats < majority <= was:
                events.append(Event(CHAMBER_MAJORITY, cur.race_id,
                                    f"{name}|lost|{majority}|{cur.last_updated}",
                                    {"party": name, "seats": seats, "majority": majority,
                                     "reached": False}))
    return events


# ---------------------------------------------------------------- main entry
def detect(prev: Snapshot | None, cur: Snapshot, *, milestones=(25, 50, 75, 90),
           majority: int | None = None, baseline: Snapshot | None = None) -> list[Event]:
    """Compare snapshots and return events.

    prev      last polled snapshot (None on first observation)
    baseline  snapshot at the last *sent* batch alert, for 'change since last alert'
    """
    rid = cur.race_id

    # First observation: no diffs possible. Still surface an existing call so it is not missed.
    if prev is None:
        return _call_events(None, cur) if cur.winners else []

    events: list[Event] = []
    anomalies = find_anomalies(prev, cur)

    # Calls and retractions always get reported, even alongside an anomaly.
    call_events = _call_events(prev, cur)
    for e in call_events:
        e.details["during_anomaly"] = bool(anomalies)
    events.extend(call_events)

    if anomalies:
        key = hashlib.sha256(("|".join(anomalies) + f"|{cur.last_updated}").encode()).hexdigest()[:16]
        events.append(Event(ANOMALY, rid, key, {"issues": anomalies}))
        return events  # an anomaly replaces the normal update

    if cur.is_chamber:
        events.extend(_chamber_events(prev, cur, majority))
        return events

    # Status flags
    for flag in ("is_disputed", "is_delayed"):
        a, b = getattr(prev, flag), getattr(cur, flag)
        if a is not None and b is not None and a != b:
            events.append(Event(STATUS_FLAG, rid, f"{flag}={b}|{cur.last_updated}",
                                {"flag": flag, "old": a, "new": b}))

    # New results batch
    pr0, pr1 = prev.percent_reporting or 0, cur.percent_reporting or 0
    tv0, tv1 = prev.total_votes, cur.total_votes
    if tv1 > tv0 or pr1 > pr0:
        base = baseline or prev
        kind = FIRST_RESULTS if tv0 == 0 and tv1 > 0 else NEW_BATCH
        events.append(Event(kind, rid, f"{tv1}|{pr1}", {
            "total_votes": tv1,
            "percent_reporting": cur.percent_reporting,
            "votes_added": tv1 - base.total_votes,
            "reporting_added": round(pr1 - (base.percent_reporting or 0), 2),
            "leader": _cand_summary(cur.leader()),
            "margin": _margin(cur),
        }))

    # Lead change (only meaningful once both snapshots have votes)
    old_lead, new_lead = prev.leader(), cur.leader()
    if old_lead is not None and new_lead is not None and cand_key(old_lead) != cand_key(new_lead):
        events.append(Event(LEAD_CHANGE, rid, f"{cand_key(new_lead)}|{tv1}", {
            "old_leader": _cand_summary(old_lead),
            "new_leader": _cand_summary(new_lead),
            "margin": _margin(cur),
        }))

    # Milestones crossed since last poll
    crossed = [m for m in milestones if pr0 < m <= pr1]
    if crossed:
        events.append(Event(MILESTONE, rid, str(max(crossed)), {
            "milestones": crossed,
            "percent_reporting": cur.percent_reporting,
            "leader": _cand_summary(cur.leader()),
            "margin": _margin(cur),
        }))
    return events
