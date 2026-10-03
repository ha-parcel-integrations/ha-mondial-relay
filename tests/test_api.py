"""Tests for the Mondial Relay BFF client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.mondial_relay.api import (
    MondialRelayApiClient,
    MondialRelayApiError,
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
    account_type,
    has_confirmed_phone,
)
from custom_components.mondial_relay.const import USER_AGENT
from custom_components.mondial_relay.oauth import MondialRelayOAuthError

from .payloads import active_item, list_envelope

DEVICE_UID = "device-abc"
USER_INFO = {
    "guid": "g-1",
    "ucPersonId": "p-1",
    "userType": "PARTICULAR",
    "phone": {"number": "+32…", "valid": True},
}


def _response(status: int, body: object = None, headers: dict | None = None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    return response


def _ctx(response: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_returning(*responses: MagicMock) -> MagicMock:
    """An HTTP session whose ``.get()`` yields the given responses in order."""
    session = MagicMock()
    session.get = MagicMock(side_effect=[_ctx(r) for r in responses])
    return session


def _oauth(token: str = "token-1") -> AsyncMock:
    oauth = AsyncMock()
    oauth.async_get_access_token = AsyncMock(return_value=token)
    oauth.async_handle_unauthorized = AsyncMock(return_value="token-2")
    return oauth


def _client(session: MagicMock, oauth: AsyncMock | None = None) -> MondialRelayApiClient:
    return MondialRelayApiClient(oauth or _oauth(), session, device_uid=DEVICE_UID)


# ---------------------------------------------------------------------------
# signing headers
# ---------------------------------------------------------------------------


async def test_every_request_carries_a_non_empty_signature_and_bearer():
    session = _session_returning(_response(200, list_envelope([active_item()])))
    oauth = _oauth("bearer-xyz")
    await _client(session, oauth).async_get_list_received()

    headers = session.get.call_args_list[0].kwargs["headers"]
    assert headers["Authorization"] == "Bearer bearer-xyz"
    assert headers["X-MR-API-KEY"]
    assert headers["X-MR-Param1"]
    assert headers["X-MR-Param2"]
    assert headers["device-uid"] == DEVICE_UID
    assert headers["User-Agent"] == USER_AGENT


async def test_signature_headers_are_fresh_per_request():
    session = _session_returning(
        _response(200, list_envelope([], page_index=0, total_pages=2)),
        _response(200, list_envelope([], page_index=1, total_pages=2)),
    )
    await _client(session).async_get_list_received()

    first = session.get.call_args_list[0].kwargs["headers"]
    second = session.get.call_args_list[1].kwargs["headers"]
    assert first["X-MR-Param1"] != second["X-MR-Param1"]


# ---------------------------------------------------------------------------
# response table
# ---------------------------------------------------------------------------


async def test_200_list_is_returned():
    session = _session_returning(_response(200, list_envelope([active_item()])))
    items = await _client(session).async_get_list_received()
    assert len(items) == 1
    assert items[0]["expedition"]["shipmentUid"]


async def test_paginates_while_more_pages_remain():
    session = _session_returning(
        _response(200, list_envelope([active_item("uid-1")], page_index=0, total_pages=2)),
        _response(200, list_envelope([active_item("uid-2")], page_index=1, total_pages=2)),
    )
    items = await _client(session).async_get_list_received()
    assert [i["expedition"]["shipmentUid"] for i in items] == ["uid-1", "uid-2"]


async def test_401_refreshes_once_then_retries():
    session = _session_returning(
        _response(401, {}),
        _response(200, list_envelope([active_item()])),
    )
    oauth = _oauth()
    items = await _client(session, oauth).async_get_list_received()
    oauth.async_handle_unauthorized.assert_awaited_once()
    assert len(items) == 1


async def test_401_twice_raises_auth_error():
    session = _session_returning(_response(401, {}), _response(401, {}))
    with pytest.raises(MondialRelayAuthError):
        await _client(session).async_get_list_received()


async def test_401_raises_auth_error_when_refresh_itself_fails():
    session = _session_returning(_response(401, {}))
    oauth = _oauth()
    oauth.async_handle_unauthorized = AsyncMock(
        side_effect=MondialRelayOAuthError("refresh failed")
    )
    with pytest.raises(MondialRelayAuthError):
        await _client(session, oauth).async_get_list_received()


async def test_403_raises_signing_rejected_not_auth_error():
    session = _session_returning(_response(403, {}))
    with pytest.raises(MondialRelaySigningRejectedError) as err:
        await _client(session).async_get_list_received()
    assert not isinstance(err.value, MondialRelayAuthError)


async def test_429_raises_with_retry_after():
    session = _session_returning(_response(429, {}, headers={"Retry-After": "30"}))
    with pytest.raises(MondialRelayApiError) as err:
        await _client(session).async_get_list_received()
    assert err.value.status_code == 429
    assert err.value.retry_after == 30


async def test_429_without_retry_after_header():
    session = _session_returning(_response(429, {}))
    with pytest.raises(MondialRelayApiError) as err:
        await _client(session).async_get_list_received()
    assert err.value.retry_after is None


async def test_other_error_status_raises_api_error():
    session = _session_returning(_response(500, {}))
    with pytest.raises(MondialRelayApiError) as err:
        await _client(session).async_get_list_received()
    assert err.value.status_code == 500


async def test_unparseable_body_raises():
    session = _session_returning(_response(200, "not json"))
    with pytest.raises(MondialRelayApiError):
        await _client(session).async_get_list_received()


async def test_missing_list_envelope_raises():
    session = _session_returning(_response(200, {"totalPages": 1}))
    with pytest.raises(MondialRelayApiError):
        await _client(session).async_get_list_received()


async def test_network_error_propagates():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await _client(session).async_get_list_received()


# ---------------------------------------------------------------------------
# shipped list / validate
# ---------------------------------------------------------------------------


async def test_get_list_shipped_returns_list():
    session = _session_returning(_response(200, list_envelope([active_item()])))
    items = await _client(session).async_get_list_shipped()
    assert len(items) == 1


async def test_validate_parcel_access_is_a_single_bounded_call():
    session = _session_returning(_response(200, list_envelope([], page_index=0, total_pages=5)))
    await _client(session).async_validate_parcel_access()
    assert session.get.call_count == 1


async def test_get_user_info_returns_the_record():
    session = _session_returning(_response(200, USER_INFO))
    info = await _client(session).async_get_user_info()
    assert info["userType"] == "PARTICULAR"


async def test_get_user_info_rejects_a_non_object_body():
    session = _session_returning(_response(200, ["not", "an", "object"]))
    with pytest.raises(MondialRelayApiError):
        await _client(session).async_get_user_info()


async def test_get_user_info_maps_401_to_auth_error():
    session = _session_returning(_response(401), _response(401))
    with pytest.raises(MondialRelayAuthError):
        await _client(session).async_get_user_info()


@pytest.mark.parametrize(
    "phone,expected",
    [
        ({"number": "+32…", "valid": True}, True),
        ({"number": "+32…", "valid": False}, False),
        # An unexpected shape must never block a setup that would work.
        ({"number": "+32…"}, True),
        (None, True),
    ],
)
def test_has_confirmed_phone(phone, expected):
    assert has_confirmed_phone({"phone": phone} if phone else {}) is expected


def test_account_type_reads_user_type():
    assert account_type({"userType": "PRO"}) == "PRO"
    assert account_type({}) is None
