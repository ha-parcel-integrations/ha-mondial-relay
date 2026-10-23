"""Tests for the public tracking client."""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mondial_relay.const import TRACKING_USER_AGENT
from custom_components.mondial_relay.tracking.api import (
    MondialRelayTrackingClient,
    MondialRelayTrackingError,
    MondialRelayTrackingRefusedError,
)

from .payloads import (
    INVALID_REQUEST,
    NOT_FOUND,
    POSTAL_CODE,
    TOKEN,
    TRACKING_CODE,
    TRACKING_PAGE,
    TRACKING_PAGE_WITHOUT_TOKEN,
    available,
    found,
)


def _response(
    status: int = 200,
    *,
    body: object = None,
    html: str | None = None,
    headers: dict | None = None,
) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.text = AsyncMock(return_value=html or "")
    if html is not None:
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", html, 0))
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _page(status: int = 200, html: str = TRACKING_PAGE, **kwargs) -> MagicMock:
    return _response(status, html=html, **kwargs)


def _ctx(response: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session(*responses: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(side_effect=[_ctx(r) for r in responses])
    session.close = AsyncMock()
    return session


def _client(
    session: MagicMock, language: str | None = "nl"
) -> MondialRelayTrackingClient:
    return MondialRelayTrackingClient(lambda: session, language)


async def _lookup(session: MagicMock, language: str | None = "nl"):
    async with _client(session, language).async_cycle() as cycle:
        return await cycle.async_get_shipment(TRACKING_CODE, POSTAL_CODE)


# ---------------------------------------------------------------------------
# the tracking page
# ---------------------------------------------------------------------------


async def test_the_page_value_is_read_past_other_attributes():
    session = _session(_page(), _response(body=found()))
    await _lookup(session)
    headers = session.get.call_args_list[1].kwargs["headers"]
    assert headers["RequestVerificationToken"] == TOKEN


async def test_a_page_without_its_value_is_a_refusal():
    session = _session(_page(html=TRACKING_PAGE_WITHOUT_TOKEN))
    with pytest.raises(MondialRelayTrackingRefusedError):
        await _lookup(session)
    session.close.assert_awaited_once()


async def test_a_value_on_another_element_is_not_used():
    html = '<div id="other" token="a:b"></div><div id="tracking"></div>'
    session = _session(_page(html=html))
    with pytest.raises(MondialRelayTrackingRefusedError):
        await _lookup(session)


async def test_a_refused_page_is_a_refusal():
    session = _session(_page(403))
    with pytest.raises(MondialRelayTrackingRefusedError) as err:
        await _lookup(session)
    assert err.value.status_code == 403
    session.close.assert_awaited_once()


async def test_a_rate_limited_page_carries_retry_after():
    session = _session(_page(429, headers={"Retry-After": "120"}))
    with pytest.raises(MondialRelayTrackingError) as err:
        await _lookup(session)
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


async def test_the_page_is_asked_for_like_a_browser_in_home_assistants_language():
    session = _session(_page(), _response(body=found()))
    await _lookup(session, language="fr")
    page_headers = session.get.call_args_list[0].kwargs["headers"]
    assert page_headers["User-Agent"] == TRACKING_USER_AGENT
    assert page_headers["Accept-Language"] == "fr"


async def test_no_language_sends_no_accept_language():
    session = _session(_page(), _response(body=found()))
    await _lookup(session, language=None)
    assert "Accept-Language" not in session.get.call_args_list[0].kwargs["headers"]


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


async def test_every_lookup_sends_the_hub_postcode_and_an_empty_brand():
    session = _session(_page(), _response(body=found()), _response(body=found()))
    async with _client(session).async_cycle() as cycle:
        await cycle.async_get_shipment(TRACKING_CODE, " 1234 ab")
        await cycle.async_get_shipment("87654321", " 1234 ab")
    for call in session.get.call_args_list[1:]:
        assert call.kwargs["params"]["postcode"] == " 1234 ab"
        assert call.kwargs["params"]["brand"] == ""
        assert call.kwargs["params"]["codePays"] == ""
        headers = call.kwargs["headers"]
        assert headers["RequestVerificationToken"] == TOKEN
        assert headers["Accept"] == "application/json"
        assert headers["Referer"].endswith("/nl-be/shipment-tracking")
        assert headers["User-Agent"] == TRACKING_USER_AGENT
    assert session.get.call_args_list[1].kwargs["params"]["shipment"] == TRACKING_CODE
    session.close.assert_awaited_once()


async def test_a_found_parcel_returns_its_expedition():
    assert await _lookup(_session(_page(), _response(body=found()))) == available()


@pytest.mark.parametrize(
    "body", [NOT_FOUND, INVALID_REQUEST, {"Expedition": None, "status": []}]
)
async def test_a_body_without_expedition_is_not_found(body):
    assert await _lookup(_session(_page(), _response(body=body))) is None


async def test_not_found_is_logged_once_per_cycle_at_debug(caplog):
    session = _session(_page(), _response(body=NOT_FOUND), _response(body=NOT_FOUND))
    with caplog.at_level(logging.DEBUG):
        async with _client(session).async_cycle() as cycle:
            await cycle.async_get_shipment("1", POSTAL_CODE)
            await cycle.async_get_shipment("2", POSTAL_CODE)
    lines = [r for r in caplog.records if "found no parcel" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno == logging.DEBUG


async def test_an_empty_status_list_is_still_not_found():
    assert await _lookup(_session(_page(), _response(body={"status": []}))) is None


async def test_a_200_that_is_not_json_is_a_refusal():
    session = _session(_page(), _response(html="<html>challenge</html>"))
    with pytest.raises(MondialRelayTrackingRefusedError):
        await _lookup(session)


async def test_a_refused_lookup_is_a_refusal():
    with pytest.raises(MondialRelayTrackingRefusedError):
        await _lookup(_session(_page(), _response(403)))


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [({"Retry-After": "30"}, 30.0), ({}, None), ({"Retry-After": "soon"}, None)],
)
async def test_a_rate_limited_lookup_carries_retry_after(headers, retry_after):
    session = _session(_page(), _response(429, headers=headers))
    with pytest.raises(MondialRelayTrackingError) as err:
        await _lookup(session)
    assert err.value.status_code == 429
    assert err.value.retry_after == retry_after


async def test_another_status_is_a_plain_error():
    with pytest.raises(MondialRelayTrackingError) as err:
        await _lookup(_session(_page(), _response(500)))
    assert err.value.status_code == 500
    assert not isinstance(err.value, MondialRelayTrackingRefusedError)


@pytest.mark.parametrize("body", [[1, 2], {"Something": 1}, {"Expedition": "x"}])
async def test_an_unexpected_shape_logs_key_names_only(body, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(MondialRelayTrackingError):
        await _lookup(_session(_page(), _response(body=body)))
    assert "unexpected" in caplog.text
