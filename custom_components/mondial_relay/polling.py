"""Scheduling helpers for dynamic polling, shared by both sources.

Each coordinator decides its own tier — the account never fully stops, a
tracking hub does once nothing is left in flight — and these turn that tier
into the next ``update_interval``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from .const import QUIET_WINDOW_END_HOUR, QUIET_WINDOW_START_HOUR, STAGGER_MINUTES

# Base for the 429 backoff when the carrier's response carries no
# ``Retry-After`` of its own: ``BACKOFF_BASE_SECONDS * 2**consecutive_429``,
# capped at ``BACKOFF_CAP_SECONDS``.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600


def stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    return (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )


def backoff_seconds(consecutive_429: int) -> int:
    """Exponential 429 backoff for a response without ``Retry-After``."""
    return min(BACKOFF_BASE_SECONDS * 2**consecutive_429, BACKOFF_CAP_SECONDS)


def next_update_interval(
    now: datetime, tier_minutes: int | None, entry_id: str
) -> timedelta | None:
    """Turn a tier into the coordinator's next ``update_interval``.

    ``None`` fully suspends scheduling (``DataUpdateCoordinator`` honours
    this natively). Otherwise, clamp the naive next-due time forward to the
    next anchor whenever it would land inside the quiet window — including
    when ``now`` itself is already inside it (an anchor poll computing its
    own follow-up).
    """
    if tier_minutes is None:
        return None

    if in_quiet_window(now):
        return next_anchor(now) - now

    stagger = timedelta(minutes=stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if in_quiet_window(candidate):
        return next_anchor(now) - now
    return candidate - now
