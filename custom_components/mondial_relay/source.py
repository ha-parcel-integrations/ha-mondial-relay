"""Which kind of hub a config entry is."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import CONF_SOURCE, SOURCE_ACCOUNT, SOURCE_TRACKING


def is_tracking_entry(entry: ConfigEntry) -> bool:
    """Whether ``entry`` is a tracking hub; entries without a source are accounts."""
    return entry.data.get(CONF_SOURCE, SOURCE_ACCOUNT) == SOURCE_TRACKING
