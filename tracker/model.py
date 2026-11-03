"""Normalize raw civicAPI v2 race JSON into a small, stable snapshot.

Field names here were verified against live v2 responses on 2026-11-03
(see fixtures/raw and API_NOTES.md). Anything the API may omit is optional.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Candidate:
    id: int | None
    name: str
    party: str | None
    votes: int
    percent: float
    winner: bool
    advanced: bool = False
    incumbent: bool = False
    seats: int | None = None  # only present on chamber seat tallies


@dataclass
class Snapshot:
    race_id: int
    name: str
    province: str | None
    office: str | None          # v2 "election_type", e.g. "Governor"
    scope: str | None           # v2 "election_scope", e.g. "Statewide"
    percent_reporting: float | None
    last_updated: str | None
    is_disputed: bool | None
    is_delayed: bool | None     # not present in v2 as of 2026-11-03; kept in case it returns
    test_data: bool
    candidates: list[Candidate] = field(default_factory=list)

    # ---------------------------------------------------------------- derived
    @property
    def total_votes(self) -> int:
        return sum(c.votes for c in self.candidates)

    @property
    def is_chamber(self) -> bool:
        """Seat-tally pseudo races (e.g. 'United States Senate') carry a `seats` field."""
        return any(c.seats is not None for c in self.candidates)

    @property
    def is_measure(self) -> bool:
        names = {c.name.strip().lower() for c in self.candidates}
        return bool(names) and names <= {"yes", "no"}

    @property
    def is_oregon(self) -> bool:
        return (self.province or "").upper() == "OR"

    @property
    def winners(self) -> list[Candidate]:
        return [c for c in self.candidates if c.winner]

    def leader(self) -> Candidate | None:
        """Top vote getter. None when there are no votes or the top spot is tied."""
        if self.total_votes <= 0 or not self.candidates:
            return None
        ranked = sorted(self.candidates, key=lambda c: c.votes, reverse=True)
        if len(ranked) > 1 and ranked[0].votes == ranked[1].votes:
            return None
        return ranked[0]

    def ranked(self) -> list[Candidate]:
        if self.is_chamber:
            return sorted(self.candidates, key=lambda c: c.seats or 0, reverse=True)
        return sorted(self.candidates, key=lambda c: c.votes, reverse=True)

    def candidate_by_key(self) -> dict:
        return {cand_key(c): c for c in self.candidates}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Snapshot":
        d = dict(d)
        d["candidates"] = [Candidate(**c) for c in d.get("candidates", [])]
        return cls(**d)


def cand_key(c: Candidate):
    return c.id if c.id is not None else c.name


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _float(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _opt_bool(v):
    return None if v is None else bool(v)


def parse_race(raw: dict, race_id: int) -> Snapshot:
    cands = []
    for c in raw.get("candidates") or []:
        cands.append(Candidate(
            id=c.get("candidate_id"),
            name=str(c.get("name") or "Unknown"),
            party=c.get("party"),
            votes=_int(c.get("votes")),
            percent=_float(c.get("percent")),
            winner=bool(c.get("winner")),
            advanced=bool(c.get("advanced")),
            incumbent=bool(c.get("incumbent")),
            seats=_int(c["seats"]) if "seats" in c and c["seats"] is not None else None,
        ))
    pr = raw.get("percent_reporting")
    return Snapshot(
        race_id=int(race_id),
        name=str(raw.get("election_name") or f"Race {race_id}"),
        province=raw.get("province"),
        office=raw.get("election_type"),
        scope=raw.get("election_scope"),
        percent_reporting=None if pr is None else _float(pr),
        last_updated=raw.get("last_updated"),
        is_disputed=_opt_bool(raw.get("is_disputed")),
        is_delayed=_opt_bool(raw.get("is_delayed")),
        test_data=bool(raw.get("test_data")),
        candidates=cands,
    )
