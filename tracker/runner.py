"""Polling loop: fetch -> detect -> policy -> persist -> deliver -> rebuild page."""
from __future__ import annotations

import hashlib
import heapq
import logging
import time

from .client import CivicClient, FetchError
from .config import Config, RaceConfig
from .detect import ANOMALY, CHAMBER_SEATS, FIRST_RESULTS, MILESTONE, NEW_BATCH, detect
from .model import parse_race
from .page import click_url, page_url, write_page
from .policy import Alert, decide
from .state import State

log = logging.getLogger("tracker.runner")


def process_race(cfg: Config, client: CivicClient, state: State, rc: RaceConfig,
                 now: float | None = None):
    """Poll one race and store results. Returns the list of new alerts (not yet delivered)."""
    now = time.time() if now is None else now
    raw = client.race(rc.id)
    snap = parse_race(raw, rc.id)
    prev = state.last_snapshot(rc.id)
    baseline, last_alert_at = state.baseline(rc.id)

    events = detect(prev, snap, milestones=cfg.settings.milestones, majority=rc.majority,
                    baseline=baseline)
    decision = decide(events, snap, rc, now=now, last_batch_alert_at=last_alert_at,
                      throttle_seconds=cfg.batch_throttle_seconds(rc), tz=cfg.settings.display_timezone)

    for ev in events:
        if ev.kind == ANOMALY:
            log.warning("Data anomaly race %s (%s): %s", rc.id, rc.label, "; ".join(ev.details["issues"]))
    if snap.test_data and events:
        log.info("Race %s is test_data; %d event(s) not alerted", rc.id, len(events))
    for ev in decision.suppressed:
        log.info("Throttled %s for race %s", ev.kind, rc.id)

    # First observation seeds the batch baseline so "since last alert" has a reference.
    advance = decision.advance_baseline or (prev is None and baseline is None)
    fresh = state.commit_poll(snap, decision.alerts, advance, now=now)
    for a in fresh:
        log.info("ALERT [%s p%d] %s", a.kind, a.priority, a.title)
    return fresh


DIGEST_KINDS = {FIRST_RESULTS, NEW_BATCH, MILESTONE, CHAMBER_SEATS}
URGENT_PRIORITY = 4
DIGEST_MAX_CHARS = 3800


def _send(notifier, alert: Alert, url: str | None) -> bool:
    try:
        return bool(notifier.send(alert, url))
    except Exception as e:  # never let delivery kill the loop
        log.exception("Notifier crashed on %s: %s", alert.alert_id, e)
        return False


def build_digest(alerts: list[Alert]) -> Alert:
    """One push summarizing routine updates across races. Critical races first."""
    ordered = sorted(alerts, key=lambda a: (not a.critical, -a.priority))
    races = {a.race_id for a in alerts}
    lines: list[str] = []
    for a in ordered:
        body = a.body.splitlines()
        detail = [ln for ln in body[:2] if not ln.startswith("+")]
        updated = next((ln for ln in body if ln.startswith("civicAPI last_updated")), "")
        block = ["* " + a.title] + ["  " + ln for ln in detail]
        if updated:
            block.append("  " + updated.replace("civicAPI last_updated", "updated"))
        if sum(len(x) + 1 for x in lines + block) > DIGEST_MAX_CHARS:
            lines.append(f"...and more on the results page.")
            break
        lines += block
    if any(a.body.find("Oregon votes by mail") >= 0 for a in alerts):
        lines.append("Oregon counts mail ballots for days. Early leads often shift.")
    crit = sum(1 for a in alerts if a.critical)
    title = f"Results update: {len(races)} race{'s' if len(races) != 1 else ''}"
    if crit:
        title += f" ({crit} control-critical)"
    key = hashlib.sha256("|".join(sorted(a.alert_id for a in alerts)).encode()).hexdigest()[:20]
    return Alert(alert_id="digest-" + key, race_id=ordered[0].race_id, kind="digest",
                 priority=max(a.priority for a in alerts), title=title, body="\n".join(lines),
                 tags=["bar_chart"], critical=crit > 0)


def deliver_pending(cfg: Config, state: State, notifier, now: float | None = None,
                    force_digest: bool = False) -> int:
    """Send urgent alerts one by one; bundle routine ones into a periodic digest.

    Respects a rolling 24h send budget so the ntfy.sh quota is never exhausted
    before a call or retraction needs to go out.
    """
    now = time.time() if now is None else now
    dcfg = cfg.delivery
    pending = state.pending_alerts(now)
    if not pending:
        return 0

    def remaining() -> int:
        return dcfg.daily_limit - state.sends_since(now - 86400)

    sent = 0
    routine: list[Alert] = []
    for a in pending:
        if a.kind in DIGEST_KINDS and a.priority < 5:
            routine.append(a)
            continue
        left = remaining()
        if left <= 0 or (left <= dcfg.reserve_for_urgent and a.priority < URGENT_PRIORITY):
            log.warning("Send budget low (%d left); holding %s", left, a.title)
            continue
        ok = _send(notifier, a, click_url(cfg, a.race_id))
        state.mark(a.alert_id, ok)
        if ok:
            state.record_send("single", now)
            sent += 1

    if routine:
        last = state.last_send("digest")
        due = force_digest or last is None or now - last >= dcfg.digest_minutes * 60
        left = remaining()
        if due and left > dcfg.reserve_for_urgent:
            digest = routine[0] if len(routine) == 1 else build_digest(routine)
            url = click_url(cfg, routine[0].race_id) if len({a.race_id for a in routine}) == 1 \
                else page_url(cfg)
            ok = _send(notifier, digest, url)
            for a in routine:
                state.mark(a.alert_id, ok, status="sent" if len(routine) == 1 else "digested")
            if ok:
                state.record_send("digest", now)
                sent += 1
        elif due:
            log.warning("Send budget low (%d left); %d routine update(s) not pushed (still on the page)",
                        left, len(routine))
    return sent


def _refresh_page(cfg: Config, state: State):
    if not cfg.page.enabled:
        return
    try:
        write_page(cfg, state)
    except Exception:
        log.exception("Could not write results page")


def run_loop(cfg: Config, client: CivicClient, state: State, notifier, once: bool = False,
             sleep=time.sleep, clock=time.time):
    try:
        st = client.status()
        log.info("civicAPI status: %s", st)
    except FetchError as e:
        log.warning("civicAPI /status check failed (continuing): %s", e)

    # Stagger first polls so we don't fire every request at once.
    queue: list[tuple[float, int]] = []
    start = clock()
    for i, rc in enumerate(cfg.races):
        heapq.heappush(queue, (start + i * cfg.settings.request_spacing_seconds, rc.id))

    deliver_pending(cfg, state, notifier)  # retry anything left from a previous run
    polled: set[int] = set()
    while queue:
        due, rid = heapq.heappop(queue)
        rc = cfg.race(rid)
        wait = due - clock()
        if wait > 0:
            sleep(wait)
        lag = clock() - due
        if lag > cfg.poll_interval(rc):
            log.warning("Polling is running %.0fs behind schedule; consider fewer races or a "
                        "longer interval", lag)
        try:
            process_race(cfg, client, state, rc)
        except FetchError as e:
            log.error("Fetch failed for race %s (%s): %s", rid, rc.label, e)
            state.record_fetch_error(rid, str(e))
        except Exception as e:
            log.exception("Unexpected error processing race %s: %s", rid, e)
            state.record_fetch_error(rid, f"internal error: {e}")
        deliver_pending(cfg, state, notifier)
        _refresh_page(cfg, state)
        polled.add(rid)
        if once:
            if len(polled) == len(cfg.races):
                break
            continue
        heapq.heappush(queue, (max(clock(), due) + cfg.poll_interval(rc), rid))
