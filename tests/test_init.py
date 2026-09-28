"""Tests for Mondial Relay setup and unload."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.api import (
    MondialRelayApiError,
    MondialRelayAuthError,
)
from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DEVICE_UID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .payloads import ACTIVE_CODE, active_item

CLIENT = "custom_components.mondial_relay.api.MondialRelayApiClient"


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
