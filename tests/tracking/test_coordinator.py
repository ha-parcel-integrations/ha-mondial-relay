"""Tests for the tracking hub's coordinator."""

from datetime import UTC, datetime
import logging

import aiohttp
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.mondial_relay.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    MID_INTERVAL_MINUTES,
    SOURCE_TRACKING,
    TRACKING_PAGE_NL,
    ParcelStatus,
)
from custom_components.mondial_relay.polling import backoff_seconds
from custom_components.mondial_relay.tracking.api import (
    MondialRelayTrackingError,
    MondialRelayTrackingRefusedError,
)
from custom_components.mondial_relay.tracking.coordinator import (
    MondialRelayTrackingCoordinator,
    _hottest_tier_minutes,
)

from .fake_client import FakeClient
from .payloads import POSTAL_CODE, available, delivered, in_distribution

CODE_A = "12345678"
CODE_B = "1234567890"


def _entry(codes=(CODE_A,), **options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Mondial Relay ({POSTAL_CODE})",
        unique_id=f"tracking:{POSTAL_CODE}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_POSTAL_CODE: POSTAL_CODE,
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            **options,
        },
    )


def _coordinator(hass, client: FakeClient, entry: MockConfigEntry | None = None):
    entry = entry or _entry()
    entry.add_to_hass(hass)
    return MondialRelayTrackingCoordinator(hass, client, entry), entry


def _set_codes(hass, entry: MockConfigEntry, codes) -> None:
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
        },
    )


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


async def test_nothing_tracked_opens_no_session_and_stops_polling(hass):
    client = FakeClient()
    coordinator, _ = _coordinator(hass, client, _entry(codes=()))
    assert await coordinator._async_update_data() == []
    assert client.cycles == 0
    assert coordinator.update_interval is None
    assert coordinator.outgoing == []
    assert coordinator.delivered_outgoing == []


async def test_a_found_parcel_is_active_and_keeps_polling(hass):
    client = FakeClient({CODE_A: available()})
    coordinator, _ = _coordinator(hass, client)
    data = await coordinator._async_update_data()
    assert [p["status"] for p in data] == [ParcelStatus.AT_PICKUP_POINT]
    assert client.lookups == [(CODE_A, POSTAL_CODE)]
    assert coordinator.current_tier_minutes == MID_INTERVAL_MINUTES
    assert coordinator.update_interval is not None
    assert coordinator.last_success_time is not None


async def test_lookups_run_in_order_in_one_cycle_with_the_postcode_verbatim(hass):
    client = FakeClient({CODE_A: available(), CODE_B: in_distribution()})
    entry = _entry(codes=(CODE_A, CODE_B))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=entry.data,
        options={**entry.options, CONF_POSTAL_CODE: "1234 ab"},
    )
    coordinator, _ = _coordinator(hass, client, entry)
    await coordinator._async_update_data()
    assert client.cycles == 1
    assert client.lookups == [(CODE_A, "1234 ab"), (CODE_B, "1234 ab")]


async def test_a_delivered_code_is_skipped_from_the_next_fetch(hass):
    client = FakeClient({CODE_A: delivered()})
    coordinator, _ = _coordinator(hass, client)
    assert await coordinator._async_update_data() == []
    assert [p["barcode"] for p in coordinator.delivered] == [CODE_A]
    assert coordinator.delivered_codes == {CODE_A}
    assert coordinator.update_interval is None

    await coordinator._async_update_data()
    assert client.cycles == 1
    assert [p["barcode"] for p in coordinator.delivered] == [CODE_A]


async def test_untracking_drops_the_cache_and_the_skip(hass):
    client = FakeClient({CODE_A: delivered()})
    coordinator, entry = _coordinator(hass, client)
    await coordinator._async_update_data()
    _set_codes(hass, entry, ())
    await coordinator._async_update_data()
    assert coordinator.delivered == []
    assert coordinator.delivered_codes == set()


async def test_a_never_found_code_shows_as_an_unknown_placeholder(hass):
    coordinator, _ = _coordinator(hass, FakeClient())
    data = await coordinator._async_update_data()
    assert [(p["barcode"], p["status"]) for p in data] == [
        (CODE_A, ParcelStatus.UNKNOWN)
    ]


async def test_a_not_found_blip_keeps_the_last_payload(hass):
    client = FakeClient({CODE_A: available()})
    coordinator, _ = _coordinator(hass, client)
    await coordinator._async_update_data()
    client.answers = {}
    data = await coordinator._async_update_data()
    assert data[0]["status"] is ParcelStatus.AT_PICKUP_POINT


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientError("boom"),
        MondialRelayTrackingError("HTTP 500", status_code=500),
    ],
)
async def test_one_failing_lookup_does_not_fail_the_cycle(hass, error, caplog):
    other = in_distribution()
    other["Numero"] = int(CODE_B)
    client = FakeClient({CODE_A: available(), CODE_B: other})
    coordinator, _ = _coordinator(hass, client, _entry(codes=(CODE_A, CODE_B)))
    await coordinator._async_update_data()
    client.answers = {CODE_A: error, CODE_B: other}
    with caplog.at_level(logging.WARNING):
        data = await coordinator._async_update_data()
    assert {p["barcode"] for p in data} == {CODE_A, CODE_B}
    assert f"lookup failed for {CODE_A}" in caplog.text


async def test_every_lookup_failing_without_a_cache_fails_the_cycle(hass):
    client = FakeClient({CODE_A: aiohttp.ClientError("boom")})
    coordinator, _ = _coordinator(hass, client)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_a_cycle_served_from_cache_is_not_a_success(hass):
    client = FakeClient({CODE_A: available()})
    coordinator, _ = _coordinator(hass, client)
    await coordinator._async_update_data()
    stamp = coordinator.last_success_time
    client.answers = {CODE_A: aiohttp.ClientError("boom")}
    await coordinator._async_update_data()
    assert coordinator.last_success_time == stamp


async def test_the_url_follows_home_assistants_country(hass):
    hass.config.country = "NL"
    coordinator, _ = _coordinator(hass, FakeClient({CODE_A: available()}))
    data = await coordinator._async_update_data()
    assert data[0]["url"].startswith(TRACKING_PAGE_NL)


async def test_history_follows_the_option(hass):
    client = FakeClient({CODE_A: available()})
    coordinator, _ = _coordinator(hass, client, _entry(**{CONF_INCLUDE_HISTORY: True}))
    data = await coordinator._async_update_data()
    assert data[0]["history"]


# ---------------------------------------------------------------------------
# refusals and rate limits
# ---------------------------------------------------------------------------


async def test_a_refused_lookup_fails_the_cycle_and_warns_once(hass, caplog):
    client = FakeClient({CODE_A: MondialRelayTrackingRefusedError("HTTP 403")})
    coordinator, _ = _coordinator(hass, client, _entry(codes=(CODE_A, CODE_B)))
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
    assert caplog.text.count("refused this integration's request") == 1
    # No retries and no further lookups once refused.
    assert client.lookups == [(CODE_A, POSTAL_CODE)] * 3


async def test_the_refusal_warning_returns_after_a_good_cycle(hass, caplog):
    client = FakeClient(cycle_error=MondialRelayTrackingRefusedError("no value"))
    coordinator, _ = _coordinator(hass, client)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        client.cycle_error = None
        client.answers = {CODE_A: available()}
        await coordinator._async_update_data()
        client.cycle_error = MondialRelayTrackingRefusedError("no value")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert caplog.text.count("refused this integration's request") == 2


async def test_a_rate_limited_lookup_backs_off_with_retry_after(hass):
    client = FakeClient(
        {CODE_A: MondialRelayTrackingError("HTTP 429", status_code=429, retry_after=30)}
    )
    coordinator, _ = _coordinator(hass, client, _entry(codes=(CODE_A, CODE_B)))
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 30
    assert client.lookups == [(CODE_A, POSTAL_CODE)]


async def test_the_backoff_grows_without_retry_after(hass):
    client = FakeClient(
        cycle_error=MondialRelayTrackingError("HTTP 429", status_code=429)
    )
    coordinator, _ = _coordinator(hass, client)
    retry_afters = []
    for _ in range(2):
        with pytest.raises(UpdateFailed) as err:
            await coordinator._async_update_data()
        retry_afters.append(err.value.retry_after)
    assert retry_afters == [backoff_seconds(1), backoff_seconds(2)]


async def test_another_page_error_fails_the_cycle(hass):
    client = FakeClient(
        cycle_error=MondialRelayTrackingError("HTTP 500", status_code=500)
    )
    coordinator, _ = _coordinator(hass, client)
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after is None


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_status_changes_fire_the_suite_events(hass):
    client = FakeClient({CODE_A: in_distribution()})
    coordinator, _ = _coordinator(hass, client)
    changed = async_capture_events(hass, f"{DOMAIN}_parcel_status_changed")
    delivered_events = async_capture_events(hass, f"{DOMAIN}_parcel_delivered")

    await coordinator._async_update_data()
    client.answers = {CODE_A: available()}
    await coordinator._async_update_data()
    client.answers = {CODE_A: delivered()}
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["new_status"] for e in changed] == [ParcelStatus.AT_PICKUP_POINT]
    assert len(delivered_events) == 1


# ---------------------------------------------------------------------------
# tier
# ---------------------------------------------------------------------------


def test_tier_is_none_when_nothing_is_active():
    assert _hottest_tier_minutes([], datetime(2030, 1, 1, 12, tzinfo=UTC)) is None


def test_tier_is_mid_while_in_transit():
    parcels = [{"status": ParcelStatus.IN_TRANSIT, "planned_from": None}]
    assert (
        _hottest_tier_minutes(parcels, datetime(2030, 1, 1, 12, tzinfo=UTC))
        == MID_INTERVAL_MINUTES
    )


@pytest.mark.parametrize(
    ("planned_from", "expected"),
    [
        (None, HOT_INTERVAL_MINUTES),
        ("not-a-date", HOT_INTERVAL_MINUTES),
        ("2030-01-01T12:30:00+00:00", HOT_INTERVAL_MINUTES),
        ("2030-01-01T18:00:00+00:00", MID_INTERVAL_MINUTES),
    ],
)
def test_tier_is_hot_only_close_to_an_out_for_delivery_window(planned_from, expected):
    now = datetime(2030, 1, 1, 12, tzinfo=UTC)
    parcels = [{"status": ParcelStatus.OUT_FOR_DELIVERY, "planned_from": planned_from}]
    assert _hottest_tier_minutes(parcels, now) == expected
