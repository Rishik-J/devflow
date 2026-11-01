"""Tell a person something needs them. GitHub comments are the record; this is the nudge.

`CommandNotifier` runs a configured command with `{text}` substituted, so it can be iMessage
through OpenClaw or `imsg`, or anything else, without devflow knowing about either.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Protocol


class Notifier(Protocol):
    def send(self, text: str) -> bool: ...


class NullNotifier:
    def send(self, text: str) -> bool:
        return False


class RecordingNotifier:
    """For tests: remembers what was sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


class CommandNotifier:
    def __init__(self, template: str, timeout_s: float = 60.0):
        self.template = template
        self.timeout_s = timeout_s

    def build(self, text: str) -> list[str]:
        # shlex-split first, then substitute, so the message can never inject shell syntax
        return [part.replace("{text}", text) for part in shlex.split(self.template)]

    def send(self, text: str) -> bool:
        try:
            proc = subprocess.run(self.build(text), capture_output=True, text=True,
                                  timeout=self.timeout_s, check=False)
        except (subprocess.SubprocessError, OSError):
            return False
        return proc.returncode == 0


def make_notifier(template: str | None) -> Notifier:
    return CommandNotifier(template) if template else NullNotifier()
