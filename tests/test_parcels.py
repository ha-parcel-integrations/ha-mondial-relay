"""Tests for the pure parcel-mapping helpers.

These need no Home Assistant instance — the whole point of keeping
``parcels.py`` free of I/O is that the carrier-specific mapping can be tested
as plain functions.
"""
from datetime import datetime, timedelta, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.const import (
    CAPABILITIES,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    ParcelStatus,
)
from custom_components.mondial_relay.parcels import (
    apply_delivered_filter,
    dedupe_by_shipment_uid,
    normalize_parcel,
    parse_iso,
    sort_parcels_by_ts,
    warn_inbox_overlap_once,
)

from .payloads import active_item, delivered_item, list_item

# ---------------------------------------------------------------------------
# timestamp helper
# ---------------------------------------------------------------------------


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


# ---------------------------------------------------------------------------
# dedupe_by_shipment_uid / warn_inbox_overlap_once
# ---------------------------------------------------------------------------


def test_dedupe_drops_repeated_shipment_uid_keeping_first():
    first = list_item(shipment_uid="uid-1", tracing_code="code-a")
    duplicate = list_item(shipment_uid="uid-1", tracing_code="code-b")
    other = list_item(shipment_uid="uid-2", tracing_code="code-c")

    deduped = dedupe_by_shipment_uid([first, duplicate, other])

    assert len(deduped) == 2
    assert deduped[0]["expedition"]["tracingCode"] == "code-a"


def test_dedupe_keeps_items_without_a_uid():
    no_uid = {"expedition": {}, "delivery": {}}
    assert dedupe_by_shipment_uid([no_uid, no_uid]) == [no_uid, no_uid]


def test_dedupe_skips_non_dict_entries():
    assert dedupe_by_shipment_uid([active_item(), "junk"]) == [active_item()]


def test_warn_inbox_overlap_once(caplog):
    warn_inbox_overlap_once(0)
    assert caplog.text == ""
    warn_inbox_overlap_once(2)
    warn_inbox_overlap_once(2)
    assert caplog.text.count("both incoming and outgoing") == 1


# ---------------------------------------------------------------------------
# normalize_parcel — the canonical contract, pre-1.0 rules
# ---------------------------------------------------------------------------

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def test_normalize_publishes_exactly_the_canonical_keys():
    """The aggregator and cross-carrier dashboards depend on this key set."""
    assert list(normalize_parcel(active_item())) == CANONICAL_KEYS


def test_capabilities_are_known_values():
    assert CAPABILITIES <= KNOWN_CAPABILITIES


def test_capabilities_are_empty_pre_1_0():
    """No optional field is confirmed yet — every one stays null."""
    assert CAPABILITIES == frozenset()


def test_normalize_maps_step_section():
    expected = {
        1: ParcelStatus.AT_PICKUP_POINT,
        2: ParcelStatus.IN_TRANSIT,
        3: ParcelStatus.DELIVERED,
        0: ParcelStatus.UNKNOWN,
        4: ParcelStatus.UNKNOWN,
        99: ParcelStatus.UNKNOWN,
    }
    for step_section, status in expected.items():
        parcel = normalize_parcel(list_item(step_section=step_section))
        assert parcel["status"] == status
        assert parcel["raw_status"] == str(step_section)


def test_normalize_mapped_step_section_does_not_warn(caplog, monkeypatch):
    monkeypatch.setattr(
        "custom_components.mondial_relay.parcels._unmapped_steps_logged", set()
    )
    for step_section in (1, 2, 3):
        normalize_parcel(list_item(step_section=step_section))
    assert "stepSection" not in caplog.text


def test_normalize_has_problem_overrides_an_active_status():
    for step_section in (1, 2, 99):
        parcel = normalize_parcel(list_item(step_section=step_section, has_problem=True))
        assert parcel["status"] == ParcelStatus.PROBLEM
        assert parcel["delivered"] is False


def test_normalize_warns_once_per_unmapped_step_section(caplog, monkeypatch):
    monkeypatch.setattr(
        "custom_components.mondial_relay.parcels._unmapped_steps_logged", set()
    )
    normalize_parcel(list_item(step_section=7))
    normalize_parcel(list_item(step_section=7))
    normalize_parcel(list_item(step_section=8))
    assert caplog.text.count("stepSection=7") == 1
    assert caplog.text.count("stepSection=8") == 1


def test_normalize_raw_status_none_without_a_step_section():
    raw = active_item()
    del raw["expedition"]["stepSection"]
    assert normalize_parcel(raw)["raw_status"] is None


def test_normalize_delivered_uses_the_tracing_date():
    parcel = normalize_parcel(delivered_item())
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2026-04-27T23:03:58Z"


def test_normalize_delivered_with_a_problem_stays_delivered():
    parcel = normalize_parcel(list_item(step_section=3, has_problem=True))
    assert parcel["status"] == ParcelStatus.DELIVERED
    assert parcel["delivered"] is True


def test_normalize_active_parcel_has_no_delivered_at():
    parcel = normalize_parcel(active_item())
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None


def test_normalize_unconfirmed_fields_stay_null():
    parcel = normalize_parcel(active_item())
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None
    assert parcel["url"] is None
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None
    assert parcel["history"] is None
    assert parcel["receiver"] is None


def test_normalize_barcode_and_sender():
    parcel = normalize_parcel(active_item())
    assert parcel["barcode"] == str(active_item()["expedition"]["shipmentId"])
    assert parcel["sender"] == "V1VINTNL"
    assert parcel["carrier"] == "Mondial Relay"


def test_normalize_blank_sender_becomes_none():
    raw = list_item(brand_label=None)
    assert normalize_parcel(raw)["sender"] is None
    raw = list_item(brand_label="")
    assert normalize_parcel(raw)["sender"] is None


def test_normalize_barcode_falls_back_to_tracing_code():
    raw = active_item()
    del raw["expedition"]["shipmentId"]
    assert normalize_parcel(raw)["barcode"] == raw["expedition"]["tracingCode"]


def test_normalize_missing_barcode_becomes_none():
    raw = active_item()
    raw["expedition"]["shipmentId"] = None
    raw["expedition"]["tracingCode"] = ""
    assert normalize_parcel(raw)["barcode"] is None


def test_normalize_handles_missing_expedition_and_delivery():
    assert normalize_parcel({})["status"] == ParcelStatus.UNKNOWN


# ---------------------------------------------------------------------------
# raw carries the full carrier record
# ---------------------------------------------------------------------------


def test_normalize_raw_is_the_full_list_item():
    raw = list_item()
    assert normalize_parcel(raw)["raw"] == raw


# ---------------------------------------------------------------------------
# sort_parcels_by_ts
# ---------------------------------------------------------------------------


def test_sort_parcels_ascending_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "planned_from": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "planned_from": None},
        {"barcode": "c", "planned_from": "2026-05-01T10:00:00Z"},
    ]
    ordered = [p["barcode"] for p in sort_parcels_by_ts(parcels, "planned_from")]
    assert ordered == ["c", "a", "b"]


def test_sort_parcels_descending_still_puts_unparseable_last():
    parcels = [
        {"barcode": "a", "delivered_at": "2026-05-02T10:00:00Z"},
        {"barcode": "b", "delivered_at": "nonsense"},
        {"barcode": "c", "delivered_at": "2026-05-01T10:00:00Z"},
    ]
    ordered = [
        p["barcode"]
        for p in sort_parcels_by_ts(parcels, "delivered_at", descending=True)
    ]
    assert ordered == ["a", "c", "b"]


# ---------------------------------------------------------------------------
# apply_delivered_filter
# ---------------------------------------------------------------------------


def _entry(filter_type: str, amount: int) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DELIVERED_FILTER_TYPE: filter_type,
            CONF_DELIVERED_FILTER_AMOUNT: amount,
        },
        unique_id=DOMAIN,
    )


def _delivered_pair() -> list[dict]:
    now = datetime.now(timezone.utc)
    return [
        {"barcode": "RECENT", "delivered_at": (now - timedelta(days=1)).isoformat()},
        {"barcode": "OLD", "delivered_at": (now - timedelta(days=30)).isoformat()},
    ]


def test_delivered_filter_by_days():
    kept = apply_delivered_filter(_delivered_pair(), _entry("days", 7))
    assert [p["barcode"] for p in kept] == ["RECENT"]


def test_delivered_filter_by_count():
    parcels = _delivered_pair()
    assert apply_delivered_filter(parcels, _entry("parcels", 1)) == parcels[:1]


def test_delivered_filter_keeps_unparseable_timestamp():
    parcels = [{"barcode": "WEIRD", "delivered_at": "nonsense"}]
    assert apply_delivered_filter(parcels, _entry("days", 7)) == parcels
