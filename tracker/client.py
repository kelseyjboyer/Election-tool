"""civicAPI HTTP client: polite requests, spacing, and exponential backoff."""
from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime

log = logging.getLogger("tracker.client")

RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


class CivicClient:
    def __init__(self, api_base: str, user_agent: str, spacing: float = 1.0,
                 timeout: float = 30.0, max_attempts: int = 5, max_backoff: float = 300.0,
                 sleep=time.sleep, clock=time.monotonic):
        self.api_base = api_base.rstrip("/")
        self.user_agent = user_agent
        self.spacing = spacing
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.max_backoff = max_backoff
        self._sleep = sleep
        self._clock = clock
        self._last_request = 0.0
        # Shared cool-down: after a 429 every request waits, not just the one that failed.
        self._cooldown_until = 0.0

    # ------------------------------------------------------------------ public
    def status(self) -> dict:
        return self._get_json("/status")

    def race(self, race_id: int) -> dict:
        return self._get_json(f"/race/{int(race_id)}")

    def search(self, query: str, limit: int = 100, offset: int = 0) -> dict:
        qs = urllib.parse.urlencode({"query": query, "limit": limit, "offset": offset})
        return self._get_json(f"/race/search?{qs}")

    def search_all(self, query: str, page_size: int = 100, max_pages: int = 10) -> list[dict]:
        """Search returns at most `limit` rows and its `count` is the page size, not a total.
        Page until a short page comes back."""
        out: list[dict] = []
        for page in range(max_pages):
            data = self.search(query, limit=page_size, offset=page * page_size)
            races = data.get("races") or []
            out.extend(races)
            if len(races) < page_size:
                break
        return out

    # ------------------------------------------------------------------ internals
    def _wait_turn(self):
        now = self._clock()
        wait = max(self._last_request + self.spacing - now, self._cooldown_until - now, 0.0)
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._clock()

    def _get_json(self, path: str) -> dict:
        url = self.api_base + path
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._wait_turn()
            req = urllib.request.Request(url, headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                return json.loads(body)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in RETRY_STATUS:
                    raise FetchError(f"HTTP {e.code} for {url}") from e
                delay = self._backoff(attempt, e.headers.get("Retry-After"))
                if e.code == 429:
                    self._cooldown_until = self._clock() + delay
                log.warning("HTTP %s for %s (attempt %d/%d), retrying in %.1fs",
                            e.code, url, attempt, self.max_attempts, delay)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last_err = e
                delay = self._backoff(attempt, None)
                log.warning("Request error for %s: %s (attempt %d/%d), retrying in %.1fs",
                            url, e, attempt, self.max_attempts, delay)
            if attempt < self.max_attempts:
                self._sleep(delay)
        raise FetchError(f"Giving up on {url} after {self.max_attempts} attempts: {last_err}")

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        base = min(self.max_backoff, 2.0 ** attempt)  # 2, 4, 8, 16, 32...
        base += random.uniform(0, base * 0.25)
        ra = _parse_retry_after(retry_after)
        return min(self.max_backoff, max(base, ra or 0.0))


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None
