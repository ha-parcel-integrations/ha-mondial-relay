"""Mondial Relay parcel tracker custom component for Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.api import MondialRelayApiClient
from .account.coordinator import MondialRelayCoordinator
from .account.oauth import MondialRelayOAuthSession
from .const import CONF_DEVICE_UID, CONF_REFRESH_TOKEN, DOMAIN, PLATFORMS
from .services import async_setup_services, async_unload_services
from .source import is_tracking_entry
from .tracking.api import MondialRelayTrackingClient
from .tracking.coordinator import MondialRelayTrackingCoordinator

_LOGGER = logging.getLogger(__name__)


type MondialRelayParcelCoordinator = (
    MondialRelayCoordinator | MondialRelayTrackingCoordinator
)


@dataclass
class MondialRelayData:
    """Runtime data attached to a Mondial Relay config entry.

    ``oauth`` and ``session`` belong to an account hub only; a tracking hub
    opens a fresh session per poll cycle instead.
    """

    client: MondialRelayApiClient | MondialRelayTrackingClient
    coordinator: MondialRelayParcelCoordinator
    oauth: MondialRelayOAuthSession | None = None
    session: aiohttp.ClientSession | None = None


type MondialRelayConfigEntry = ConfigEntry[MondialRelayData]


async def async_setup_entry(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> bool:
    """Set up Mondial Relay from a config entry."""
    if is_tracking_entry(entry):
        return await _async_setup_tracking_entry(hass, entry)
    return await _async_setup_account_entry(hass, entry)


async def _async_setup_tracking_entry(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> bool:
    shared = async_get_clientsession(hass)

    def _session_factory() -> aiohttp.ClientSession:
        # Its own cookie jar, so the tracking cookies never reach Home
        # Assistant's shared session or any other integration.
        return aiohttp.ClientSession(connector=shared.connector, connector_owner=False)

    client = MondialRelayTrackingClient(_session_factory, hass.config.language)
    coordinator = MondialRelayTrackingCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to
    # catch cleanly (it logs a warning and half-sets-up the entry).
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MondialRelayData(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, settings) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. This is also the resume path after polling
    # fully suspended.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    async_setup_services(hass)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> None:
    """Apply a tracking hub's changed options by refreshing its coordinator."""
    await entry.runtime_data.coordinator.async_request_refresh()


async def _async_setup_account_entry(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> bool:
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
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if entry.runtime_data.session is not None:
        await entry.runtime_data.session.close()
    if is_tracking_entry(entry):
        # The services are shared by every tracking hub, so they go only
        # with the last one.
        others_loaded = any(
            other.entry_id != entry.entry_id
            and other.state is ConfigEntryState.LOADED
            and is_tracking_entry(other)
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        if not others_loaded:
            async_unload_services(hass)
    return True
