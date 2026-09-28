"""Tests for Mondial Relay sensor property logic."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from custom_components.mondial_relay.const import ParcelStatus
from custom_components.mondial_relay.sensor import (
    MondialRelayAwaitingPickupSensor,
    MondialRelayDeliveredParcelsSensor,
    MondialRelayEnRouteToPickupPointSensor,
    MondialRelayIncomingParcelsSensor,
    MondialRelayLastUpdateSensor,
    MondialRelayNextDeliverySensor,
    MondialRelayOutgoingDeliveredSensor,
    MondialRelayOutgoingParcelsSensor,
    MondialRelayParcelSensor,
    _active_barcodes,
    _active_parcels,
)


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(
    data: list[dict],
    delivered: list[dict] | None = None,
    outgoing: list[dict] | None = None,
    delivered_outgoing: list[dict] | None = None,
) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.delivered = delivered if delivered is not None else []
    coordinator.outgoing = outgoing if outgoing is not None else []
    coordinator.delivered_outgoing = (
        delivered_outgoing if delivered_outgoing is not None else []
    )
    return coordinator


def _parcel(
    barcode: str,
    status: ParcelStatus = ParcelStatus.IN_TRANSIT,
    pickup: bool = False,
    planned_from: str | None = None,
) -> dict:
    return {
        "carrier": "Mondial Relay",
        "barcode": barcode,
        "sender": "Sender",
        "receiver": None,
        "status": status,
        "pickup": pickup,
        "planned_from": planned_from,
    }


def test_incoming_counts_and_lists():
    coordinator = _coordinator([_parcel("A"), _parcel("B")])
    sensor = MondialRelayIncomingParcelsSensor(coordinator, _entry(), lambda _: None, set())
    assert sensor.native_value == 2
    assert len(sensor.extra_state_attributes["parcels"]) == 2


def test_parcel_sensor_status_and_attributes():
    parcel = _parcel("A", status=ParcelStatus.OUT_FOR_DELIVERY)
    sensor = MondialRelayParcelSensor(_coordinator([parcel]), _entry(), "A")
    assert sensor.native_value == ParcelStatus.OUT_FOR_DELIVERY
    assert sensor.extra_state_attributes["barcode"] == "A"


def test_parcel_sensor_missing_barcode():
    sensor = MondialRelayParcelSensor(_coordinator([_parcel("A")]), _entry(), "OTHER")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_picks_earliest():
    coordinator = _coordinator([
        _parcel("A", planned_from="2026-05-02T10:00:00Z"),
        _parcel("B", planned_from="2026-05-01T10:00:00Z"),
    ])
    sensor = MondialRelayNextDeliverySensor(coordinator, _entry())
    assert sensor.native_value == datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_next_delivery_none_without_moments():
    sensor = MondialRelayNextDeliverySensor(_coordinator([_parcel("A")]), _entry())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_next_delivery_skips_unparseable_moment():
    coordinator = _coordinator([
        _parcel("A", planned_from="not-a-date"),
        _parcel("B", planned_from="2026-05-01T10:00:00Z"),
    ])
    sensor = MondialRelayNextDeliverySensor(coordinator, _entry())
    assert sensor.extra_state_attributes["barcode"] == "B"


def test_en_route_and_awaiting_pickup_split():
    parcels = [
        _parcel("EN_ROUTE", pickup=True),
        _parcel("READY", pickup=True, status=ParcelStatus.AT_PICKUP_POINT),
        _parcel("HOME"),
    ]
    en_route = MondialRelayEnRouteToPickupPointSensor(_coordinator(parcels), _entry())
    awaiting = MondialRelayAwaitingPickupSensor(_coordinator(parcels), _entry())
    assert en_route.unique_id == "e1_en_route_to_pickup_point"
    assert awaiting.unique_id == "e1_awaiting_pickup"
    assert [p["barcode"] for p in en_route.extra_state_attributes["parcels"]] == ["EN_ROUTE"]
    assert [p["barcode"] for p in awaiting.extra_state_attributes["parcels"]] == ["READY"]
    assert en_route.native_value == 1
    assert awaiting.native_value == 1


def test_pickup_sensors_zero_without_data():
    coordinator = _coordinator([])
    coordinator.data = None
    assert MondialRelayEnRouteToPickupPointSensor(coordinator, _entry()).native_value == 0
    assert MondialRelayAwaitingPickupSensor(coordinator, _entry()).native_value == 0


def test_delivered_sensor():
    coordinator = _coordinator([], delivered=[_parcel("D", status=ParcelStatus.DELIVERED)])
    sensor = MondialRelayDeliveredParcelsSensor(coordinator, _entry())
    assert sensor.native_value == 1
    assert sensor.extra_state_attributes["parcels"][0]["barcode"] == "D"


def test_last_update_sensor():
    coordinator = _coordinator([])
    moment = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    coordinator.last_success_time = moment
    sensor = MondialRelayLastUpdateSensor(coordinator, _entry())
    assert sensor.native_value == moment


def test_outgoing_sensors():
    coordinator = _coordinator(
        [], outgoing=[_parcel("O1")], delivered_outgoing=[_parcel("O2")]
    )
    active = MondialRelayOutgoingParcelsSensor(coordinator, _entry())
    delivered = MondialRelayOutgoingDeliveredSensor(coordinator, _entry())
    assert active.unique_id == "e1_outgoing_parcels"
    assert delivered.unique_id == "e1_outgoing_delivered_parcels"
    assert active.native_value == 1
    assert delivered.native_value == 1
    assert active.extra_state_attributes["parcels"][0]["barcode"] == "O1"
    assert delivered.extra_state_attributes["parcels"][0]["barcode"] == "O2"


def test_active_parcels_spans_both_directions():
    coordinator = _coordinator([_parcel("IN1")], outgoing=[_parcel("OUT1")])
    assert _active_barcodes(coordinator) == {"IN1", "OUT1"}
    assert len(_active_parcels(coordinator)) == 2


def test_parcel_sensor_finds_an_outgoing_barcode():
    coordinator = _coordinator([], outgoing=[_parcel("OUT1")])
    sensor = MondialRelayParcelSensor(coordinator, _entry(), "OUT1")
    assert sensor.native_value is not None
