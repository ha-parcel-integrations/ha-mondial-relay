"""Diagnostics support for the Mondial Relay parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import MondialRelayConfigEntry
from .const import CONF_ACCOUNT_SUBJECT, CONF_DEVICE_UID, CONF_REFRESH_TOKEN

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an account, a device or a specific shipment.
# Over-redacting is cheap; under-redacting leaks account/session material
# into a GitHub thread.
TO_REDACT = {
    CONF_REFRESH_TOKEN,
    CONF_ACCOUNT_SUBJECT,
    CONF_DEVICE_UID,
    # canonical fields we publish ourselves
    "barcode",
    "sender",
    "receiver",
    "url",
    # carrier payload under raw
    "shipmentId",
    "shipmentUid",
    "tracingCode",
    "tracingSubCode",
    "tracingDate",
    "brandLabel",
    "markAlphaCode",
    "markNumCode",
    "agencyCode",
    "availabilityDate",
    "code",
    "country",
    "deadline",
    "deliveryPointId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: MondialRelayConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Mondial Relay config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "outgoing_active": len(coordinator.outgoing or []),
            "outgoing_delivered": len(coordinator.delivered_outgoing or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
        "outgoing": async_redact_data(coordinator.outgoing or [], TO_REDACT),
        "outgoing_delivered": async_redact_data(
            coordinator.delivered_outgoing or [], TO_REDACT
        ),
    }
