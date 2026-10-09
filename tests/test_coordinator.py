"""Tests for the Mondial Relay coordinator: fetching, inbox split and events.

The parcel mapping itself is covered by ``test_parcels.py``.
"""
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.api import (
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
)
from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE_UID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ParcelStatus,
)
from custom_components.mondial_relay.coordinator import MondialRelayCoordinator

from .payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    active_item,
    delivered_item,
    list_item,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Mondial Relay",
        unique_id="subject-1",
        data={
            CONF_REFRESH_TOKEN: "refresh-token-1",
            CONF_ACCOUNT_SUBJECT: "subject-1",
            CONF_DEVICE_UID: "device-1",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
        },
    )


def _client(received=None, shipped=None) -> AsyncMock:
    client = AsyncMock()
    client.async_get_list_received.return_value = received or []
    client.async_get_list_shipped.return_value = shipped or []
    return client


# ---------------------------------------------------------------------------
# fetching / inbox split
# ---------------------------------------------------------------------------


async def test_update_splits_incoming_and_outgoing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()], shipped=[delivered_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert [p["barcode"] for p in data] == [ACTIVE_CODE]
    assert coordinator.outgoing == []
    assert coordinator.delivered == []
    assert [p["barcode"] for p in coordinator.delivered_outgoing] == [DELIVERED_CODE]
    assert coordinator.last_success_time is not None


async def test_update_handles_an_empty_account(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)

    assert await coordinator._async_update_data() == []
    assert coordinator.outgoing == []


async def test_dedupes_within_each_list(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    duplicate = list_item(shipment_uid="uid-x", tracing_code="code-1")
    client = _client(received=[duplicate, duplicate])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1


async def test_overlap_between_lists_is_kept_in_both_and_warned(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    shared = list_item(shipment_uid="uid-shared", tracing_code="code-shared")
    client = _client(received=[shared], shipped=[shared])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    data = await coordinator._async_update_data()

    assert len(data) == 1
    assert len(coordinator.outgoing) == 1
    assert "both incoming and outgoing" in caplog.text


async def test_expired_refresh_token_triggers_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_list_received.side_effect = MondialRelayAuthError("HTTP 401")
    coordinator = MondialRelayCoordinator(hass, client, entry)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


# ---------------------------------------------------------------------------
# signing rejection (403) — abort the poll, never reauth
# ---------------------------------------------------------------------------


async def test_signing_rejection_on_first_refresh_fails_the_update(hass):
    """No last-good data exists yet, so this must still surface as a failure."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client()
    client.async_get_list_received.side_effect = MondialRelaySigningRejectedError(
        "HTTP 403"
    )
    coordinator = MondialRelayCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_signing_rejection_after_success_keeps_last_good_data(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    first = await coordinator._async_update_data()
    assert len(first) == 1

    client.async_get_list_received.side_effect = MondialRelaySigningRejectedError(
        "HTTP 403"
    )
    second = await coordinator._async_update_data()

    assert second == first
    assert "not a problem with your account" in caplog.text


async def test_signing_rejection_never_calls_the_second_list_endpoint(hass):
    """A rejected signature must abort before hammering the shipped list too."""
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    client.async_get_list_received.side_effect = MondialRelaySigningRejectedError(
        "HTTP 403"
    )
    coordinator = MondialRelayCoordinator(hass, client, entry)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    client.async_get_list_shipped.assert_not_awaited()


async def test_signing_rejection_warns_only_once(hass, caplog):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)
    await coordinator._async_update_data()

    client.async_get_list_received.side_effect = MondialRelaySigningRejectedError(
        "HTTP 403"
    )
    await coordinator._async_update_data()
    caplog.clear()
    await coordinator._async_update_data()

    assert "not a problem with your account" not in caplog.text


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


async def test_first_refresh_fires_nothing(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    fired = []
    for suffix in (
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    ):
        hass.bus.async_listen(f"{DOMAIN}_{suffix}", lambda e: fired.append(e))

    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert fired == []


async def test_fires_registered_event_for_new_parcel(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_list_received.return_value = [
        active_item(),
        active_item(shipment_uid="shp-new-0001", shipment_id=11112222),
    ]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["barcode"] == "11112222"


async def test_step_section_to_delivered_fires_delivered_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    delivered, changed = [], []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e))

    await coordinator._async_update_data()
    client.async_get_list_received.return_value = [list_item(step_section=3)]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert [e.data["barcode"] for e in delivered] == [ACTIVE_CODE]
    assert changed == []


async def test_unchanged_status_fires_no_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    client = _client(received=[active_item()])
    coordinator = MondialRelayCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == []


async def test_event_carries_device_id(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    client = _client()
    coordinator = MondialRelayCoordinator(hass, client, entry)

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    await coordinator._async_update_data()  # first refresh: suppressed
    client.async_get_list_received.return_value = [active_item()]
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events[0].data["device_id"] == device.id


async def test_device_id_is_cached_after_first_lookup(hass):
    from homeassistant.helpers import device_registry as dr

    entry = _entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
    )
    coordinator = MondialRelayCoordinator(hass, _client(), entry)

    assert coordinator._device_id() == device.id
    assert coordinator._device_id() == device.id  # cached branch


def test_delivered_codes_always_empty(hass):
    coordinator = MondialRelayCoordinator(hass, _client(), _entry())
    assert coordinator.delivered_codes == set()


# ---------------------------------------------------------------------------
# generic status-changed/delivered/delivery-time-changed event machinery
#
# Shared by every carrier in the suite. ``planned_from`` is always ``None``
# here and the step section never yields ``out_for_delivery``, so hand-built
# dicts exercise the same generic branches.
# ---------------------------------------------------------------------------


def _fake_parcel(barcode: str, status: ParcelStatus, planned_from=None, planned_to=None) -> dict:
    return {
        "barcode": barcode,
        "status": status,
        "planned_from": planned_from,
        "planned_to": planned_to,
    }


async def test_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"A": (None, None)}

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: events.append(e)
    )

    coordinator._fire_change_events(
        [_fake_parcel("A", ParcelStatus.OUT_FOR_DELIVERY)]
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT
    assert events[0].data["new_status"] == ParcelStatus.OUT_FOR_DELIVERY


async def test_delivery_fires_delivered_event_not_status_changed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_state = {"A": ParcelStatus.OUT_FOR_DELIVERY}
    coordinator._known_delivery_times = {"A": (None, None)}

    delivered = []
    changed = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_delivered", lambda e: delivered.append(e))
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_status_changed", lambda e: changed.append(e)
    )

    coordinator._fire_change_events([_fake_parcel("A", ParcelStatus.DELIVERED)])
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1


async def test_fires_delivery_time_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_state = {"A": ParcelStatus.IN_TRANSIT}
    coordinator._known_delivery_times = {"A": (None, None)}

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_parcel_delivery_time_changed", lambda e: events.append(e)
    )

    coordinator._fire_change_events(
        [
            _fake_parcel(
                "A",
                ParcelStatus.IN_TRANSIT,
                planned_from="2026-04-29T16:00:00Z",
                planned_to="2026-04-29T18:00:00Z",
            )
        ]
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["new_planned_from"] == "2026-04-29T16:00:00Z"


async def test_outgoing_fires_status_changed_event(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_outgoing_state = {"A": ParcelStatus.IN_TRANSIT}

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: events.append(e)
    )

    coordinator._fire_outgoing_change_events(
        [_fake_parcel("A", ParcelStatus.OUT_FOR_DELIVERY)]
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["old_status"] == ParcelStatus.IN_TRANSIT


async def test_outgoing_delivery_fires_delivered_not_status_changed(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_outgoing_state = {"A": ParcelStatus.OUT_FOR_DELIVERY}

    delivered = []
    changed = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_delivered", lambda e: delivered.append(e)
    )
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: changed.append(e)
    )

    coordinator._fire_outgoing_change_events([_fake_parcel("A", ParcelStatus.DELIVERED)])
    await hass.async_block_till_done()

    assert changed == []
    assert len(delivered) == 1


async def test_fire_change_events_skips_a_parcel_with_no_barcode(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_state = {}
    coordinator._known_delivery_times = {}

    events = []
    hass.bus.async_listen(f"{DOMAIN}_parcel_registered", lambda e: events.append(e))

    coordinator._fire_change_events([{"status": ParcelStatus.UNKNOWN}])
    await hass.async_block_till_done()

    assert events == []


async def test_outgoing_no_event_for_unknown_barcode_or_unchanged_status(hass):
    entry = _entry()
    entry.add_to_hass(hass)
    coordinator = MondialRelayCoordinator(hass, _client(), entry)
    coordinator._known_outgoing_state = {"A": ParcelStatus.IN_TRANSIT}

    events = []
    hass.bus.async_listen(
        f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: events.append(e)
    )

    coordinator._fire_outgoing_change_events(
        [
            _fake_parcel("A", ParcelStatus.IN_TRANSIT),  # unchanged
            _fake_parcel("UNKNOWN", ParcelStatus.OUT_FOR_DELIVERY),  # not tracked
            {"status": ParcelStatus.OUT_FOR_DELIVERY},  # no barcode at all
        ]
    )
    await hass.async_block_till_done()

    assert events == []
