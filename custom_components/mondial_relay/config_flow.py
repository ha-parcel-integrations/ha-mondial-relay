"""Config flow for the Mondial Relay parcel tracker integration.

Two kinds of hub. A tracking hub is one postcode: parcels addressed to it are
followed by their tracking code alone, and setting it up makes no network
call. An account hub signs in to a Mondial Relay account. The account backend's login has no redirect Home Assistant can catch — the
official app's own redirect is a native deep link, not a URL a server-side
integration can receive. So this flow builds its own authorization URL with a
freshly generated PKCE verifier/state, the user opens it, logs in with their
InPost Group account, and pastes back the callback URL the browser lands on
(which it cannot open). Modelled on ha-dhl's DE browser-paste PKCE flow — same
shape, different provider.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import secrets
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .account.api import (
    MondialRelayApiClient,
    MondialRelayApiError,
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
    account_type,
    has_confirmed_phone,
)
from .account.oauth import (
    MondialRelayOAuthAuthError,
    MondialRelayOAuthError,
    MondialRelayOAuthSession,
    decode_id_token_subject,
    is_valid_callback_url,
    parse_callback_url,
)
from .const import (
    ACCOUNT_MARKETS,
    ACCOUNT_WEBSITES,
    CONF_ACCOUNT_SUBJECT,
    CONF_ACCOUNT_TYPE,
    CONF_COUNTRY,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_DEVICE_UID,
    CONF_INCLUDE_HISTORY,
    CONF_MARKET,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DEFAULT_ACCOUNT_MARKET,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .source import is_tracking_entry

_LOGGER = logging.getLogger(__name__)

_CALLBACK_SCHEMA = vol.Schema({vol.Required("callback_url"): str})
_MARKET_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[market.lower() for market in ACCOUNT_MARKETS],
        translation_key=CONF_COUNTRY,
        mode=selector.SelectSelectorMode.DROPDOWN,
        # By the translated country name, so the order is alphabetical in
        # every language.
        sort=True,
    )
)


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code trimmed, nothing else."""
    return (value or "").strip()


def valid_tracking_code(value: str) -> bool:
    """Whether ``value`` is a non-empty tracking code — no format regex."""
    return bool(value)


def normalize_postal_code(value: str) -> str:
    """Return the postcode trimmed and otherwise verbatim.

    No pattern and no case or spacing rewrite: a postcode that does not
    match the label just comes back "not found" per parcel, which is a far
    cheaper failure than turning a valid notation away.
    """
    return (value or "").strip()


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class MondialRelayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the browser-paste OAuth/PKCE flow for the Mondial Relay integration."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise per-flow state — never persisted, never reused."""
        self._oauth: MondialRelayOAuthSession | None = None
        self._authorize_url: str | None = None
        self._code_verifier: str | None = None
        self._state: str | None = None
        self._device_uid: str | None = None
        self._market: str = DEFAULT_ACCOUNT_MARKET
        self._account_type: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> MondialRelayOptionsFlowHandler:
        """Return the options flow handler."""
        return MondialRelayOptionsFlowHandler()

    def _get_oauth(self) -> MondialRelayOAuthSession:
        """Return this flow's one OAuth session, creating it on first use."""
        if self._oauth is None:
            self._oauth = MondialRelayOAuthSession(async_get_clientsession(self.hass))
        return self._oauth

    def _ensure_authorize_url(self) -> None:
        """Build the authorization URL once per flow and hold it for its lifetime.

        Regenerating the verifier/state on a retry would invalidate a URL the
        user may have already opened.
        """
        if self._authorize_url is not None:
            return
        (
            self._authorize_url,
            self._code_verifier,
            self._state,
        ) = self._get_oauth().build_authorization_url(
            language=self.hass.config.language, market=self._market
        )

    async def _async_exchange_and_validate(self, callback_url: str) -> str | None:
        """Parse, exchange and validate a pasted callback URL.

        Returns an error code for the form, or ``None`` on success. Validates
        HTTPS host, exact path and state before ever exchanging the code —
        the code and verifier are discarded either way once this returns.
        """
        if not is_valid_callback_url(callback_url):
            return "invalid_redirect"
        code, state = parse_callback_url(callback_url)
        if not code or state != self._state:
            return "invalid_redirect"

        oauth = self._get_oauth()
        try:
            await oauth.async_exchange_code(code, self._code_verifier or "")
        except MondialRelayOAuthAuthError as err:
            # Logged, and reported separately from the account-backend
            # refusal below: one shared message leaves a bug report unable to
            # say which side refused.
            _LOGGER.warning(
                "The identity provider rejected the pasted sign-in (%s)",
                err.error_code or err.status_code or "no error code",
            )
            return "invalid_auth"
        except (MondialRelayOAuthError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("Failed to exchange the pasted callback URL", exc_info=True)
            return "cannot_connect"

        self._device_uid = self._device_uid or secrets.token_hex(16)
        client = MondialRelayApiClient(
            oauth, async_get_clientsession(self.hass), device_uid=self._device_uid
        )

        # Identity first, parcel feed second. Asked in this order because a
        # token the backend refuses outright and a token it accepts while
        # refusing the parcel feed need different things from the user, and
        # one combined call cannot tell them apart.
        try:
            user_info = await client.async_get_user_info()
        except MondialRelayAuthError:
            _LOGGER.warning(
                "Signing in succeeded, but the Mondial Relay account backend "
                "rejected the account's own token with HTTP 401"
            )
            return "account_rejected"
        except MondialRelaySigningRejectedError:
            # The user did everything right; this integration's own request
            # was rejected. Not something a different callback URL fixes.
            _LOGGER.debug("The user-infos call was rejected with HTTP 403")
            return "cannot_connect"
        except (MondialRelayApiError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("The user-infos call failed", exc_info=True)
            return "cannot_connect"

        self._account_type = account_type(user_info)

        try:
            await client.async_validate_parcel_access()
        except MondialRelayAuthError:
            # An unconfirmed phone number explains this refusal, so it is
            # reported here rather than as a gate of its own: that the
            # backend actually requires a confirmed number is an inference,
            # and blocking on it would turn a working account away.
            if not has_confirmed_phone(user_info):
                _LOGGER.warning(
                    "The parcel list was refused with HTTP 401 and this "
                    "account's phone number is not confirmed, which Mondial "
                    "Relay's own app asks for before the account can be used"
                )
                return "phone_not_confirmed"
            _LOGGER.warning(
                "The account itself is valid (user-infos returned a record) "
                "and its phone number is confirmed, but its parcel list was "
                "refused with HTTP 401"
            )
            return "parcels_unavailable"
        except MondialRelaySigningRejectedError:
            _LOGGER.debug("The parcel-list call was rejected with HTTP 403")
            return "cannot_connect"
        except (MondialRelayApiError, aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("The parcel-list call failed", exc_info=True)
            return "cannot_connect"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which kind of hub to set up."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create a tracking hub for one postcode, without a network call."""
        errors: dict[str, str] = {}
        if user_input is not None:
            postal_code = normalize_postal_code(user_input[CONF_POSTAL_CODE])
            if not postal_code:
                errors[CONF_POSTAL_CODE] = "invalid_postcode"
            else:
                # Prefixed so it can never collide with an account's subject.
                await self.async_set_unique_id(f"{SOURCE_TRACKING}:{postal_code}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Mondial Relay ({postal_code})",
                    data={CONF_SOURCE: SOURCE_TRACKING},
                    options={
                        CONF_POSTAL_CODE: postal_code,
                        CONF_PARCELS: [],
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )
        return self.async_show_form(
            step_id=SOURCE_TRACKING,
            data_schema=vol.Schema({vol.Required(CONF_POSTAL_CODE): str}),
            errors=errors,
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which country the account is registered in.

        It has to be known before the link is built: the sign-in page is
        brand- and market-scoped, and the generic page it falls back to
        cannot sign in a non-Polish account at all.
        """
        if user_input is not None:
            # A link already built for a previous answer points at the wrong
            # market's sign-in page.
            self._authorize_url = None
            self._market = user_input[CONF_COUNTRY].upper()
            return await self.async_step_sign_in()
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_COUNTRY, default=DEFAULT_ACCOUNT_MARKET.lower()
                    ): _MARKET_SELECTOR
                }
            ),
        )

    async def async_step_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the authorization URL and the paste-back form."""
        self._ensure_authorize_url()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_exchange_and_validate(user_input["callback_url"])
            if error is not None:
                errors["base"] = error
            else:
                oauth = self._get_oauth()
                subject = decode_id_token_subject(oauth.id_token or "") or "unknown"
                await self.async_set_unique_id(subject)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Mondial Relay",
                    data={
                        CONF_REFRESH_TOKEN: oauth.refresh_token,
                        CONF_ACCOUNT_SUBJECT: subject,
                        CONF_DEVICE_UID: self._device_uid,
                        CONF_MARKET: self._market,
                        CONF_ACCOUNT_TYPE: self._account_type,
                    },
                    options={
                        CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                        CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                        CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                    },
                )

        return self.async_show_form(
            step_id="sign_in",
            data_schema=_CALLBACK_SCHEMA,
            errors=errors,
            description_placeholders={
                "authorize_url": self._authorize_url or "",
                "website_url": ACCOUNT_WEBSITES.get(
                    self._market, ACCOUNT_WEBSITES[DEFAULT_ACCOUNT_MARKET]
                ),
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the refresh token stopped working.

        The device id is already persisted on the entry and must not change —
        every signed request after reauth still has to look like it comes
        from the same device.
        """
        self._device_uid = entry_data.get(CONF_DEVICE_UID)
        self._market = entry_data.get(CONF_MARKET, DEFAULT_ACCOUNT_MARKET)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to repeat the browser paste and update the entry."""
        self._ensure_authorize_url()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_exchange_and_validate(user_input["callback_url"])
            if error is not None:
                errors["base"] = error
            else:
                oauth = self._get_oauth()
                subject = decode_id_token_subject(oauth.id_token or "") or "unknown"
                # Pasting a *different* account's authorization must not
                # silently rebind this entry to it.
                await self.async_set_unique_id(subject)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_REFRESH_TOKEN: oauth.refresh_token,
                        CONF_ACCOUNT_SUBJECT: subject,
                        CONF_MARKET: self._market,
                        CONF_ACCOUNT_TYPE: self._account_type,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_CALLBACK_SCHEMA,
            errors=errors,
            description_placeholders={"authorize_url": self._authorize_url or ""},
        )


class MondialRelayOptionsFlowHandler(OptionsFlow):
    """Manage an account hub's settings, or a tracking hub's parcels and settings.

    The two apply changes differently and must not mix: an account hub
    reloads on submit and registers no update listener, a tracking hub's
    update listener refreshes its coordinator live.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a tracking hub's menu, or show an account hub's sectioned form."""
        if is_tracking_entry(self.config_entry):
            return await self.async_step_tracking()
        if user_input is not None:
            delivered = user_input["delivered"]
            history = user_input["history"]
            # Reload so a changed history/delivered-retention setting takes
            # effect immediately. No update listener is registered —
            # combining the two is deprecated.
            self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
            return self.async_create_entry(
                title="",
                data={
                    CONF_DELIVERED_FILTER_TYPE: delivered[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        delivered[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(history[CONF_INCLUDE_HISTORY]),
                },
            )

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required("delivered"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_DELIVERED_FILTER_TYPE,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_TYPE,
                                    DEFAULT_DELIVERED_FILTER_TYPE,
                                ),
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=["days", "parcels"],
                                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                                    mode=selector.SelectSelectorMode.LIST,
                                )
                            ),
                            vol.Required(
                                CONF_DELIVERED_FILTER_AMOUNT,
                                default=current.get(
                                    CONF_DELIVERED_FILTER_AMOUNT,
                                    DEFAULT_DELIVERED_FILTER_AMOUNT,
                                ),
                            ): selector.NumberSelector(
                                selector.NumberSelectorConfig(
                                    min=1,
                                    max=365,
                                    step=1,
                                    mode=selector.NumberSelectorMode.BOX,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required("history"): section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_INCLUDE_HISTORY,
                                default=current.get(
                                    CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY
                                ),
                            ): selector.BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a tracking hub's parcels separately from its settings."""
        return self.async_show_menu(
            step_id=SOURCE_TRACKING, menu_options=["parcels", "settings"]
        )

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle a tracking hub's complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
                    },
                )
        current_codes = [
            parcel[CONF_TRACKING_CODE] for parcel in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle a tracking hub's non-parcel settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
