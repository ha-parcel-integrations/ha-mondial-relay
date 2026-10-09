"""Tests for Mondial Relay diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.mondial_relay.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .payloads import list_item


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.data = {
        "refresh_token": "rt-secret",
        "account_subject": "subject-1",
        "device_uid": "device-1",
    }
    entry.options = {"delivered_filter_type": "days"}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "06180712345678",
            "sender": "V1VINTNL",
            "receiver": None,
            "status": "in_transit",
            "raw_status": "2",
            "delivered_at": "2026-04-27T23:03:58Z",
            "raw": list_item(),
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = []
    entry.runtime_data.coordinator.delivered_outgoing = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "outgoing_active": 0,
        "outgoing_delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
    }
    # credentials and canonical identifying fields are redacted
    assert result["entry_data"]["refresh_token"] == "**REDACTED**"
    assert result["entry_data"]["account_subject"] == "**REDACTED**"
    assert result["entry_data"]["device_uid"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert result["incoming"][0]["sender"] == "**REDACTED**"
    assert result["incoming"][0]["delivered_at"] == "**REDACTED**"
    # non-identifying fields survive, or the diagnostics would be useless
    assert result["incoming"][0]["status"] == "in_transit"
    assert result["incoming"][0]["raw_status"] == "2"
    raw = result["incoming"][0]["raw"]
    assert raw["expedition"]["stepSection"] == 2
    for key in ("shipmentId", "shipmentUid", "tracingCode", "tracingSubCode",
                "tracingDate", "brandLabel", "markAlphaCode", "markNumCode"):
        assert raw["expedition"][key] == "**REDACTED**"
    for key in ("agencyCode", "availabilityDate", "code", "country",
                "deadline", "deliveryPointId"):
        assert raw["delivery"][key] == "**REDACTED**"


async def test_diagnostics_covers_outgoing_lists(hass):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    entry.runtime_data.coordinator.current_tier_minutes = 45
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=45)
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.outgoing = [{"barcode": "OUT1"}]
    entry.runtime_data.coordinator.delivered_outgoing = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"]["outgoing_active"] == 1
    assert result["outgoing"][0]["barcode"] == "**REDACTED**"
