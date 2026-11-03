"""Build simulated election-night responses from real saved civicAPI fixtures."""
from __future__ import annotations

import copy
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "raw"

GOVERNOR = 87375        # Oregon Governor, 4 candidates
MEASURE = 185604        # Measure 03-639, Yes/No
SENATE_CONTROL = 87528  # United States Senate seat tally
NY17 = 87319


def load_raw(race_id: int) -> dict:
    return json.loads((FIXTURES / f"race_{race_id}.json").read_text())


def with_votes(raw: dict, votes: list[int], percent_reporting: float | None = None,
               last_updated: str | None = None, winners: tuple[int, ...] = ()) -> dict:
    """Copy of `raw` with candidate votes set (by position) and percents recomputed."""
    r = copy.deepcopy(raw)
    r.pop("region_results", None)
    total = sum(votes)
    for i, c in enumerate(r["candidates"]):
        v = votes[i] if i < len(votes) else 0
        c["votes"] = v
        c["percent"] = round(100.0 * v / total, 2) if total else 0
        c["winner"] = i in winners
    if percent_reporting is not None:
        r["percent_reporting"] = percent_reporting
    if last_updated is not None:
        r["last_updated"] = last_updated
    return r


def with_seats(raw: dict, seats: list[int], last_updated: str | None = None) -> dict:
    r = copy.deepcopy(raw)
    r.pop("region_results", None)
    for i, c in enumerate(r["candidates"]):
        c["seats"] = seats[i] if i < len(seats) else 0
    if last_updated:
        r["last_updated"] = last_updated
    return r
