"""Tests for the request-signing helper.

Only shape is asserted — freshness and non-emptiness — never the derived
value itself.
"""
from custom_components.mondial_relay.signing import build_signature_headers


def test_headers_are_present_and_non_empty():
    headers = build_signature_headers(
        device_uid="device-1", language="en", origin_app="MR"
    )
    assert headers["X-MR-Param1"]
    assert headers["X-MR-Param2"]
    assert headers["X-MR-API-KEY"]
    assert headers["device-uid"] == "device-1"
    assert headers["Accept-Language"] == "en"
    assert headers["X-OriginApp"] == "MR"


def test_param1_is_a_fresh_uuid_each_call():
    first = build_signature_headers(device_uid="d", language="en", origin_app="MR")
    second = build_signature_headers(device_uid="d", language="en", origin_app="MR")
    assert first["X-MR-Param1"] != second["X-MR-Param1"]
    assert first["X-MR-API-KEY"] != second["X-MR-API-KEY"]


def test_param2_is_numeric_epoch_seconds():
    headers = build_signature_headers(device_uid="d", language="en", origin_app="MR")
    assert headers["X-MR-Param2"].isdigit()
