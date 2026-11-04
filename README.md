# Election Results Tracker (civicAPI)

A small personal tool that watches a list of races, detects meaningful changes, and pushes alerts to your phone through [ntfy](https://ntfy.sh). Tapping an alert opens a results page that jumps to that race.

Data comes from [civicAPI](https://civicapi.org). **Race calls are civicAPI projections from its own decision desk.** They are not AP calls and not official results, and every alert says so.

## What it watches (see `config.toml`)

| Group | Races |
|---|---|
| **Chamber control** | US Senate seat tally (51 for majority), US House seat tally (218) |
| **My ballot (Milwaukie, OR)** | US Senate, OR-5, Governor, OR House 41, OR Senate seat tally, Milwaukie Mayor and Council 2 and 4, Measure 03-639 (North Clackamas SD), Clackamas Commissioner 2 and 4, Sheriff, Clerk, Treasurer, Justice of the Peace, Circuit Court 5th Dist Pos 5, Soil and Water seats |
| **Senate battleground** | AK, IA, KS, ME, MI, OH (special), TX, NE, SC, NH, GA, NC, MN |
| **House battleground** | NY-17, MT-1, MI-4, MI-7, MI-10, FL-14, FL-22, IA-1, IA-2, IA-3, AZ-6, CA-22, PA-7, PA-8, PA-10, WA-3, plus OR-5 |

Battleground races are marked `critical = true`. They get a shorter throttle window and higher push priority.

## Running without a computer (GitHub Actions)

`.github/workflows/tracker.yml` runs the tracker on GitHub's servers. Nothing needs to be running on your end.

- **Schedule:** runs continuously in 50-minute shifts through Nov 20 (change `STOP_AFTER` to extend it).
- **State between shifts:** saved in the Actions cache, so no alert is sent twice.
- **Cost:** none. The repo is public, and public repos get unlimited standard Actions minutes.
- **Run a shift now:** GitHub > Actions > Election tracker > Run workflow.
- **Stop:** disable the workflow on the same page.
- **Required:** a repository secret named `NTFY_TOPIC` holding your ntfy topic. The repo is public, so the topic must never go in `config.toml`.

## Setup (about 5 minutes)

Requires **Python 3.11+**. No third-party packages.

1. **Get the code**
   ```bash
   git clone <this repo> && cd Election-tool
   ```
2. **Pick an ntfy topic.** Anyone who knows the name can read it, so make it long and random:
   ```bash
   python3 -c "import secrets; print('election-' + secrets.token_hex(8))"
   ```
   For local runs, export it as `NTFY_TOPIC=...`. For GitHub Actions, store it as the `NTFY_TOPIC` repository secret. Don't commit it, because this repo is public.
3. **Subscribe on your phone.** Install the ntfy app (iOS or Android), tap **+**, and enter the same topic.
4. **Set the tap-through link** (optional but recommended). Find your computer's LAN address (macOS: `ipconfig getifaddr en0`) and set:
   ```toml
   [page]
   public_url = "http://192.168.1.20:8765"
   ```
   If you leave this empty, taps open civicapi.org/results instead.
5. **Test delivery**
   ```bash
   python3 -m tracker test-notify
   ```

## Running

| Command | What it does |
|---|---|
| `python3 -m tracker run` | **Main mode.** Polls, alerts, and serves the results page on port 8765. |
| `python3 -m tracker run --dry-run` | Prints alerts to the console. Uses a separate state file, so real alerts are not affected. |
| `python3 -m tracker run --once` | Polls every race once, then exits. |
| `python3 -m tracker status` | One-shot table of every watched race. |
| `python3 -m tracker discover "Oregon Measure"` | Searches civicAPI and saves raw results to `fixtures/search/`. |
| `python3 -m tracker discover --verify` | Fetches every configured race, saves it to `fixtures/raw/`, and flags wrong dates. |

Stop with **Ctrl+C**. Restarting is safe because state lives in `state.db` and already-sent alerts are never re-sent.

**Keep your computer awake.** On macOS, run `caffeinate -i python3 -m tracker run`.

### Seeing the page away from home

The page is served from your computer, so by default it only works on your home Wi-Fi. To reach it from anywhere, install [Tailscale](https://tailscale.com) on the computer and phone, then set `public_url` to the computer's Tailscale address (for example `http://100.x.y.z:8765`).

## Alerts

| Alert | Sent | Priority |
|---|---|---|
| **Call retracted / call changed** | Immediately | Urgent |
| **Chamber majority reached or lost** | Immediately | Urgent |
| **civicAPI projection (race called)** | Immediately | High |
| **Lead change** (including Yes/No flips) | Immediately | High |
| **Data anomaly** (votes went down, percents off, candidate missing) | Immediately, **instead of** the normal update | High |
| **Status flag** (`is_disputed`, `is_delayed`) | Immediately | High |
| **New results / first results / 25-50-75-90% milestones / seat counts** | Bundled into one **digest** push every 10 min | Normal |

Every alert includes the race's civicAPI `last_updated` time. Every Oregon alert includes a mail-ballot caution, because Oregon counts for days and early leads often shift.

**Throttles.** New-results alerts are limited per race: 5 min for critical races, 10 min for normal races, 60 min for low priority. A skipped update is not lost. The next alert reports the full change since the last one you received.

**ntfy.sh quota.** Without an account, ntfy.sh allows **250 messages per day per IP**. The tracker keeps a rolling 24-hour count, stops at 240, and holds back the last 60 sends for calls, retractions and lead changes. Routine updates that don't fit stay on the results page.

## Adding or removing races

1. Find the race ID:
   ```bash
   python3 -m tracker discover "Washington US House 8"
   ```
   Use specific terms. Broad searches like `Senate` can time out (HTTP 503) under election-night load.
2. Add a block to `config.toml`:
   ```toml
   [[race]]
   id = 87517
   label = "WA-8"
   group = "house"        # control | my_ballot | senate | house | other
   critical = true
   ```
3. Run `python3 -m tracker discover --verify` to confirm it.

Use `priority = "low"` for down-ballot races you want to check less often (they poll every 5 min).

**Soil and Water zones.** Only one zone is on your ballot. Delete the zone entries that aren't yours.

## Settings worth knowing

| Setting | Default | Notes |
|---|---|---|
| `poll_interval_seconds` | 90 | Per race. Anything under 60 is rejected. |
| `request_spacing_seconds` | 1.0 | Gap between any two API calls. |
| `batch_throttle_minutes` | 10 | Critical races: 5. Low priority: 60. |
| `digest_minutes` | 10 | How often routine updates are bundled and pushed. |
| `daily_limit` / `reserve_for_urgent` | 240 / 60 | Matches the ntfy.sh anonymous quota. |

At 51 races this is about 0.55 requests per second. If civicAPI sends HTTP 429, every request pauses together and backs off exponentially, honoring `Retry-After`.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests build simulated election-night data from **real saved civicAPI responses** in `fixtures/raw/`. They cover new batches, lead changes, calls, retractions, call changes, anomalies, milestones, chamber majorities, throttling, digests, the send budget, and restart safety.

## Files

```
config.toml          watchlist and settings
tracker/client.py    HTTP, spacing, backoff
tracker/model.py     v2 JSON -> Snapshot
tracker/detect.py    change detection (pure functions)
tracker/policy.py    priority and throttling
tracker/format.py    alert wording
tracker/state.py     SQLite state and alert log
tracker/notify/      delivery: console, ntfy (swap or add backends here)
tracker/page.py      results page and local web server
tracker/runner.py    polling loop, digest, send budget
fixtures/            raw API responses captured 2026-11-03
API_NOTES.md         what was verified about civicAPI vs. what is still uncertain
```

## Attribution

Results data from [civicAPI](https://civicapi.org). This is a personal tool. Link civicapi.org if you share it.

## Calls-only mode (current)

`config.calls.toml` watches every 2026 US House and Senate race that was still uncalled on Nov 4, and alerts **only** when one is decided:

- a **civicAPI projection** (race called), a call changed or withdrawn, or a race heading to a **runoff**
- a party crossing the 218 (House) or 51 (Senate) majority line

Every alert ends with the chamber's current seat count, for example
`House now: Democrats 231, Republicans 143 (218 for majority, 61 uncalled)`.

The GitHub workflow uses this file (`TRACKER_CONFIG`) and runs until `STOP_AFTER` (Dec 15). To refresh the list of uncalled races, run `python3 tools/build_calls_config.py` and commit the result.
