"""Turn events into human-readable alert text. No delivery logic here."""
from __future__ import annotations

from datetime import datetime, timezone

from . import detect as d
from .model import Snapshot

PROJECTION = "civicAPI projection"
PROJECTION_NOTE = ("This is a civicAPI projection from its own decision desk. "
                   "It is not an AP call and not an official or certified result.")
OREGON_NOTE = ("Oregon votes by mail and counts for days. Early leads often shift and "
               "percent reporting is an estimate.")

PARTY_ABBR = {"democratic": "D", "republican": "R", "independent": "I", "libertarian": "L",
              "nonpartisan": None}


def fmt_time(iso: str | None, tz_name: str = "America/Los_Angeles") -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    try:
        from zoneinfo import ZoneInfo
        local = dt.astimezone(ZoneInfo(tz_name))
        label = local.strftime("%Z")
    except Exception:  # zoneinfo or tz data missing (some Windows installs)
        local, label = dt.astimezone(timezone.utc), "UTC"
    hour = local.hour % 12 or 12
    return f"{local.strftime('%b')} {local.day}, {hour}:{local.minute:02d} {local.strftime('%p')} {label}"


def party(p: str | None) -> str:
    if not p:
        return ""
    ab = PARTY_ABBR.get(p.lower(), p)
    return f" ({ab})" if ab else ""


def pct(v) -> str:
    return "?" if v is None else f"{float(v):g}%"


def candidate_lines(s: Snapshot, limit: int = 4) -> list[str]:
    if s.is_chamber:
        rows = [c for c in s.ranked() if (c.seats or 0) > 0][:limit]
        return [f"{c.name}: {c.seats} seats" for c in rows] or ["No seats assigned yet"]
    rows = s.ranked()
    shown = [c for c in rows if c.votes > 0][:limit] or rows[:limit]
    out = []
    for c in shown:
        mark = " [projected winner]" if c.winner else ""
        out.append(f"{c.name}{party(c.party)} {c.percent:.1f}% ({c.votes:,}){mark}")
    hidden = len(rows) - len(shown)
    if hidden > 0:
        out.append(f"+{hidden} more")
    return out


def _margin_line(m: dict | None) -> str | None:
    if not m:
        return None
    return f"Margin: {m['votes']:,} votes ({m['points']:.1f} pts)"


def _footer(s: Snapshot, tz: str) -> list[str]:
    lines = [f"Reporting: {pct(s.percent_reporting)}",
             f"civicAPI last_updated: {fmt_time(s.last_updated, tz)}"]
    if s.is_oregon:
        lines.append(OREGON_NOTE)
    return lines


def render(event: d.Event, snap: Snapshot, label: str, tz: str = "America/Los_Angeles") -> tuple[str, str]:
    """Return (title, body)."""
    k, x = event.kind, event.details
    body: list[str] = []

    if k in d.BATCH_KINDS:
        what = "first results" if k == d.FIRST_RESULTS else "new results"
        title = f"{label}: {what}, {pct(snap.percent_reporting)} reporting"
        if x.get("milestones"):
            title += f" (passed {max(x['milestones'])}%)"
        body += candidate_lines(snap)
        if (m := _margin_line(x.get("margin"))):
            body.append(m)
        if k == d.NEW_BATCH:
            body.append(f"Since last alert: +{x['votes_added']:,} votes, "
                        f"+{x['reporting_added']:g} pts reporting")
    elif k == d.LEAD_CHANGE:
        new, old = x["new_leader"], x["old_leader"]
        title = f"LEAD CHANGE {label}: {new['name']} now ahead"
        body.append(f"{new['name']}{party(new['party'])} passed {old['name']}{party(old['party'])}")
        if (m := _margin_line(x.get("margin"))):
            body.append(m)
        body += candidate_lines(snap)
    elif k == d.CALL:
        names = " and ".join(x["winners"])
        title = f"{PROJECTION}: {names} wins {label}"
        body += candidate_lines(snap)
        body.append(PROJECTION_NOTE)
        if x.get("during_anomaly"):
            body.append("Warning: this update also failed data checks. Treat with extra caution.")
    elif k == d.CALL_RETRACTED:
        title = f"CALL RETRACTED {label}"
        body.append(f"civicAPI withdrew its projection for {', '.join(x['previous_winners'])}. "
                    "Race is uncalled again.")
        body += candidate_lines(snap)
        body.append(PROJECTION_NOTE)
    elif k == d.CALL_CHANGED:
        title = f"CALL CHANGED {label}: now {' and '.join(x['winners'])}"
        body.append(f"Projection moved from {', '.join(x['previous_winners'])} "
                    f"to {', '.join(x['winners'])}.")
        body += candidate_lines(snap)
        body.append(PROJECTION_NOTE)
    elif k == d.STATUS_FLAG:
        flag = x["flag"].replace("is_", "")
        state = "now flagged" if x["new"] else "no longer flagged"
        title = f"{label}: {state} {flag}"
        body += candidate_lines(snap)
    elif k == d.MILESTONE:
        title = f"{label}: passed {max(x['milestones'])}% reporting"
        body += candidate_lines(snap)
        if (m := _margin_line(x.get("margin"))):
            body.append(m)
    elif k == d.ANOMALY:
        title = f"DATA ANOMALY {label}"
        body.append("Normal update withheld. The latest data failed checks:")
        body += [f"- {i}" for i in x["issues"]]
        body.append("Check the source before trusting these numbers.")
    elif k == d.CHAMBER_SEATS:
        maj = x.get("majority")
        title = f"{label}: seat count changed" + (f" ({maj} for majority)" if maj else "")
        for name, (a, b) in sorted(x["changes"].items()):
            body.append(f"{name}: {a} to {b}")
        body.append("Totals: " + ", ".join(candidate_lines(snap)))
        body.append(f"Seat counts reflect {PROJECTION}s, not official results.")
    elif k == d.CHAMBER_MAJORITY:
        verb = "reaches" if x["reached"] else "falls below"
        title = f"{label}: {x['party']} {verb} {x['majority']} seats"
        body.append(f"{x['party']} now at {x['seats']} seats ({PROJECTION}).")
        body.append(PROJECTION_NOTE)
    else:
        title = f"{label}: {k}"
        body += candidate_lines(snap)

    body += _footer(snap, tz)
    return title, "\n".join(body)
