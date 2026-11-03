from __future__ import annotations

import sys

from ..policy import Alert

PRIORITY_LABEL = {1: "min", 2: "low", 3: "default", 4: "HIGH", 5: "URGENT"}


class ConsoleNotifier:
    """Prints alerts. Used for --dry-run."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, alert: Alert, click_url: str | None = None) -> bool:
        bar = "=" * 72
        star = " [CRITICAL RACE]" if alert.critical else ""
        print(bar, file=self.stream)
        print(f"[{PRIORITY_LABEL.get(alert.priority, alert.priority)}]{star} {alert.title}", file=self.stream)
        print("-" * 72, file=self.stream)
        print(alert.body, file=self.stream)
        if click_url:
            print(f"(tap opens: {click_url})", file=self.stream)
        self.stream.flush()
        return True
