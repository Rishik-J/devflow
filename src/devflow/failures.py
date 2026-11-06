"""Classify why an agent or host call failed, so the engine can react to the cause.

A limit means "wait or switch agent", a sandbox denial means "this agent cannot do this task
here, so retrying only burns money", and infra means "something outside the ticket is down".
"""
from __future__ import annotations

import re

from .router import is_limit_error

SANDBOX_PATTERN = re.compile(
    r"sandbox[- ]?(?:denied|blocked|restrict)|denied by (?:the )?sandbox|operation not permitted|"
    r"\bEPERM\b|permission denied.{0,40}(?:socket|bind|port)|(?:socket|bind|listen).{0,60}"
    r"(?:denied|not permitted|blocked)|network (?:access )?(?:is )?(?:disabled|blocked|denied)",
    re.IGNORECASE | re.DOTALL,
)
INFRA_PATTERN = re.compile(
    r"openclaw\] the cli command failed|openclaw doctor|connection (?:refused|reset)|"
    r"temporarily unavailable|could not resolve host|graphql: api rate limit|"
    r"\b50[234]\b|bad gateway|service unavailable",
    re.IGNORECASE,
)

LIMIT, SANDBOX, INFRA, OTHER = "limit", "sandbox", "infra", "other"


def classify(text: str | None) -> str:
    """Return limit | sandbox | infra | other. Limits win over infra; sandbox wins over both."""
    value = text or ""
    if SANDBOX_PATTERN.search(value):
        return SANDBOX
    if is_limit_error(value):
        return LIMIT
    if INFRA_PATTERN.search(value):
        return INFRA
    return OTHER
