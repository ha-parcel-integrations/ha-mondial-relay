"""The parcel contract's checks, run over every normalised sample."""

from datetime import date, datetime

import pytest

from custom_components.mondial_relay.account.parcels import (
    normalize_parcel as normalize_account,
)
from custom_components.mondial_relay.const import ParcelStatus
from custom_components.mondial_relay.parcel_lists import parse_iso, planned_window
from custom_components.mondial_relay.tracking.parcels import (
    CARRIER_TZ,
    normalize_parcel as normalize_tracking,
)

from .payloads import active_item, delivered_item, list_item
from .tracking.payloads import (
    at_step,
    available,
    delivered,
    in_distribution,
    in_transit_with_estimate,
)

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
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

PAGE = "https://example.invalid/track"


def _tracking(expedition: dict, **kwargs) -> dict:
    return normalize_tracking(expedition, page=PAGE, **kwargs)


def _estimate(value: str) -> dict:
    expedition = in_transit_with_estimate()
    expedition["EstimatedDeliveryDate"] = value
    return expedition


SAMPLES = {
    "account_active": lambda: normalize_account(active_item()),
    "account_pickup": lambda: normalize_account(list_item(step_section=1)),
    "account_delivered": lambda: normalize_account(delivered_item()),
    "account_problem": lambda: normalize_account(list_item(has_problem=True)),
    "tracking_active": lambda: _tracking(in_transit_with_estimate()),
    "tracking_registered": lambda: _tracking(at_step(1)),
    "tracking_distribution": lambda: _tracking(in_distribution()),
    "tracking_at_pickup_point": lambda: _tracking(available(), include_history=True),
    "tracking_collected": lambda: _tracking(delivered(), include_history=True),
    "tracking_other_mode": lambda: _tracking(_other_mode()),
    "tracking_bare": lambda: _tracking({"Numero": 1}),
}


def _other_mode() -> dict:
    expedition = available()
    expedition["DeliveryMode"] = "HOM"
    return expedition


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return SAMPLES[request.param]()


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_the_estimate_survives_while_the_parcel_is_in_flight():
    parcel = _tracking(in_transit_with_estimate())
    assert parcel["planned_from"] is not None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = _tracking(delivered())
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "OTHER TEST SHOP"


def test_a_pickup_route_is_pickup_before_the_parcel_arrives():
    parcel = _tracking(in_transit_with_estimate())
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["pickup"] is True


def test_another_delivery_mode_is_not_pickup():
    parcel = _tracking(_other_mode())
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_the_account_step_for_a_pickup_point_is_pickup():
    parcel = normalize_account(list_item(step_section=1))
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None


def test_an_estimate_is_a_whole_local_day():
    parcel = _tracking(_estimate("2099-01-05T13:52:54"))
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert start.utcoffset() == CARRIER_TZ.utcoffset(start.replace(tzinfo=None))
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()


def test_planned_window_shapes():
    zone = CARRIER_TZ
    assert planned_window(None, None, zone=zone) == (None, None)
    assert planned_window(None, "2099-01-05", zone=zone)[0].startswith("2099-01-05T00")
    assert planned_window(
        "2099-01-05T10:00:00+01:00", "2099-01-05T12:00:00+01:00", zone=zone
    ) == (
        "2099-01-05T10:00:00+01:00",
        "2099-01-05T12:00:00+01:00",
    )
    assert (
        planned_window(
            "2099-01-05T12:00:00+01:00", "2099-01-05T10:00:00+01:00", zone=zone
        )[0]
        == "2099-01-05T10:00:00+01:00"
    )
    assert planned_window("2099-01-05", "2099-01-07", zone=zone) == (
        "2099-01-05T00:00:00+01:00",
        "2099-01-07T23:59:59+01:00",
    )
    assert (
        planned_window(date(2099, 1, 5), None, zone=zone)[1]
        == "2099-01-05T23:59:59+01:00"
    )
    assert planned_window("2099-01-05T23:59:59+01:00", None, zone=zone) == (
        "2099-01-05T23:59:59+01:00",
        None,
    )
    assert (
        planned_window(datetime(2099, 1, 5, 9), None, zone=zone)[0]
        == "2099-01-05T09:00:00+01:00"
    )
    assert planned_window("garbage", None, zone=zone) == (None, None)
    assert planned_window(5, None, zone=zone) == (None, None)
