"""Static results page. Rewritten after every poll; served by a tiny local HTTP server.

ntfy alerts link to <public_url>/#race-<id>, which scrolls to and highlights that race.
"""
from __future__ import annotations

import functools
import html
import http.server
import logging
import os
import threading
import time
from pathlib import Path

from .config import Config, RaceConfig
from .format import OREGON_NOTE, PROJECTION, fmt_time, party
from .model import Snapshot

log = logging.getLogger("tracker.page")

GROUP_TITLES = {
    "control": "Chamber control",
    "my_ballot": "My ballot (Milwaukie, OR)",
    "senate": "Senate battleground",
    "house": "House battleground",
    "other": "Other races",
}

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--accent:#2f6fde;
--dem:#3b6fd6;--rep:#d64545;--other:#8a8a8f;--warn:#b26b00;--warnbg:#fff4e0;--hl:#fff7c2}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151517;--card:#1f1f22;
--text:#eee;--muted:#a0a0a8;--line:#333338;--accent:#6c9cff;--warnbg:#3a2a10;--hl:#3b3510}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0}h2{font-size:16px;margin:24px 0 8px;color:var(--muted);
text-transform:uppercase;letter-spacing:.04em}
.meta{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;
margin:8px 0;scroll-margin-top:12px}
.card:target{outline:3px solid var(--accent);background:var(--hl)}
.top{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:baseline}
.label{font-weight:600}.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.called{background:var(--accent);color:#fff;border-color:var(--accent)}
.crit{color:var(--warn);font-size:12px;font-weight:600}
.row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;margin-top:6px;font-size:14px}
.bar{grid-column:1/3;height:6px;background:var(--line);border-radius:3px;overflow:hidden}
.bar span{display:block;height:100%}
.note{font-size:12px;color:var(--muted);margin-top:6px}
.warn{font-size:12px;background:var(--warnbg);color:var(--warn);padding:4px 8px;border-radius:6px;margin-top:6px}
.alerts{font-size:13px}.alerts li{margin:2px 0}
nav a{margin-right:12px;color:var(--accent);font-size:14px}
"""


def _color(p: str | None) -> str:
    p = (p or "").lower()
    if p.startswith("dem"):
        return "var(--dem)"
    if p.startswith("rep"):
        return "var(--rep)"
    return "var(--other)"


def _race_card(rc: RaceConfig, snap: Snapshot | None, fetched_at: float | None,
               error: tuple[str, float] | None, tz: str) -> str:
    e = html.escape
    label = e(rc.label or (snap.name if snap else f"Race {rc.id}"))
    parts = [f'<section class="card" id="race-{rc.id}"><div class="top"><span class="label">{label}</span>']
    if snap is None:
        parts.append('<span class="pill">no data yet</span></div>')
    else:
        if snap.winners and not snap.is_chamber:
            names = e(", ".join(c.name for c in snap.winners))
            parts.append(f'<span class="pill called">{e(PROJECTION)}: {names}</span>')
        elif not snap.is_chamber:
            parts.append('<span class="pill">not called</span>')
        parts.append("</div>")
        if rc.critical:
            parts.append('<div class="crit">Control-critical</div>')
        pr = snap.percent_reporting
        parts.append(f'<div class="meta">{e(snap.name)} · {"?" if pr is None else f"{pr:g}"}% reporting · '
                     f'{snap.total_votes:,} votes · updated {e(fmt_time(snap.last_updated, tz))}</div>')
        if snap.is_chamber:
            maj = rc.majority
            for c in snap.ranked():
                if (c.seats or 0) == 0 and c.name.lower() not in ("democrats", "republicans", "democratic", "republican"):
                    continue
                width = min(100, 100 * (c.seats or 0) / maj) if maj else 0
                parts.append(f'<div class="row"><span>{e(c.name)}</span><b>{c.seats}'
                             f'{f" / {maj}" if maj else ""}</b><div class="bar">'
                             f'<span style="width:{width:.1f}%;background:{_color(c.party)}"></span></div></div>')
        else:
            total = snap.total_votes
            for c in snap.ranked():
                if total and c.votes == 0 and len(snap.candidates) > 4:
                    continue
                win = " ✔" if c.winner else ""
                parts.append(f'<div class="row"><span>{e(c.name)}{e(party(c.party))}{win}</span>'
                             f'<b>{c.percent:.1f}% · {c.votes:,}</b><div class="bar">'
                             f'<span style="width:{min(100, c.percent):.1f}%;background:{_color(c.party)}"></span>'
                             f'</div></div>')
        if snap.is_disputed:
            parts.append('<div class="warn">civicAPI flags this race as disputed.</div>')
        if snap.is_delayed:
            parts.append('<div class="warn">civicAPI flags this race as delayed.</div>')
        if snap.test_data:
            parts.append('<div class="warn">civicAPI marks this as test data. Alerts disabled.</div>')
        if snap.is_oregon:
            parts.append(f'<div class="note">{e(OREGON_NOTE)}</div>')
    if rc.note:
        parts.append(f'<div class="note">{e(rc.note)}</div>')
    if error:
        parts.append(f'<div class="warn">Last fetch failed {e(time.strftime("%H:%M", time.localtime(error[1])))}:'
                     f' {e(error[0][:160])}</div>')
    if fetched_at:
        parts.append(f'<div class="note">Checked {e(time.strftime("%H:%M:%S", time.localtime(fetched_at)))}</div>')
    parts.append("</section>")
    return "".join(parts)


def render_page(cfg: Config, snapshots: dict, alerts: list[dict], errors: dict) -> str:
    tz = cfg.settings.display_timezone
    e = html.escape
    out = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta http-equiv="refresh" content="60">',
           f"<title>Election Results Tracker</title><style>{CSS}</style></head><body><main>",
           "<h1>Election results</h1>",
           f'<div class="meta">Page built {e(time.strftime("%b %d %H:%M:%S"))} · data: '
           '<a href="https://civicapi.org">civicAPI</a> · calls are civicAPI projections, '
           'not AP or official results · auto-refreshes every 60s</div><nav>']
    groups = [g for g in GROUP_TITLES if any(r.group == g for r in cfg.races)]
    for g in groups:
        out.append(f'<a href="#g-{g}">{e(GROUP_TITLES[g])}</a>')
    out.append('<a href="#alerts">Recent alerts</a></nav>')
    for g in groups:
        out.append(f'<h2 id="g-{g}">{e(GROUP_TITLES[g])}</h2>')
        for rc in [r for r in cfg.races if r.group == g]:
            snap, fetched = snapshots.get(rc.id, (None, None))
            out.append(_race_card(rc, snap, fetched, errors.get(rc.id), tz))
    out.append('<h2 id="alerts">Recent alerts</h2><ul class="alerts">')
    for a in alerts:
        when = time.strftime("%H:%M", time.localtime(a["created_at"]))
        out.append(f'<li><a href="#race-{a["race_id"]}">{e(when)} {e(a["title"])}</a> '
                   f'<span class="meta">({e(a["status"])})</span></li>')
    out.append("</ul></main></body></html>")
    return "".join(out)


def write_page(cfg: Config, state) -> Path:
    outdir = cfg.path(cfg.page.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    html_text = render_page(cfg, state.all_snapshots(), state.recent_alerts(40), state.fetch_errors())
    tmp = outdir / "index.html.tmp"
    tmp.write_text(html_text, encoding="utf-8")
    os.replace(tmp, outdir / "index.html")  # atomic, so the server never serves half a file
    return outdir / "index.html"


def page_url(cfg: Config) -> str:
    base = cfg.page.public_url.rstrip("/")
    return f"{base}/" if cfg.page.enabled and base else "https://civicapi.org/results"


def click_url(cfg: Config, race_id: int) -> str:
    base = cfg.page.public_url.rstrip("/")
    if cfg.page.enabled and base:
        return f"{base}/#race-{race_id}"
    return "https://civicapi.org/results"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("page: " + fmt, *args)


def start_server(cfg: Config) -> http.server.ThreadingHTTPServer:
    outdir = cfg.path(cfg.page.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(_QuietHandler, directory=str(outdir))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", cfg.page.port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="page-server").start()
    log.info("Results page at http://localhost:%d/ (public_url=%r)", cfg.page.port, cfg.page.public_url)
    return srv
