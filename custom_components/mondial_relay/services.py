"""Services for the Mondial Relay parcel tracker integration.

`mondial_relay.track_parcel` / `mondial_relay.untrack_parcel` add or remove a
tracked parcel on a tracking hub without opening its options — so a
dashboard button can start tracking a parcel straight away. Format-only
validation (non-empty tracking code), deliberately not a live lookup — a
service call must not block on a network round-trip.

A tracking hub is one postcode, so a parcel normally needs only its tracking
code. The optional ``postal_code`` picks *which* hub when there are several.
Account hubs are never a target: their parcels come from the account itself.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .config_flow import (
    normalize_postal_code,
    normalize_tracking_code,
    valid_tracking_code,
)
from .const import CONF_PARCELS, CONF_POSTAL_CODE, CONF_TRACKING_CODE, DOMAIN
from .source import is_tracking_entry

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRACKING_CODE): cv.string,
        vol.Optional(CONF_POSTAL_CODE): cv.string,
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


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Mondial Relay services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = _validated_code(call)
        entries = _resolve_entries(hass, call.data.get(CONF_POSTAL_CODE))
        if len(entries) > 1:
            raise ServiceValidationError(
                "Several Mondial Relay tracking hubs are set up — pass "
                "postal_code to choose one"
            )
        entry = entries[0]
        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == tracking_code for p in parcels):
            return  # already tracked — no-op
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
                hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PARCELS: kept}
                )

    hass.services.async_register(DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Mondial Relay services."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
