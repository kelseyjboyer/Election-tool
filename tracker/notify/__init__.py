"""Delivery backends. Each takes an Alert and returns True on success.

Alert detection never imports from here, so delivery can be swapped freely.
"""
from .base import Notifier
from .console import ConsoleNotifier
from .ntfy import NtfyNotifier

__all__ = ["Notifier", "ConsoleNotifier", "NtfyNotifier"]
