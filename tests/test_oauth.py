"""Tests for the InPost Group OAuth/PKCE session."""
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.mondial_relay.oauth import (
    MondialRelayOAuthAuthError,
    MondialRelayOAuthError,
    MondialRelayOAuthSession,
    decode_id_token_claims,
    decode_id_token_subject,
    generate_nonce,
    generate_pkce,
    generate_state,
    is_valid_callback_url,
    parse_callback_url,
    sign_in_language,
)


def _jwt(payload: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{part}.sig"


def _response(status: int, body: dict | str) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.text = AsyncMock(return_value=body if isinstance(body, str) else json.dumps(body))
    return response


def _ctx(response) -> MagicMock:
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _session_returning(*responses) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(side_effect=[_ctx(r) for r in responses])
    return session


# ---------------------------------------------------------------------------
# PKCE / URL helpers
# ---------------------------------------------------------------------------


def test_pkce_pair_is_fresh_and_verifiable():
    verifier_a, challenge_a = generate_pkce()
    verifier_b, challenge_b = generate_pkce()
    assert verifier_a != verifier_b
    assert challenge_a != challenge_b
    assert verifier_a and challenge_a


def test_state_and_nonce_are_fresh():
    assert generate_state() != generate_state()
    assert generate_nonce() != generate_nonce()


def test_parse_callback_url_extracts_code_and_state():
    code, state = parse_callback_url(
        " https://account.inpost-group.com/callback?code=abc&state=xyz \n"
    )
    assert code == "abc"
    assert state == "xyz"


def test_parse_callback_url_missing_params():
    code, state = parse_callback_url("https://account.inpost-group.com/callback")
    assert code is None
    assert state is None


def test_is_valid_callback_url_accepts_the_real_host():
    assert is_valid_callback_url(
        "https://account.inpost-group.com/callback?code=abc&state=xyz"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://account.inpost-group.com/callback?code=abc",
        "https://evil.example.com/callback?code=abc",
        "https://account.inpost-group.com/other?code=abc",
    ],
)
def test_is_valid_callback_url_rejects_the_rest(url):
    assert not is_valid_callback_url(url)


def test_decode_id_token_subject():
    token = _jwt({"sub": "abc123"})
    assert decode_id_token_subject(token) == "abc123"


def test_decode_id_token_subject_malformed():
    assert decode_id_token_subject("not-a-jwt") is None
    assert decode_id_token_claims("a.b") is None


def test_decode_id_token_subject_missing_claim():
    assert decode_id_token_subject(_jwt({})) is None


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def test_build_authorization_url_contains_client_and_challenge():
    session = MondialRelayOAuthSession(MagicMock())
    url, verifier, state = session.build_authorization_url()
    assert "mondialrelay-mobile" in url
    assert "code_challenge=" in url
    assert verifier and state


def test_build_authorization_url_scopes_the_page_to_brand_and_market():
    """Without these the provider serves the Polish-only InPost sign-up."""
    session = MondialRelayOAuthSession(MagicMock())
    url, _, _ = session.build_authorization_url(language="nl", market="BE")
    assert "brand=mr" in url
    assert "supported_markets=BE" in url
    assert "lang=nl-BE" in url


@pytest.mark.parametrize(
    "language,market,expected",
    [
        (None, "FR", "fr-FR"),
        ("en", "FR", "fr-FR"),
        # Belgium is bilingual: Home Assistant's own language picks the side.
        ("nl", "BE", "nl-BE"),
        ("fr", "BE", "fr-BE"),
        ("en", "BE", "fr-BE"),
        ("nl", "NL", "nl-NL"),
        # No Spanish or Portuguese sign-in translation exists, but the tag's
        # region still drives the phone step's dial code.
        ("es", "ES", "es-ES"),
        ("en", "PT", "pt-PT"),
        # An unknown market must not produce an unknown language tag.
        ("nl", "XX", "fr-FR"),
    ],
)
def test_sign_in_language(language, market, expected):
    assert sign_in_language(language, market) == expected


async def test_exchange_code_stores_tokens():
    http = _session_returning(
        _response(200, {"access_token": "at-1", "refresh_token": "rt-1", "id_token": _jwt({"sub": "s1"}), "expires_in": 3600})
    )
    session = MondialRelayOAuthSession(http)
    await session.async_exchange_code("code-1", "verifier-1")

    assert session.refresh_token == "rt-1"
    assert await session.async_get_access_token() == "at-1"


async def test_exchange_code_without_refresh_token_raises():
    http = _session_returning(_response(200, {"access_token": "at-1"}))
    session = MondialRelayOAuthSession(http)
    with pytest.raises(MondialRelayOAuthError):
        await session.async_exchange_code("code-1", "verifier-1")


async def test_exchange_code_rejected_raises_auth_error():
    http = _session_returning(_response(400, {"error": "invalid_grant"}))
    session = MondialRelayOAuthSession(http)
    with pytest.raises(MondialRelayOAuthAuthError):
        await session.async_exchange_code("code-1", "verifier-1")


async def test_exchange_code_outage_raises_plain_error():
    http = _session_returning(_response(500, {}))
    session = MondialRelayOAuthSession(http)
    with pytest.raises(MondialRelayOAuthError) as err:
        await session.async_exchange_code("code-1", "verifier-1")
    assert not isinstance(err.value, MondialRelayOAuthAuthError)


async def test_get_access_token_without_refresh_token_raises():
    session = MondialRelayOAuthSession(MagicMock())
    with pytest.raises(MondialRelayOAuthError):
        await session.async_get_access_token()


async def test_get_access_token_refreshes_when_stale():
    http = _session_returning(
        _response(200, {"access_token": "at-2", "refresh_token": "rt-1", "expires_in": 3600})
    )
    session = MondialRelayOAuthSession(http, refresh_token="rt-1")
    token = await session.async_get_access_token()
    assert token == "at-2"


async def test_refresh_token_rotation_is_flagged_and_cleared():
    http = _session_returning(
        _response(200, {"access_token": "at-2", "refresh_token": "rt-2", "expires_in": 3600})
    )
    session = MondialRelayOAuthSession(http, refresh_token="rt-1")
    await session.async_get_access_token()

    assert session.refresh_token == "rt-2"
    assert session.pop_refresh_token_changed() is True
    assert session.pop_refresh_token_changed() is False


async def test_handle_unauthorized_forces_a_refresh():
    http = _session_returning(
        _response(200, {"access_token": "at-3", "refresh_token": "rt-1", "expires_in": 3600})
    )
    session = MondialRelayOAuthSession(http, refresh_token="rt-1")
    assert await session.async_handle_unauthorized() == "at-3"


async def test_handle_unauthorized_without_refresh_token_raises():
    session = MondialRelayOAuthSession(MagicMock())
    with pytest.raises(MondialRelayOAuthError):
        await session.async_handle_unauthorized()


async def test_token_response_with_no_expires_in_uses_fallback():
    http = _session_returning(
        _response(200, {"access_token": "at-1", "refresh_token": "rt-1"})
    )
    session = MondialRelayOAuthSession(http)
    await session.async_exchange_code("code-1", "verifier-1")
    assert await session.async_get_access_token() == "at-1"


async def test_token_endpoint_unparseable_body_falls_back_to_empty_payload():
    http = _session_returning(_response(200, "not json"))
    session = MondialRelayOAuthSession(http)
    with pytest.raises(MondialRelayOAuthError):
        await session.async_exchange_code("code-1", "verifier-1")


async def test_network_error_propagates():
    http = MagicMock()
    http.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    session = MondialRelayOAuthSession(http)
    with pytest.raises(aiohttp.ClientError):
        await session.async_exchange_code("code-1", "verifier-1")
