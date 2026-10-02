"""Tests for the Mondial Relay config and options flow."""
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import SOURCE_USER
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.api import (
    MondialRelayApiError,
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
)
from custom_components.mondial_relay.config_flow import MondialRelayConfigFlow
from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE_UID,
    CONF_INCLUDE_HISTORY,
    CONF_MARKET,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)
from custom_components.mondial_relay.oauth import MondialRelayOAuthAuthError

OAUTH_CLASS = "custom_components.mondial_relay.config_flow.MondialRelayOAuthSession"
CLIENT_CLASS = "custom_components.mondial_relay.config_flow.MondialRelayApiClient"
SUBJECT_FN = "custom_components.mondial_relay.config_flow.decode_id_token_subject"

AUTHORIZE_URL = "https://account.inpost-group.com/oauth2/authorize?..."
STATE = "state-1"
VALID_CALLBACK = f"https://account.inpost-group.com/callback?code=abc123&state={STATE}"
WRONG_STATE_CALLBACK = "https://account.inpost-group.com/callback?code=abc123&state=other"
NO_CODE_CALLBACK = f"https://account.inpost-group.com/callback?state={STATE}"
WRONG_HOST_CALLBACK = f"https://evil.example.com/callback?code=abc123&state={STATE}"


def _fake_oauth(*, exchange_side_effect=None) -> MagicMock:
    session = MagicMock()
    session.build_authorization_url = MagicMock(
        return_value=(AUTHORIZE_URL, "verifier", STATE)
    )
    session.async_exchange_code = AsyncMock(side_effect=exchange_side_effect)
    session.refresh_token = "refresh-token-1"
    session.id_token = "header.payload.sig"
    return session


async def _start(hass, country: str = "fr"):
    """Init the flow and answer the country step, landing on ``sign_in``."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COUNTRY: country}
    )


def _fake_client(*, validate_side_effect=None) -> MagicMock:
    client = MagicMock()
    client.async_validate = AsyncMock(side_effect=validate_side_effect)
    return client


def _entry(subject: str = "subject-1") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Mondial Relay",
        unique_id=subject,
        data={
            CONF_REFRESH_TOKEN: "refresh-token-1",
            CONF_ACCOUNT_SUBJECT: subject,
            CONF_DEVICE_UID: "device-1",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "days",
            CONF_DELIVERED_FILTER_AMOUNT: 7,
            CONF_INCLUDE_HISTORY: False,
        },
    )


# ---------------------------------------------------------------------------
# user step
# ---------------------------------------------------------------------------


async def test_user_flow_asks_for_the_country_first(hass):
    """The sign-in link cannot be built before the market is known."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"


async def test_user_flow_shows_authorize_url(hass):
    oauth = _fake_oauth()
    with patch(OAUTH_CLASS, return_value=oauth):
        result = await _start(hass, country="be")
    assert result["step_id"] == "sign_in"
    assert AUTHORIZE_URL in result["description_placeholders"]["authorize_url"]
    # The chosen market reaches the URL builder — without it the identity
    # provider serves the Polish-only InPost sign-up.
    assert oauth.build_authorization_url.call_args.kwargs["market"] == "BE"


async def test_user_flow_creates_entry(hass):
    with (
        patch(OAUTH_CLASS, return_value=_fake_oauth()),
        patch(CLIENT_CLASS, return_value=_fake_client()),
        patch(SUBJECT_FN, return_value="subject-1"),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "Mondial Relay"
    assert result["data"][CONF_REFRESH_TOKEN] == "refresh-token-1"
    assert result["data"][CONF_ACCOUNT_SUBJECT] == "subject-1"
    assert result["data"][CONF_DEVICE_UID]
    assert result["data"][CONF_MARKET] == "FR"


async def test_user_flow_rejects_wrong_state(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": WRONG_STATE_CALLBACK}
    )
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_user_flow_rejects_missing_code(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": NO_CODE_CALLBACK}
    )
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_user_flow_rejects_wrong_host(hass):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"callback_url": WRONG_HOST_CALLBACK}
    )
    assert result["errors"] == {"base": "invalid_redirect"}


async def test_user_flow_surfaces_exchange_auth_error(hass):
    with patch(
        OAUTH_CLASS,
        return_value=_fake_oauth(
            exchange_side_effect=MondialRelayOAuthAuthError("rejected")
        ),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_surfaces_exchange_connection_error(hass):
    with patch(
        OAUTH_CLASS,
        return_value=_fake_oauth(exchange_side_effect=aiohttp.ClientError("boom")),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )
    assert result["errors"] == {"base": "cannot_connect"}


@pytest.mark.parametrize(
    "validate_error,expected",
    [
        # Not invalid_auth: the sign-in itself worked, so a different
        # callback URL cannot fix it.
        (MondialRelayAuthError("HTTP 401"), "account_rejected"),
        (MondialRelaySigningRejectedError("HTTP 403"), "cannot_connect"),
        (MondialRelayApiError("HTTP 500"), "cannot_connect"),
        (aiohttp.ClientError("boom"), "cannot_connect"),
    ],
)
async def test_user_flow_surfaces_validation_errors(hass, validate_error, expected):
    with (
        patch(OAUTH_CLASS, return_value=_fake_oauth()),
        patch(CLIENT_CLASS, return_value=_fake_client(validate_side_effect=validate_error)),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )
    assert result["errors"] == {"base": expected}


async def test_country_step_rebuilds_the_link_when_the_answer_changes(hass):
    """A link built for the previous answer points at the wrong market."""
    flow = MondialRelayConfigFlow()
    flow.hass = hass
    flow.flow_id = "flow-1"
    flow.handler = DOMAIN
    oauth = _fake_oauth()
    with patch(OAUTH_CLASS, return_value=oauth):
        await flow.async_step_user({CONF_COUNTRY: "fr"})
        await flow.async_step_user({CONF_COUNTRY: "be"})
    markets = [
        call.kwargs["market"] for call in oauth.build_authorization_url.call_args_list
    ]
    assert markets == ["FR", "BE"]


async def test_user_flow_aborts_on_duplicate_account(hass):
    _entry().add_to_hass(hass)

    with (
        patch(OAUTH_CLASS, return_value=_fake_oauth()),
        patch(CLIENT_CLASS, return_value=_fake_client()),
        patch(SUBJECT_FN, return_value="subject-1"),
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# reauth
# ---------------------------------------------------------------------------


async def test_reauth_updates_the_refresh_token(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with (
        patch(OAUTH_CLASS, return_value=_fake_oauth()),
        patch(CLIENT_CLASS, return_value=_fake_client()),
        patch(SUBJECT_FN, return_value="subject-1"),
    ):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "refresh-token-1"
    # The device id must never change on reauth.
    assert entry.data[CONF_DEVICE_UID] == "device-1"


async def test_reauth_rejects_a_different_account(hass):
    """Signing in as another account must not silently rebind this entry."""
    entry = _entry(subject="subject-1")
    entry.add_to_hass(hass)

    with (
        patch(OAUTH_CLASS, return_value=_fake_oauth()),
        patch(CLIENT_CLASS, return_value=_fake_client()),
        patch(SUBJECT_FN, return_value="subject-2"),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCOUNT_SUBJECT] == "subject-1"


async def test_reauth_surfaces_invalid_credentials(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    with patch(
        OAUTH_CLASS,
        return_value=_fake_oauth(
            exchange_side_effect=MondialRelayOAuthAuthError("rejected")
        ),
    ):
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"callback_url": VALID_CALLBACK}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_auth"}


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


async def test_options_flow_saves_and_reloads(hass):
    entry = _entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "delivered": {
                    CONF_DELIVERED_FILTER_TYPE: "parcels",
                    CONF_DELIVERED_FILTER_AMOUNT: 5,
                },
                "history": {CONF_INCLUDE_HISTORY: True},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_DELIVERED_FILTER_TYPE: "parcels",
        CONF_DELIVERED_FILTER_AMOUNT: 5,
        CONF_INCLUDE_HISTORY: True,
    }
    schedule_reload.assert_called_once_with(entry.entry_id)
