"""Tests for Mondial Relay setup and unload."""

from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.account.api import (
    MondialRelayApiError,
    MondialRelayAuthError,
)
from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DEVICE_UID,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.mondial_relay.tracking.api import (
    MondialRelayTrackingRefusedError,
)

from .payloads import ACTIVE_CODE, active_item
from .tracking.fake_client import FakeClient
from .tracking.payloads import POSTAL_CODE, TRACKING_CODE, available

CLIENT = "custom_components.mondial_relay.account.api.MondialRelayApiClient"


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
    )


def _mock_lists(received=None, shipped=None):
    return (
        patch(
            f"{CLIENT}.async_get_list_received",
            new=AsyncMock(return_value=received or []),
        ),
        patch(
            f"{CLIENT}.async_get_list_shipped",
            new=AsyncMock(return_value=shipped or []),
        ),
    )


async def test_setup_and_unload(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    received, shipped = _mock_lists(received=[active_item()])
    with received, shipped:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_incoming_parcels"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_rejected_refresh_token_starts_reauth(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        f"{CLIENT}.async_get_list_received",
        new=AsyncMock(side_effect=MondialRelayAuthError("HTTP 401")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


@pytest.mark.parametrize(
    "error",
    [MondialRelayApiError("HTTP 500"), aiohttp.ClientError("boom")],
)
async def test_outage_retries_instead_of_reauth(hass, error):
    """A 5xx/network error must retry with backoff — never push into reauth."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(f"{CLIENT}.async_get_list_received", new=AsyncMock(side_effect=error)):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not hass.config_entries.flow.async_progress()


async def test_failed_platform_setup_closes_the_session(hass):
    """Every failed-setup path must close the per-entry session, or each retry
    leaks one."""
    entry = _entry()
    entry.add_to_hass(hass)

    received, shipped = _mock_lists(received=[active_item()])
    with (
        received,
        shipped,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform blew up")),
        ),
        patch("aiohttp.ClientSession.close", new=AsyncMock()) as close,
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    close.assert_awaited()


async def test_rotated_refresh_token_is_persisted_after_a_poll(hass):
    """A refresh token the identity provider silently rotated must survive a restart."""
    entry = _entry()
    entry.add_to_hass(hass)

    received, shipped = _mock_lists()
    with received, shipped:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    oauth = entry.runtime_data.oauth
    oauth.refresh_token = "rotated-token"
    oauth._refresh_token_changed = True

    received2, shipped2 = _mock_lists()
    with received2, shipped2:
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

    assert entry.data[CONF_REFRESH_TOKEN] == "rotated-token"


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    received = AsyncMock(return_value=[active_item()])
    with (
        patch(f"{CLIENT}.async_get_list_received", new=received),
        patch(f"{CLIENT}.async_get_list_shipped", new=AsyncMock(return_value=[])),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        received.return_value = [active_item(shipment_id=22222222)]
        await entry.runtime_data.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_22222222"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


async def test_an_entry_without_a_source_sets_up_as_an_account(hass):
    """Entries from before tracking hubs existed carry no source at all."""
    entry = _entry()
    assert CONF_SOURCE not in entry.data
    entry.add_to_hass(hass)

    received, shipped = _mock_lists()
    with received, shipped, patch(TRACKING_CLIENT) as tracking_client:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.oauth is not None
    tracking_client.assert_not_called()
    assert not hass.services.has_service(DOMAIN, "track_parcel")


# ---------------------------------------------------------------------------
# tracking hubs
# ---------------------------------------------------------------------------

TRACKING_CLIENT = "custom_components.mondial_relay.MondialRelayTrackingClient"


def _tracking_entry(postal_code: str = POSTAL_CODE, codes=()) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Mondial Relay ({postal_code})",
        unique_id=f"tracking:{postal_code}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_POSTAL_CODE: postal_code,
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
        },
    )


async def test_tracking_hub_setup_and_unload(hass):
    entry = _tracking_entry(codes=(TRACKING_CODE,))
    entry.add_to_hass(hass)
    client = FakeClient({TRACKING_CODE: available()})

    with patch(TRACKING_CLIENT, return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")

    registry = er.async_get(hass)
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert f"{entry.entry_id}_incoming_parcels" in unique_ids
    assert f"{entry.entry_id}_awaiting_pickup" in unique_ids
    assert f"{entry.entry_id}_{TRACKING_CODE}" in unique_ids
    # Incoming parcels only, so no outgoing lists that would always be zero.
    assert f"{entry.entry_id}_outgoing_parcels" not in unique_ids
    assert f"{entry.entry_id}_outgoing_delivered_parcels" not in unique_ids

    device = next(
        iter(dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id))
    )
    assert device.name == f"Mondial Relay ({POSTAL_CODE})"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_services_stay_while_another_tracking_hub_is_loaded(hass):
    first, second = _tracking_entry("1000"), _tracking_entry("2000")
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    with patch(TRACKING_CLIENT, side_effect=lambda *a: FakeClient()):
        # Setting up the domain sets up both of its entries.
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
    assert second.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_a_tracking_hub_applies_option_changes_live(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    client = FakeClient({TRACKING_CODE: available()})
    with patch(TRACKING_CLIENT, return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert client.cycles == 0

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [{CONF_TRACKING_CODE: TRACKING_CODE}],
            },
        )
        await hass.async_block_till_done()

    reload.assert_not_called()
    assert client.cycles == 1
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{TRACKING_CODE}"
    )


async def test_a_tracking_hub_retries_when_the_first_poll_fails(hass):
    entry = _tracking_entry(codes=(TRACKING_CODE,))
    entry.add_to_hass(hass)
    client = FakeClient(cycle_error=MondialRelayTrackingRefusedError("HTTP 403"))
    with patch(TRACKING_CLIENT, return_value=client):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_each_tracking_cycle_gets_its_own_session(hass):
    entry = _tracking_entry()
    entry.add_to_hass(hass)
    with patch(TRACKING_CLIENT, return_value=FakeClient()) as tracking_client:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    session_factory, language = tracking_client.call_args.args
    assert language == hass.config.language
    first, second = session_factory(), session_factory()
    try:
        assert first is not second
        assert first.cookie_jar is not second.cookie_jar
        assert first.cookie_jar is not async_get_clientsession(hass).cookie_jar
    finally:
        await first.close()
        await second.close()
