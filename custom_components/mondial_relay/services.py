"""Services for the Mondial Relay parcel tracker integration.

`mondial_relay.track_parcel` / `mondial_relay.untrack_parcel` add or remove a
tracked parcel on a tracking hub without opening its options — so a
dashboard button can start tracking a parcel straight away. Format-only
validation (non-empty tracking code), deliberately not a live lookup — a
service call must not block on a network round-trip.

A tracking hub is one postcode, so a parcel normally needs only its tracking
code. The optional ``postal_code`` picks *which* hub when there are several.
Account hubs are never a target: their parcels come from the account itself.

`mondial_relay.update_contents` sets or clears the text describing what is in
a parcel, tracked or not, on the hub(s) or account(s) that list the code.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import (
    normalize_postal_code,
    normalize_tracking_code,
    valid_tracking_code,
)
from .const import (
    CONF_CONFIG_ENTRY_ID,
    CONF_CONTENTS,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    DOMAIN,
)
from .contents import ContentsStore, normalize_code
from .source import is_tracking_entry

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"
SERVICE_UPDATE_CONTENTS = "update_contents"

_TRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
    }
)
_UNTRACK_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
    }
)
_UPDATE_CONTENTS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_CONTENTS): cv.string,
        vol.Optional(CONF_CONFIG_ENTRY_ID): cv.string,
    }
)


def _tracking_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if is_tracking_entry(entry)
    ]
    if not entries:
        raise ServiceValidationError(
            "No Mondial Relay tracking hub is set up — add one with the "
            "Tracking code option first"
        )
    return entries


def _resolve_entries(hass: HomeAssistant, postal_code: str | None) -> list[ConfigEntry]:
    """Return the tracking hub ``postal_code`` names, or every tracking hub."""
    entries = _tracking_entries(hass)
    if not postal_code:
        return entries
    target = normalize_postal_code(postal_code)
    matching = [e for e in entries if e.options.get(CONF_POSTAL_CODE) == target]
    if not matching:
        raise ServiceValidationError(
            f"No Mondial Relay tracking hub for postcode {target}"
        )
    return matching


def _validated_code(call: ServiceCall) -> str:
    tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
    if not valid_tracking_code(tracking_code):
        raise ServiceValidationError("A Mondial Relay tracking code is required")
    return tracking_code


def _loaded_coordinator(entry: ConfigEntry):
    """Return the coordinator of a set-up entry, or raise when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("Mondial Relay is not loaded")
    return entry.runtime_data.coordinator


def _contents_store(entry: ConfigEntry) -> ContentsStore | None:
    """Return the entry's contents store, or ``None`` when it is not loaded."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data.coordinator.contents_store


def async_setup_services(hass: HomeAssistant, *, hub: bool = True) -> None:
    """Register the Mondial Relay services (idempotent).

    ``hub`` is False for an account entry: it needs only ``update_contents``.
    """

    async def _track(call: ServiceCall) -> None:
        tracking_code = _validated_code(call)
        entries = _resolve_entries(hass, call.data.get(CONF_POSTAL_CODE))
        if len(entries) > 1:
            raise ServiceValidationError(
                "Several Mondial Relay tracking hubs are set up — pass "
                "postal_code to choose one"
            )
        entry = entries[0]
        coordinator = _loaded_coordinator(entry) if CONF_CONTENTS in call.data else None
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == tracking_code for p in parcels):
            # Already tracked: only a changed contents value does anything.
            if coordinator is not None and coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            ):
                await coordinator.async_request_refresh()
            return
        if coordinator is not None:
            # Stored before the options write, whose listener refreshes.
            coordinator.contents_store.async_set(
                tracking_code, call.data[CONF_CONTENTS]
            )
        parcels.append({CONF_TRACKING_CODE: tracking_code})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = _validated_code(call)
        # Without a postcode, the code goes from whichever hub tracks it.
        for entry in _resolve_entries(hass, call.data.get(CONF_POSTAL_CODE)):
            current = entry.options.get(CONF_PARCELS, [])
            kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
            if len(kept) != len(current):
                if (store := _contents_store(entry)) is not None:
                    store.async_set(tracking_code, None)
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
                )

    async def _update_contents(call: ServiceCall) -> None:
        code = normalize_code(call.data[CONF_TRACKING_CODE])
        if not code:
            raise ServiceValidationError("A tracking code is required")
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        ]
        if not loaded:
            raise ServiceValidationError("Mondial Relay is not set up")
        if entry_id := call.data.get(CONF_CONFIG_ENTRY_ID):
            targets = [entry for entry in loaded if entry.entry_id == entry_id]
            if not targets:
                raise ServiceValidationError(
                    f"'{entry_id}' is not a loaded Mondial Relay entry"
                )
        elif len(loaded) == 1:
            targets = loaded
        else:
            # A code can sit in two entries, so every entry that lists it gets
            # the value; guessing an entry would write to the wrong one.
            targets = [
                entry
                for entry in loaded
                if code in entry.runtime_data.coordinator.known_codes
            ]
            if not targets:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="update_contents_entry_required",
                )
        for entry in targets:
            coordinator = entry.runtime_data.coordinator
            if coordinator.contents_store.async_set(code, call.data.get(CONF_CONTENTS)):
                await coordinator.async_request_refresh()

    if hub and not hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        hass.services.async_register(
            DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
        )
        hass.services.async_register(
            DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
        )
    if not hass.services.has_service(DOMAIN, SERVICE_UPDATE_CONTENTS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_UPDATE_CONTENTS,
            _update_contents,
            schema=_UPDATE_CONTENTS_SCHEMA,
        )


def async_unload_services(
    hass: HomeAssistant, *, hubs_left: bool, entries_left: bool
) -> None:
    """Remove the Mondial Relay services their entries no longer need.

    The hub services go with the last tracking hub; ``update_contents`` stays
    while any entry, hub or account, is still loaded.
    """
    services = []
    if not hubs_left:
        services += [SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL]
    if not entries_left:
        services.append(SERVICE_UPDATE_CONTENTS)
    for service in services:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
