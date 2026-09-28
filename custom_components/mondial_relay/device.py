"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry — and because every config entry shares the literal
entry title "Mondial Relay" (the plan's setup contract, so two accounts stay
distinguishable only through the device name, not the entry title).
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_ACCOUNT_SUBJECT, DOMAIN

CONFIGURATION_URL = "https://www.mondialrelay.fr"

ATTRIBUTION = "Data provided by Mondial Relay"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this account.

    A short, non-reversible suffix of the account subject distinguishes two
    configured accounts, since the entry title itself does not — falls back
    to the entry id when the subject could not be decoded.
    """
    subject = entry.data.get(CONF_ACCOUNT_SUBJECT)
    suffix = subject[-6:] if subject and subject != "unknown" else entry.entry_id[-6:]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Mondial Relay ({suffix})",
        manufacturer="Mondial Relay",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
