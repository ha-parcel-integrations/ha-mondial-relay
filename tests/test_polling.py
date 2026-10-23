"""Tests for the shared polling-schedule helpers."""

from datetime import UTC, datetime, timedelta

from custom_components.mondial_relay.const import (
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    STAGGER_MINUTES,
)
from custom_components.mondial_relay.polling import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    backoff_seconds,
    in_quiet_window,
    next_anchor,
    next_update_interval,
    stagger_minutes,
)

# ---------------------------------------------------------------------------
# in_quiet_window / next_anchor
# ---------------------------------------------------------------------------


def test_quiet_window_is_midnight_to_six():
    assert in_quiet_window(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))
    assert in_quiet_window(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    assert not in_quiet_window(datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert not in_quiet_window(datetime(2026, 1, 1, 23, 59, tzinfo=UTC))


def testnext_anchor_before_six_is_six_today():
    now = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
    assert next_anchor(now) == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def testnext_anchor_after_six_is_midnight_tomorrow():
    now = datetime(2026, 1, 1, 14, 0, tzinfo=UTC)
    assert next_anchor(now) == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# stagger_minutes
# ---------------------------------------------------------------------------


def test_stagger_is_stable_and_bounded():
    a = stagger_minutes("entry-1")
    b = stagger_minutes("entry-1")
    c = stagger_minutes("entry-2")
    assert a == b
    assert 0 <= a < STAGGER_MINUTES
    assert 0 <= c < STAGGER_MINUTES


# ---------------------------------------------------------------------------
# next_update_interval
# ---------------------------------------------------------------------------


def test_daytime_candidate_outside_window_is_tier_plus_stagger():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    interval = next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    stagger = stagger_minutes("entry-1")
    assert interval == timedelta(minutes=MID_INTERVAL_MINUTES + stagger)


def test_now_inside_quiet_window_jumps_tonext_anchor():
    now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)  # an anchor poll itself
    interval = next_update_interval(now, HOT_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)


def test_candidate_landingin_quiet_window_clamps_to_the_midnight_anchor():
    now = datetime(2026, 1, 1, 23, 50, tzinfo=UTC)
    interval = next_update_interval(now, MID_INTERVAL_MINUTES, "entry-1")
    assert now + interval == datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_no_tier_suspends_polling():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert next_update_interval(now, None, "entry-1") is None


def test_backoff_doubles_and_is_capped():
    assert backoff_seconds(1) == BACKOFF_BASE_SECONDS * 2
    assert backoff_seconds(2) == BACKOFF_BASE_SECONDS * 4
    assert backoff_seconds(20) == BACKOFF_CAP_SECONDS
