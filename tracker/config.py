"""Load and validate config.toml."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MIN_POLL_SECONDS = 60  # hard floor, cannot be lowered from config
MIN_REQUEST_SPACING = 0.5
VALID_GROUPS = ("control", "my_ballot", "senate", "house", "other")


class ConfigError(ValueError):
    pass


@dataclass
class RaceConfig:
    id: int
    label: str | None = None
    group: str = "other"
    critical: bool = False
    priority: str = "normal"  # "normal" or "low"
    majority: int | None = None
    note: str = ""


@dataclass
class Settings:
    poll_interval_seconds: float = 90
    low_priority_poll_interval_seconds: float = 300
    request_spacing_seconds: float = 1.0
    batch_throttle_minutes: float = 10
    critical_batch_throttle_minutes: float = 5
    low_priority_batch_throttle_minutes: float = 60
    milestones: list[int] = field(default_factory=lambda: [25, 50, 75, 90])
    state_db: str = "state.db"
    log_file: str = "tracker.log"
    display_timezone: str = "America/Los_Angeles"
    user_agent: str = "election-results-tracker/0.1"
    api_base: str = "https://civicapi.org/api/v2"


@dataclass
class NtfyConfig:
    server: str = "https://ntfy.sh"
    topic: str = ""
    token: str = ""


@dataclass
class DeliveryConfig:
    digest_minutes: float = 10        # bundle routine updates into one push this often
    daily_limit: int = 240            # ntfy.sh anonymous limit is 250 messages per IP per day
    reserve_for_urgent: int = 60      # budget kept back for calls, retractions, lead changes


@dataclass
class PageConfig:
    enabled: bool = True
    port: int = 8765
    output_dir: str = "site"
    public_url: str = ""


@dataclass
class Config:
    settings: Settings
    ntfy: NtfyConfig
    page: PageConfig
    races: list[RaceConfig]
    base_dir: Path
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)

    def race(self, race_id: int) -> RaceConfig | None:
        for r in self.races:
            if r.id == race_id:
                return r
        return None

    def path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else self.base_dir / q

    def poll_interval(self, race: RaceConfig) -> float:
        s = self.settings
        val = s.low_priority_poll_interval_seconds if race.priority == "low" else s.poll_interval_seconds
        return max(MIN_POLL_SECONDS, float(val))

    def batch_throttle_seconds(self, race: RaceConfig) -> float:
        s = self.settings
        if race.critical:
            m = s.critical_batch_throttle_minutes
        elif race.priority == "low":
            m = s.low_priority_batch_throttle_minutes
        else:
            m = s.batch_throttle_minutes
        return max(0.0, float(m)) * 60


def _build(cls, data: dict, section: str):
    known = cls.__dataclass_fields__.keys()
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    settings = _build(Settings, raw.get("settings", {}), "settings")
    if settings.poll_interval_seconds < MIN_POLL_SECONDS:
        raise ConfigError(f"poll_interval_seconds must be >= {MIN_POLL_SECONDS}")
    if settings.low_priority_poll_interval_seconds < MIN_POLL_SECONDS:
        raise ConfigError(f"low_priority_poll_interval_seconds must be >= {MIN_POLL_SECONDS}")
    settings.request_spacing_seconds = max(MIN_REQUEST_SPACING, float(settings.request_spacing_seconds))
    settings.milestones = sorted(int(m) for m in settings.milestones)

    ntfy = _build(NtfyConfig, raw.get("ntfy", {}), "ntfy")
    page = _build(PageConfig, raw.get("page", {}), "page")
    delivery = _build(DeliveryConfig, raw.get("delivery", {}), "delivery")
    if delivery.digest_minutes < 1:
        raise ConfigError("digest_minutes must be at least 1")

    races: list[RaceConfig] = []
    seen: set[int] = set()
    for i, r in enumerate(raw.get("race", [])):
        rc = _build(RaceConfig, r, f"race #{i + 1}")
        rc.id = int(rc.id)
        if rc.id in seen:
            raise ConfigError(f"Race id {rc.id} is listed twice")
        if rc.group not in VALID_GROUPS:
            raise ConfigError(f"Race {rc.id}: group must be one of {VALID_GROUPS}")
        if rc.priority not in ("normal", "low"):
            raise ConfigError(f"Race {rc.id}: priority must be 'normal' or 'low'")
        seen.add(rc.id)
        races.append(rc)

    return Config(settings=settings, ntfy=ntfy, page=page, races=races,
                  base_dir=path.resolve().parent, delivery=delivery)
