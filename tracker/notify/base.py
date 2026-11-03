from __future__ import annotations

from typing import Protocol

from ..policy import Alert


class Notifier(Protocol):
    def send(self, alert: Alert, click_url: str | None = None) -> bool: ...
