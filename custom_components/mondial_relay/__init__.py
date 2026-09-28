"""Mondial Relay parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MondialRelayApiClient
from .const import CONF_DEVICE_UID, CONF_REFRESH_TOKEN, PLATFORMS
from .coordinator import MondialRelayCoordinator
from .oauth import MondialRelayOAuthSession

_LOGGER = logging.getLogger(__name__)


@dataclass
class MondialRelayData:
    """Runtime data attached to a Mondial Relay config entry."""

    client: MondialRelayApiClient
    coordinator: MondialRelayCoordinator
    oauth: MondialRelayOAuthSession
    session: aiohttp.ClientSession


type MondialRelayConfigEntry = ConfigEntry[MondialRelayData]


async def async_setup_entry(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> bool:
    """Set up Mondial Relay from a config entry."""
    # Each config entry gets its own session, mirroring the other
    # account-based carriers in the suite, even though this backend
    # authenticates with a bearer token rather than a cookie.
    session = aiohttp.ClientSession(
        connector=async_get_clientsession(hass).connector,
        connector_owner=False,
    )
    oauth = MondialRelayOAuthSession(
        session, refresh_token=entry.data.get(CONF_REFRESH_TOKEN)
    )
    client = MondialRelayApiClient(
        oauth, session, device_uid=entry.data[CONF_DEVICE_UID]
    )
    coordinator = MondialRelayCoordinator(hass, client, entry)

    @callback
    def _persist_rotated_refresh_token() -> None:
        # The identity provider is free to rotate the refresh token on any
        # exchange; a rotated one that is never persisted strands the entry
        # on a value the provider has already invalidated.
        if oauth.pop_refresh_token_changed():
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: oauth.refresh_token}
            )

    try:
        # Fetch initial data here, before forwarding to platforms. Raising
        # ConfigEntryNotReady/ConfigEntryAuthFailed from a forwarded platform
        # is too late for HA to catch cleanly (it logs a warning and
        # half-sets-up the entry); doing the first refresh here lets a
        # transient failure, or a rejected refresh token, fail the whole
        # entry so HA retries it (or starts reauth) cleanly.
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        # Without this, every setup retry leaks a session.
        await session.close()
        raise
    _persist_rotated_refresh_token()

    entry.runtime_data = MondialRelayData(
        client=client, coordinator=coordinator, oauth=oauth, session=session
    )
    entry.async_on_unload(
        coordinator.async_add_listener(_persist_rotated_refresh_token)
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await session.close()
        raise

    # No entry.add_update_listener: the options flow calls
    # async_schedule_reload itself. Combining an update listener with a
    # reload-on-update flow is deprecated and becomes an error in HA 2026.12+.
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> bool:
    """Unload a Mondial Relay config entry."""
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.session.close()
        return True
    return False
