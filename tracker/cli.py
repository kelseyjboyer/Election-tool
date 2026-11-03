"""Command line entry point: python -m tracker <command>"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .client import CivicClient, FetchError
from .config import ConfigError, load_config
from .format import PROJECTION, fmt_time
from .model import parse_race

log = logging.getLogger("tracker")


def _setup_logging(cfg, verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if cfg is not None and cfg.settings.log_file:
        fh = logging.FileHandler(cfg.path(cfg.settings.log_file), encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _client(cfg) -> CivicClient:
    s = cfg.settings
    return CivicClient(s.api_base, s.user_agent, spacing=s.request_spacing_seconds)


# ---------------------------------------------------------------- commands
def cmd_run(cfg, args):
    from .notify import ConsoleNotifier, NtfyNotifier
    from .page import start_server
    from .runner import run_loop
    from .state import State

    if args.dry_run:
        notifier = ConsoleNotifier()
        db = args.state or str(cfg.path(cfg.settings.state_db)) + ".dryrun"
        log.info("DRY RUN: alerts print to the console; state kept separately in %s", db)
    else:
        notifier = NtfyNotifier(cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token,
                                user_agent=cfg.settings.user_agent)
        db = args.state or str(cfg.path(cfg.settings.state_db))
    state = State(db)
    if cfg.page.enabled and not args.no_server:
        start_server(cfg)
    log.info("Watching %d races; poll interval %ss (low priority %ss)", len(cfg.races),
             cfg.settings.poll_interval_seconds, cfg.settings.low_priority_poll_interval_seconds)
    try:
        run_loop(cfg, _client(cfg), state, notifier, once=args.once)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        state.close()
    return 0


def cmd_status(cfg, args):
    client = _client(cfg)
    tz = cfg.settings.display_timezone
    groups = {}
    for rc in cfg.races:
        groups.setdefault(rc.group, []).append(rc)
    for g, races in groups.items():
        print(f"\n== {g} ==")
        for rc in races:
            try:
                snap = parse_race(client.race(rc.id), rc.id)
            except FetchError as e:
                print(f"  {rc.label or rc.id}: FETCH FAILED ({e})")
                continue
            label = rc.label or snap.name
            pr = "?" if snap.percent_reporting is None else f"{snap.percent_reporting:g}%"
            if snap.is_chamber:
                seats = ", ".join(f"{c.name} {c.seats}" for c in snap.ranked() if c.seats)
                line = f"seats: {seats or 'none yet'}" + (f" ({rc.majority} for majority)" if rc.majority else "")
            else:
                lead = snap.leader()
                ranked = snap.ranked()
                if lead and len(ranked) > 1:
                    m = ranked[0].votes - ranked[1].votes
                    line = f"{lead.name} leads by {m:,} ({lead.percent:.1f}%), {snap.total_votes:,} votes"
                elif snap.total_votes:
                    line = f"tied, {snap.total_votes:,} votes"
                else:
                    line = "no votes yet"
                if snap.winners:
                    line += f" | {PROJECTION}: " + ", ".join(c.name for c in snap.winners)
            flags = []
            if snap.is_disputed:
                flags.append("DISPUTED")
            if snap.is_delayed:
                flags.append("DELAYED")
            if snap.test_data:
                flags.append("TEST DATA")
            star = "*" if rc.critical else " "
            print(f" {star}{label:<38} {pr:>5}  {line}  [updated {fmt_time(snap.last_updated, tz)}]"
                  + (f" {' '.join(flags)}" if flags else ""))
    print("\n* = control-critical. Calls are civicAPI projections, not official results.")
    return 0


def _slug(q: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", q).strip("_")[:60]


def cmd_discover(cfg, args):
    client = _client(cfg)
    outdir = Path(args.fixtures)
    if args.verify:
        (outdir / "raw").mkdir(parents=True, exist_ok=True)
        for rc in cfg.races:
            try:
                raw = client.race(rc.id)
            except FetchError as e:
                print(f"MISSING {rc.id} {rc.label}: {e}")
                continue
            (outdir / "raw" / f"race_{rc.id}.json").write_text(json.dumps(raw, indent=1))
            ok = str(raw.get("election_date", "")).startswith(args.date)
            print(f"{'ok     ' if ok else 'WRONG DATE'} {rc.id:>7} {rc.label!r:<40} -> {raw.get('election_name')}"
                  f" ({raw.get('election_date', '')[:10]})")
        return 0
    if not args.queries:
        print("Give one or more search terms, or --verify to check the configured watchlist.")
        return 2
    (outdir / "search").mkdir(parents=True, exist_ok=True)
    for q in args.queries:
        try:
            races = client.search_all(q)
        except FetchError as e:
            print(f"{q!r}: FAILED ({e}). Try a more specific search term.")
            continue
        (outdir / "search" / f"search_{_slug(q)}.json").write_text(json.dumps(races, indent=1))
        hits = [r for r in races if args.all_dates or str(r.get("election_date", "")).startswith(args.date)]
        print(f"\n{q!r}: {len(races)} results, {len(hits)} on {args.date}")
        for r in hits:
            print(f"  {r['id']:>7}  {r['election_name']}  [{r.get('type')}, {str(r.get('election_date'))[:10]}]")
    return 0


def cmd_test_notify(cfg, args):
    from .notify import NtfyNotifier
    from .page import click_url
    from .policy import Alert
    n = NtfyNotifier(cfg.ntfy.server, cfg.ntfy.topic, cfg.ntfy.token, user_agent=cfg.settings.user_agent)
    rid = cfg.races[0].id if cfg.races else 0
    ok = n.send(Alert("test", rid, "test", 3, "Election tracker test",
                      "If you can read this, ntfy delivery works. Tap to open the results page.",
                      ["white_check_mark"]), click_url(cfg, rid))
    print("sent" if ok else "FAILED (see log)")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracker", description="Personal election results tracker (civicAPI)")
    p.add_argument("-c", "--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="poll watched races and send alerts")
    r.add_argument("--dry-run", action="store_true", help="print alerts instead of sending (separate state)")
    r.add_argument("--once", action="store_true", help="poll every race once, then exit")
    r.add_argument("--state", help="override state database path")
    r.add_argument("--no-server", action="store_true", help="do not start the results page server")

    sub.add_parser("status", help="print current status of every watched race (one shot)")

    d = sub.add_parser("discover", help="search civicAPI and save raw responses to fixtures/")
    d.add_argument("queries", nargs="*")
    d.add_argument("--verify", action="store_true", help="fetch every configured race and check it")
    d.add_argument("--date", default="2026-11-03")
    d.add_argument("--all-dates", action="store_true")
    d.add_argument("--fixtures", default="fixtures")

    sub.add_parser("test-notify", help="send one test notification through ntfy")

    args = p.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ConfigError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2
    _setup_logging(cfg if args.cmd == "run" else None, args.verbose)
    try:
        return {"run": cmd_run, "status": cmd_status, "discover": cmd_discover,
                "test-notify": cmd_test_notify}[args.cmd](cfg, args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
