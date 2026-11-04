"""Build config.calls.toml: every still-uncalled 2026 US House and Senate race, plus the
two chamber seat tallies. Alerts are limited to decisions (calls, call changes,
retractions, runoffs, majority crossings) and each one quotes the chamber seat count.

Usage:
    python3 tools/build_calls_config.py                # search civicAPI (a few minutes)
    python3 tools/build_calls_config.py --from-json f  # reuse a saved search result
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tracker.client import CivicClient, FetchError  # noqa: E402

ELECTION_DATE = "2026-11-03"
HOUSE_TALLY, SENATE_TALLY = 87524, 87528
STATES = [s.replace("_", " ") for s in (
    "Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia "
    "Hawaii Idaho Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts "
    "Michigan Minnesota Mississippi Missouri Montana Nebraska Nevada New_Hampshire New_Jersey "
    "New_Mexico New_York North_Carolina North_Dakota Ohio Oklahoma Oregon Pennsylvania "
    "Rhode_Island South_Carolina South_Dakota Tennessee Texas Utah Vermont Virginia Washington "
    "West_Virginia Wisconsin Wyoming").split()]
# Races civicAPI search does not return; fetched by id instead.
EXTRA_IDS = [87281, 87282]  # New Hampshire US House 1 and 2


def search(client: CivicClient) -> dict:
    races: dict[str, dict] = {}
    for st in STATES:
        for kind, typ in (("US House", "House of Representatives"), ("US Senate", "US Senate")):
            try:
                rows = client.search_all(f"{st} {kind}", page_size=100, max_pages=3)
            except FetchError as e:
                print(f"search failed: {st} {kind}: {e}", file=sys.stderr)
                continue
            for r in rows:
                if (str(r.get("election_date", "")).startswith(ELECTION_DATE) and r.get("type") == typ
                        and r["election_name"].startswith(f"{st} {kind}")):
                    races[str(r["id"])] = r
        print(f"{st}: {len(races)} so far", file=sys.stderr)
    for rid in EXTRA_IDS:
        r = client.race(rid)
        r["id"] = rid
        r["type"] = "House of Representatives"
        races[str(rid)] = r
    return races


def short_label(name: str) -> str:
    """'North Carolina US House 13' -> 'North Carolina House 13'."""
    return name.replace(" US House", " House").replace(" US Senate", " Senate")


def build(races: dict) -> str:
    out = [
        "# Calls-only watchlist: every 2026 US House and Senate race that was uncalled when",
        "# this file was built, plus the chamber seat tallies. Rebuild: tools/build_calls_config.py",
        "",
        "[settings]",
        "poll_interval_seconds = 90",
        "request_spacing_seconds = 1.0",
        'state_db = "state.db"',
        'log_file = "tracker.log"',
        'display_timezone = "America/Los_Angeles"',
        'user_agent = "election-results-tracker/0.1"',
        'api_base = "https://civicapi.org/api/v2"',
        "# Decisions only. No routine results, milestones or lead changes.",
        'alert_kinds = ["call", "call_changed", "call_retracted", "runoff", "chamber_majority"]',
        "",
        "[ntfy]",
        'server = "https://ntfy.sh"',
        'topic = ""   # set by the NTFY_TOPIC environment variable / GitHub secret',
        "",
        "[delivery]",
        "digest_minutes = 10",
        "daily_limit = 240",
        "reserve_for_urgent = 60",
        "",
        "[page]",
        "enabled = false",
        "",
        "[[race]]",
        f"id = {HOUSE_TALLY}",
        'label = "House"',
        'group = "control"',
        "critical = true",
        "majority = 218",
        "total_seats = 435",
        "",
        "[[race]]",
        f"id = {SENATE_TALLY}",
        'label = "Senate"',
        'group = "control"',
        "critical = true",
        "majority = 51",
        "total_seats = 100",
        "",
    ]
    uncalled = [r for r in races.values() if not any(c.get("winner") for c in r.get("candidates", []))]
    uncalled.sort(key=lambda r: (r["type"] != "US Senate", r["election_name"]))
    for r in uncalled:
        senate = r["type"] == "US Senate"
        out += [
            "[[race]]",
            f"id = {int(r['id'])}",
            f'label = "{short_label(r["election_name"])}"',
            f'group = "{"senate" if senate else "house"}"',
            "critical = true",
            f"chamber = {SENATE_TALLY if senate else HOUSE_TALLY}",
            "",
        ]
    house = sum(1 for r in uncalled if r["type"] != "US Senate")
    print(f"{len(uncalled)} uncalled races ({house} House, {len(uncalled) - house} Senate)", file=sys.stderr)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="saved search output ({'races': {...}})")
    ap.add_argument("--out", default=str(ROOT / "config.calls.toml"))
    args = ap.parse_args()
    if args.from_json:
        races = json.loads(Path(args.from_json).read_text())["races"]
    else:
        races = search(CivicClient("https://civicapi.org/api/v2", "election-results-tracker/0.1",
                                   spacing=1.2, max_attempts=6))
    Path(args.out).write_text(build(races))
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
