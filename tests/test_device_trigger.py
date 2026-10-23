"""Tests for Mondial Relay device triggers."""

from unittest.mock import AsyncMock

from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.const import (
    CONF_POSTAL_CODE,
    CONF_SOURCE,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.mondial_relay.device_trigger import (
    TRIGGER_EVENTS,
    async_attach_trigger,
    async_get_triggers,
)


async def test_get_triggers_returns_all_six(hass):
    triggers = await async_get_triggers(hass, "device123")
    types = {t["type"] for t in triggers}
    assert types == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
        "outgoing_parcel_status_changed",
        "outgoing_parcel_delivered",
    }
    for trigger in triggers:
        assert trigger["domain"] == DOMAIN
        assert trigger["device_id"] == "device123"


async def test_a_tracking_hub_offers_no_outgoing_triggers(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_POSTAL_CODE: "1000"},
    )
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    triggers = await async_get_triggers(hass, device.id)
    assert {t["type"] for t in triggers} == {
        "parcel_registered",
        "parcel_status_changed",
        "parcel_delivered",
        "parcel_delivery_time_changed",
    }


async def test_an_account_device_keeps_the_outgoing_triggers(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    triggers = await async_get_triggers(hass, device.id)
    assert "outgoing_parcel_delivered" in {t["type"] for t in triggers}


def test_trigger_events_map_to_domain_prefix():
    assert TRIGGER_EVENTS["parcel_registered"] == f"{DOMAIN}_parcel_registered"


async def test_attach_trigger_fires_only_for_its_device(hass):
    action = AsyncMock()
    unsub = await async_attach_trigger(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": "device123",
            "type": "parcel_delivered",
        },
        action,
        {"trigger_data": {}, "variables": {}},
    )

    hass.bus.async_fire(TRIGGER_EVENTS["parcel_delivered"], {"device_id": "other"})
    hass.bus.async_fire(TRIGGER_EVENTS["parcel_delivered"], {"device_id": "device123"})
    await hass.async_block_till_done()
    unsub()

    assert action.call_count == 1
    event = action.call_args.args[0]["trigger"]["event"]
    assert event.data["device_id"] == "device123"
