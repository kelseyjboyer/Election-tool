# civicAPI v2: verified vs. uncertain

All checks were run against the live API on **2026-11-03, 18:38 to 19:20 UTC**, before polls closed. Raw responses are in `fixtures/`.

## Verified

**Endpoints**
- **`GET /api/v2/status`** returns only `{"status":"ok"}`. No rate-limit or election info.
- **`GET /api/v2/race/search?query=`** works and accepts **`limit`** (100 tested) and **`offset`**. The default limit is 20.
  - **Gotcha:** the response field `count` is the number of rows returned on this page, **not** the total number of matches. The tracker keeps paging until a page comes back short.
  - Results include **past elections** (the May 2026 primaries matched too). Filter on `election_date`.
- **`GET /api/v2/race/<id>`** works for every one of the 51 configured races.

**Load behavior**
- Broad searches (`Senate`, `Milwaukie`, `US House`) sometimes return **HTTP 503 `DATABASE_TIMEOUT`** with `Retry-After: 1`. Retrying usually succeeds. The client retries 429 and 5xx with exponential backoff.
- No rate-limit headers were seen, and no 429 came back at about 1 request per second.

**CORS:** responses include `access-control-allow-origin: *`.

**Race fields (v2)**
- `election_name`, `election_type`, `election_scope`, `election_date`, `country`, `province`, `district`, `municipality`
- `polls_open`, `polls_close`, `percent_reporting`, `last_updated`, `is_disputed`, `test_data`, `round`
- `has_map`, `maps`, `registered_voters`, `region_results` (county-level results)
- Chamber tallies also have `seats`.

**Differences from the community v1 doc**

| v1 doc said | v2 reality |
|---|---|
| `election_type` = scope ("Statewide") | `election_type` is now the **office** ("Governor"). Scope moved to **`election_scope`**. |
| `is_delayed` | **Not present.** The tracker handles it if it ever comes back. |
| `sources` | **Not present.** |

**Candidate fields**
- `name`, `candidate_id`, `party`, `incumbent`, `votes`, `percent`, `winner`, plus new `advanced`, `major_candidate`, `color`
- `winner` is the race call. `advanced` means a runoff or top-two advance.
- **Ballot measures** use two candidates named `Yes` and `No`, party `Nonpartisan`.
- **Chamber control** comes from pseudo-races:
  - "United States Senate" (87528) and "United States House of Representatives" (87524), whose candidates are parties with a **`seats`** count.
  - The Senate tally starts with seats not up this year (Democrats 34, Republicans 31). The House starts at 0.
  - State legislatures have the same kind of race (Oregon State Senate is 159554).
- **Search vs. detail:** search results contain `percent_reporting` and candidates, but not `last_updated` or `is_disputed`. That is why the tracker polls race detail.

## Uncertain (watch on election night)

- **Seat tallies and calls.** Do the tally `seats` increase as civicAPI calls races, and is the tally's own `winner` ever set? It is untested until calls happen. The tracker alerts on seat changes and majority crossings either way.
- **`last_updated`.** All races showed timestamps from Nov 2 or earlier. The two chamber tallies showed Oct 23. It is unconfirmed whether `last_updated` refreshes on every vote update or only on some edits.
- **`percent_reporting`.** It is unclear how this is computed for Oregon (mail ballots, no precincts). Treat it as approximate.
- **Percents.** The tracker assumes `percent` is votes divided by the race's total votes. If civicAPI computes it differently, the "percents don't add up" check could fire false anomaly alerts. If that happens, loosen `PERCENT_SUM_TOLERANCE` in `tracker/detect.py`.
- **Retractions.** It is unknown whether civicAPI ever un-sets `winner`. The tracker treats any change as urgent.
- **Rate limits.** The real limits are unpublished. The tracker stays at about 0.55 requests per second and backs off globally on 429.
- **Watchlist placement.**
  - **OR-5 for Milwaukie** is based on public reporting, not a precinct lookup. Confirm against your printed ballot.
  - Your **Soil and Water zone** is unknown.
  - civicAPI search turned up **no statewide Oregon measures** for November and **no Milwaukie city measure**. That matches Wikipedia's list of 2026 Oregon elections, but compare it with your voters' pamphlet.
- **Battleground lists.** These are drawn from late-October and November 2 forecaster ratings (Cook, Sabato, RCP, Fox) via secondary sources, because Cook's pages blocked automated reading. The House list in particular is a reasonable set, not a complete one.
