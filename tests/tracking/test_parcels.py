"""Tests for the tracking source's pure parcel-mapping helpers."""

import logging

import pytest

from custom_components.mondial_relay.account.parcels import (
    normalize_parcel as normalize_account_parcel,
)
from custom_components.mondial_relay.const import (
    CAPABILITIES_BY_VARIANT,
    HISTORY_MAX_EVENTS,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES_BY_VARIANT,
    TRACKING_PAGE_BE_FR,
    TRACKING_PAGE_BE_NL,
    TRACKING_PAGE_FR,
    TRACKING_PAGE_NL,
    ParcelStatus,
)
from custom_components.mondial_relay.tracking.parcels import (
    build_history,
    local_timestamp,
    normalize_parcel,
    tracking_page,
    tracking_url,
)
from tests.payloads import active_item

from .payloads import at_step, available, delivered, in_distribution

PAGE = TRACKING_PAGE_BE_NL


def _normalize(expedition: dict, **kwargs) -> dict:
    return normalize_parcel(expedition, page=PAGE, **kwargs)


# ---------------------------------------------------------------------------
# contract
# ---------------------------------------------------------------------------


def test_normalize_publishes_exactly_the_canonical_keys():
    assert set(_normalize(available())) == set(normalize_account_parcel(active_item()))


def test_capabilities_per_variant_are_known_values():
    for variant in ("Account", "Tracking"):
        assert CAPABILITIES_BY_VARIANT[variant] <= KNOWN_CAPABILITIES
        assert PENDING_CAPABILITIES_BY_VARIANT.get(variant, frozenset()) <= (
            KNOWN_CAPABILITIES
        )


def test_tracking_capabilities_match_what_the_normaliser_fills():
    parcel = _normalize(delivered(), include_history=True)
    assert CAPABILITIES_BY_VARIANT["Tracking"] == {
        "delivery_window",
        "pickup_point",
        "url",
        "history",
    }
    active = _normalize(available())
    assert active["planned_from"] is not None
    assert active["planned_to"] is not None
    assert parcel["pickup_point"] is not None
    assert parcel["url"] is not None
    assert parcel["history"]
    assert parcel["weight"] is None
    assert parcel["dimensions"] is None


def test_raw_is_the_full_untrimmed_expedition():
    expedition = available()
    assert _normalize(expedition)["raw"] is expedition


def test_barcode_is_the_shipment_number_and_sender_the_merchant_code():
    parcel = _normalize(available())
    assert parcel["carrier"] == "Mondial Relay"
    assert parcel["barcode"] == "12345678"
    assert parcel["sender"] == "ZZTESTNL"
    assert parcel["receiver"] is None


@pytest.mark.parametrize("merchant", [None, "", "  ", 42])
def test_no_usable_merchant_code_means_no_sender(merchant):
    expedition = available()
    expedition["Enseigne"] = merchant
    assert _normalize(expedition)["sender"] is None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (1, ParcelStatus.REGISTERED),
        (2, ParcelStatus.IN_TRANSIT),
        (3, ParcelStatus.IN_TRANSIT),
        (4, ParcelStatus.AT_PICKUP_POINT),
        (5, ParcelStatus.DELIVERED),
    ],
)
def test_status_follows_the_highest_reached_step(step, expected, caplog):
    parcel = _normalize(at_step(step))
    assert parcel["status"] is expected
    assert parcel["raw_status"] == str(step)
    assert parcel["delivered"] is (expected is ParcelStatus.DELIVERED)
    assert "WARNING" not in caplog.text


def test_a_step_without_an_event_is_not_reached():
    assert _normalize(in_distribution())["status"] is ParcelStatus.IN_TRANSIT


def test_step_4_with_another_delivery_mode_is_in_transit_and_warns_once(caplog):
    expedition = available()
    expedition["DeliveryMode"] = "HOM"
    with caplog.at_level(logging.WARNING):
        first = _normalize(expedition)
        _normalize(expedition)
    assert first["status"] is ParcelStatus.IN_TRANSIT
    assert first["pickup"] is False
    assert caplog.text.count("DeliveryMode=HOM") == 1


def test_a_pickup_point_delivery_sets_pickup():
    assert _normalize(available())["pickup"] is True


def test_no_reached_step_is_unknown_and_warns_once(caplog):
    expedition = available()
    expedition["SuiviParEtapes"] = {}
    with caplog.at_level(logging.WARNING):
        first = _normalize(expedition)
        _normalize(expedition)
    assert first["status"] is ParcelStatus.UNKNOWN
    assert first["raw_status"] is None
    assert caplog.text.count("step=none") == 1


def test_an_unknown_step_alone_is_unknown_and_warns(caplog):
    expedition = available()
    expedition["SuiviParEtapes"] = {"7": {"Evenement": {"Date": "2030-01-07"}}}
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(expedition)
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["raw_status"] == "7"
    assert "step=7" in caplog.text


def test_an_unknown_step_falls_back_to_the_highest_known_one(caplog):
    expedition = at_step(3)
    expedition["SuiviParEtapes"]["7"] = {"Evenement": {"Date": "2030-01-07"}}
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(expedition)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert parcel["raw_status"] == "7"
    assert "step=7" in caplog.text


def test_a_non_numeric_step_key_is_ignored_and_warns(caplog):
    expedition = at_step(2)
    expedition["SuiviParEtapes"]["x"] = {"Evenement": {"Date": "2030-01-07"}}
    with caplog.at_level(logging.WARNING):
        parcel = _normalize(expedition)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert "step=x" in caplog.text


def test_a_never_found_placeholder_is_unknown_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        parcel = _normalize({"Numero": "87654321"})
    assert parcel["status"] is ParcelStatus.UNKNOWN
    assert parcel["barcode"] == "87654321"
    assert parcel["url"] is None
    assert parcel["pickup"] is False
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# delivered, window, pickup point
# ---------------------------------------------------------------------------


def test_delivered_at_is_step_5s_date_in_local_time():
    parcel = _normalize(delivered())
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] == "2030-01-06T14:43:23.783000+01:00"


def test_a_delivered_step_without_a_dated_event_has_no_delivered_at():
    expedition = delivered()
    expedition["SuiviParEtapes"]["5"]["Evenement"] = True
    parcel = _normalize(expedition)
    assert parcel["delivered"] is True
    assert parcel["delivered_at"] is None


def test_an_active_parcel_has_no_delivered_at():
    assert _normalize(available())["delivered_at"] is None


def test_the_estimate_becomes_a_whole_local_day():
    parcel = _normalize(available())
    assert parcel["planned_from"] == "2030-01-05T00:00:00+01:00"
    assert parcel["planned_to"] == "2030-01-05T23:59:59+01:00"


@pytest.mark.parametrize("estimate", ["0001-01-01T00:00:00", "garbage", None, 5])
def test_a_missing_or_placeholder_estimate_has_no_window(estimate):
    expedition = available()
    expedition["EstimatedDeliveryDate"] = estimate
    parcel = _normalize(expedition)
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_the_estimate_is_read_as_utc_before_its_local_day_is_taken():
    expedition = available()
    # 23:30 UTC is already the next day in local time.
    expedition["EstimatedDeliveryDate"] = "2030-01-05T23:30:00"
    assert _normalize(expedition)["planned_from"] == "2030-01-06T00:00:00+01:00"


def test_a_delivered_parcel_has_no_window():
    parcel = _normalize(delivered())
    assert parcel["planned_from"] is None
    assert parcel["planned_to"] is None


def test_the_step_3_variant_has_no_window():
    assert _normalize(in_distribution())["planned_from"] is None


def test_the_collection_event_names_the_pickup_point():
    assert _normalize(delivered())["pickup_point"] == "OTHER TEST SHOP"


def test_the_drop_off_point_never_becomes_the_pickup_point():
    # Ready at the pickup point, but only the drop-off event names a place.
    assert _normalize(available())["pickup_point"] is None


def test_a_ready_event_that_names_its_place_gives_the_pickup_point():
    expedition = available()
    ready_date = expedition["SuiviParEtapes"]["4"]["Evenement"]["Date"]
    for event in expedition["Evenements"]:
        if event["Date"] == ready_date:
            event["DetailPointRelais"] = {"Adresse": {"Libelle": " READY SHOP "}}
    assert _normalize(expedition)["pickup_point"] == "READY SHOP"


def test_no_pickup_point_outside_a_pickup_point_delivery():
    expedition = delivered()
    expedition["DeliveryMode"] = "HOM"
    assert _normalize(expedition)["pickup_point"] is None


@pytest.mark.parametrize(
    "relay",
    [None, "x", {"Adresse": None}, {"Adresse": {"Libelle": "  "}}, {"Adresse": {}}],
)
def test_an_unusable_relay_point_gives_no_pickup_point(relay):
    expedition = delivered()
    expedition["Evenements"][-1]["DetailPointRelais"] = relay
    assert _normalize(expedition)["pickup_point"] is None


def test_odd_steps_or_events_give_no_pickup_point():
    expedition = delivered()
    expedition["Evenements"] = "not a list"
    assert _normalize(expedition)["pickup_point"] is None
    expedition = delivered()
    expedition["Evenements"].insert(0, "not an event")
    assert _normalize(expedition)["pickup_point"] == "OTHER TEST SHOP"


# ---------------------------------------------------------------------------
# timestamps and history
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2030-01-01T10:00:00.123", "2030-01-01T10:00:00.123000+01:00"),
        ("2030-01-02T15:00:00.45", "2030-01-02T15:00:00.450000+01:00"),
        ("2030-01-03T02:00:00.001", "2030-01-03T02:00:00.001000+01:00"),
        ("2030-01-04T08:00:00", "2030-01-04T08:00:00+01:00"),
        ("2030-07-04T08:00:00", "2030-07-04T08:00:00+02:00"),
        ("2030-01-04T08:00:00+00:00", "2030-01-04T08:00:00+00:00"),
        ("not-a-date", None),
        ("", None),
        (None, None),
    ],
)
def test_local_timestamp_handles_every_fraction_width(value, expected):
    assert local_timestamp(value) == expected


def test_history_is_opt_in():
    assert _normalize(available())["history"] is None


def test_history_is_oldest_first_with_trimmed_text():
    history = _normalize(available(), include_history=True)["history"]
    assert [entry["timestamp"] for entry in history] == sorted(
        entry["timestamp"] for entry in history
    )
    assert history[0] == {
        "timestamp": "2030-01-01T10:00:00.123000+01:00",
        "status": None,
        "raw_status": "Pakje in voorbereiding bij de verzender",
    }
    assert len(history) == 5


def test_history_skips_odd_events_and_keeps_the_newest():
    events = [{"Date": f"2030-01-01T10:{minute:02d}:00"} for minute in range(30)]
    events += ["not an event", {"Date": "garbage", "Libelle": "x"}]
    history = build_history(events)
    assert len(history) == HISTORY_MAX_EVENTS
    assert history[-1]["timestamp"] == "2030-01-01T10:29:00+01:00"
    assert history[-1]["raw_status"] is None
    assert build_history(None) == []


# ---------------------------------------------------------------------------
# url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("country", "language", "expected"),
    [
        ("NL", "nl", TRACKING_PAGE_NL),
        ("NL", "fr", TRACKING_PAGE_NL),
        ("BE", "fr", TRACKING_PAGE_BE_FR),
        ("BE", "fr-BE", TRACKING_PAGE_BE_FR),
        ("BE", "nl", TRACKING_PAGE_BE_NL),
        ("BE", "en", TRACKING_PAGE_BE_NL),
        ("FR", "en", TRACKING_PAGE_FR),
        ("ES", "fr", TRACKING_PAGE_BE_FR),
        ("PT", "pt", TRACKING_PAGE_BE_NL),
        (None, None, TRACKING_PAGE_BE_NL),
    ],
)
def test_tracking_page_follows_home_assistants_country_and_language(
    country, language, expected
):
    assert tracking_page(country, language) == expected


def test_url_is_built_from_the_responses_own_number_and_brand():
    assert _normalize(available())["url"] == (
        f"{PAGE}?numeroExpedition=12345678&codeMarque=ZZ"
    )


def test_url_carries_no_postcode():
    assert "9999" not in _normalize(available())["url"]


@pytest.mark.parametrize("brand", [None, "", "Z", 12])
def test_url_needs_a_two_character_brand(brand):
    expedition = available()
    expedition["Enseigne"] = brand
    assert tracking_url(expedition, PAGE) is None


def test_url_needs_a_number():
    expedition = available()
    del expedition["Numero"]
    assert tracking_url(expedition, PAGE) is None
