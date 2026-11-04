"""ntfy.sh delivery. Docs: https://docs.ntfy.sh/publish/"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from ..policy import Alert

log = logging.getLogger("tracker.ntfy")


class NtfyNotifier:
    def __init__(self, server: str, topic: str, token: str = "", timeout: float = 15.0,
                 user_agent: str = "election-results-tracker/0.1"):
        if not topic or topic.startswith("CHANGE-ME"):
            raise ValueError("Set [ntfy] topic in config.toml before sending real alerts.")
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.user_agent = user_agent
        self._quota: tuple[float, int | None] | None = None

    def quota_remaining(self, max_age: float = 60.0) -> int | None:
        """Messages this IP may still send today, per the ntfy server. None if unknown."""
        now = time.monotonic()
        if self._quota and now - self._quota[0] < max_age:
            return self._quota[1]
        headers = {"User-Agent": self.user_agent}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        value = None
        try:
            req = urllib.request.Request(f"{self.server}/v1/account", headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                value = int(json.load(resp)["stats"]["messages_remaining"])
        except Exception as e:  # unknown quota is fine; the local count is the fallback
            log.debug("ntfy quota check failed: %s", e)
        self._quota = (now, value)
        return value

    def payload(self, alert: Alert, click_url: str | None) -> dict:
        p = {
            "topic": self.topic,
            "title": alert.title[:250],
            "message": alert.body[:3900],
            "priority": int(alert.priority),
            "tags": alert.tags,
        }
        if click_url:
            p["click"] = click_url
            p["actions"] = [{"action": "view", "label": "Results", "url": click_url}]
        return p

    def send(self, alert: Alert, click_url: str | None = None) -> bool:
        # JSON publishing to the server root keeps non-ASCII names (e.g. accents) intact.
        data = json.dumps(self.payload(alert, click_url)).encode()
        headers = {"Content-Type": "application/json", "User-Agent": self.user_agent}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.server, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            log.error("ntfy rejected alert %s: HTTP %s %s", alert.alert_id, e.code, e.read()[:200])
            return False
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            log.error("ntfy send failed for %s: %s", alert.alert_id, e)
            return False
        if not ok:
            log.error("ntfy unexpected status for %s", alert.alert_id)
        elif self._quota and self._quota[1] is not None:
            self._quota = (self._quota[0], self._quota[1] - 1)
        return ok
