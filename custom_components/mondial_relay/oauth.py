"""The InPost Group OAuth/PKCE session backing a Mondial Relay account.

The official app has no reachable redirect of its own (it opens a native
deep link a server-side integration cannot catch), so the config flow builds
the authorization URL itself and asks the user to open it in a browser and
paste back the resulting callback URL. This module owns the whole token
lifecycle: building that URL with a fresh PKCE verifier/state/nonce,
exchanging the pasted-back code, and refreshing the access token before it
expires.

Modelled on ha-dhl's DE OIDC session (a near-identical browser-paste PKCE
flow) — same shape, different provider.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from .const import (
    DEFAULT_ACCOUNT_MARKET,
    MR_OAUTH_AUTHORIZE_URL,
    MR_OAUTH_BRAND,
    MR_OAUTH_CLIENT_ID,
    MR_OAUTH_REDIRECT_URI,
    MR_OAUTH_SCOPE,
    MR_OAUTH_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

# Refresh this long before the access token's own expiry — a poll starting
# just under the wire must not go out with a token that expires mid-flight.
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Used only when a token response carries no expires_in at all.
_FALLBACK_TOKEN_LIFETIME = timedelta(minutes=15)

# The sign-in page's own translations, per market. Belgium is bilingual, so
# Home Assistant's language picks the side; Spain and Portugal have no
# translation and render the French copy, but the tag's region still decides
# which dial code the phone step starts on, so send the region-correct one
# rather than a bare language.
_MARKET_LANGUAGES = {
    "FR": ("fr-FR",),
    "BE": ("fr-BE", "nl-BE"),
    "NL": ("nl-NL",),
    "ES": ("es-ES",),
    "PT": ("pt-PT",),
}


class MondialRelayOAuthError(Exception):
    """Raised when the identity provider can't hand out a usable token."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """Store the message and, where available, the status and error code."""
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class MondialRelayOAuthAuthError(MondialRelayOAuthError):
    """Raised when the refresh token is rejected.

    Distinct from :class:`MondialRelayOAuthError` so the caller can map only
    this one to ``ConfigEntryAuthFailed`` — a transport-level outage must
    never push a user into a reauth flow they cannot complete.
    """


def generate_pkce() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, code_challenge)`` pair (S256).

    Generated per login attempt, never reused — a fixed verifier would let
    one flow's authorization code be replayed against another's.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """Return a fresh random ``state`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def generate_nonce() -> str:
    """Return a fresh random ``nonce`` value for one authorization request."""
    return secrets.token_urlsafe(24)


def sign_in_language(language: str | None, market: str) -> str:
    """Pick the sign-in page's language tag for one market."""
    choices = _MARKET_LANGUAGES.get(market) or _MARKET_LANGUAGES[DEFAULT_ACCOUNT_MARKET]
    spoken = (language or "").lower()[:2]
    for choice in choices:
        if spoken and choice.startswith(spoken):
            return choice
    return choices[0]


def parse_callback_url(value: str) -> tuple[str | None, str | None]:
    """Pull ``code``/``state`` out of the pasted callback URL.

    Users paste messily — leading/trailing whitespace, a trailing newline —
    so this strips first.
    """
    parsed = urlparse(value.strip())
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    state = params.get("state", [None])[0]
    return code, state


def is_valid_callback_url(value: str) -> bool:
    """Whether a pasted URL is HTTPS, on the expected host, with the exact path.

    Checked before the code is ever used — a URL that fails this was not
    produced by the real authorization step and must not be exchanged.
    """
    parsed = urlparse(value.strip())
    expected = urlparse(MR_OAUTH_REDIRECT_URI)
    return (
        parsed.scheme == "https"
        and parsed.netloc == expected.netloc
        and parsed.path == expected.path
    )


def decode_id_token_claims(id_token: str) -> dict[str, Any] | None:
    """Best-effort, unverified read of an ID token's claim set.

    Never used to authorise anything — only to key ``unique_id`` at
    config-flow time — so no signature check is needed or possible without
    the tenant's signing key. Returns ``None`` on any malformed token.
    """
    try:
        _, payload_b64, _ = id_token.split(".")
    except ValueError:
        return None
    padding = "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def decode_id_token_subject(id_token: str) -> str | None:
    """Best-effort, unverified read of the ID token's ``sub`` claim."""
    claims = decode_id_token_claims(id_token)
    if claims is None:
        return None
    subject = claims.get("sub")
    return str(subject) if subject else None


class MondialRelayOAuthSession:
    """Owns one config entry's OAuth tokens: authorization, exchange, refresh.

    One instance per config entry, constructed with the entry's shared
    aiohttp session and (once past config-flow) the refresh token persisted
    in ``entry.data``. A fresh instance always starts with no cached access
    token, so its first :meth:`async_get_access_token` call refreshes even
    though the refresh token itself is not new.
    """

    def __init__(
        self, session: aiohttp.ClientSession, *, refresh_token: str | None = None
    ) -> None:
        """Initialise with an optional already-persisted refresh token."""
        self._session = session
        self.refresh_token = refresh_token
        self.id_token: str | None = None
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._refresh_token_changed = False

    @property
    def _needs_refresh(self) -> bool:
        """Whether the cached access token is missing or inside the refresh margin."""
        if self._access_token is None or self._expires_at is None:
            return True
        return datetime.now(timezone.utc) >= self._expires_at - TOKEN_REFRESH_MARGIN

    def pop_refresh_token_changed(self) -> bool:
        """Return whether the refresh token rotated, and clear the flag."""
        value = self._refresh_token_changed
        self._refresh_token_changed = False
        return value

    def build_authorization_url(
        self, *, language: str | None = None, market: str = DEFAULT_ACCOUNT_MARKET
    ) -> tuple[str, str, str]:
        """Build the one-time browser authorization URL for one market.

        Returns ``(url, code_verifier, state)`` — the caller (config_flow.py)
        holds ``code_verifier`` and ``state`` for the lifetime of this one
        flow and passes ``code_verifier`` back into
        :meth:`async_exchange_code`.

        ``brand``, ``lang`` and ``supported_markets`` are not cosmetic: drop
        them and the identity provider serves the generic InPost sign-up
        instead of Mondial Relay's, whose phone step accepts Polish numbers
        only, which locks out every market this carrier actually delivers in.
        """
        code_verifier, code_challenge = generate_pkce()
        state = generate_state()
        nonce = generate_nonce()
        params = {
            "response_type": "code",
            "client_id": MR_OAUTH_CLIENT_ID,
            "redirect_uri": MR_OAUTH_REDIRECT_URI,
            "scope": MR_OAUTH_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            "response_mode": "query",
            "brand": MR_OAUTH_BRAND,
            "lang": sign_in_language(language, market),
            "supported_markets": market,
        }
        query = "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
        return f"{MR_OAUTH_AUTHORIZE_URL}?{query}", code_verifier, state

    async def async_exchange_code(self, code: str, code_verifier: str) -> None:
        """Exchange an authorization code for tokens.

        The authorization code and PKCE verifier are single-use by design —
        the caller must discard them immediately after this call, whether it
        succeeds or fails.
        """
        body = {
            "grant_type": "authorization_code",
            "client_id": MR_OAUTH_CLIENT_ID,
            "redirect_uri": MR_OAUTH_REDIRECT_URI,
            "code_verifier": code_verifier,
            "code": code,
        }
        payload = await self._async_post_token(body)
        self._store_tokens(payload)
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise MondialRelayOAuthError(
                "token response had no refresh_token — offline access was not granted"
            )
        self.refresh_token = refresh_token

    async def async_get_access_token(self) -> str:
        """Return a valid access token, refreshing first if it is stale."""
        if self.refresh_token is None:
            raise MondialRelayOAuthError(
                "no refresh_token to refresh with — the config entry is not set up"
            )
        if self._needs_refresh:
            await self._async_refresh()
        assert self._access_token is not None
        return self._access_token

    async def async_handle_unauthorized(self) -> str:
        """Force one refresh after a 401 from the account backend.

        Callers must retry the failing request exactly once with the
        returned token and then give up — never loop.
        """
        if self.refresh_token is None:
            raise MondialRelayOAuthError(
                "no refresh_token to refresh with — the config entry is not set up"
            )
        await self._async_refresh()
        assert self._access_token is not None
        return self._access_token

    async def _async_refresh(self) -> None:
        body = {
            "grant_type": "refresh_token",
            "client_id": MR_OAUTH_CLIENT_ID,
            "refresh_token": self.refresh_token,
        }
        payload = await self._async_post_token(body)
        self._store_tokens(payload)
        new_refresh_token = payload.get("refresh_token")
        if new_refresh_token and new_refresh_token != self.refresh_token:
            self.refresh_token = new_refresh_token
            self._refresh_token_changed = True

    async def _async_post_token(self, body: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint; raises :class:`MondialRelayOAuthAuthError` on rejection."""
        async with self._session.post(
            MR_OAUTH_TOKEN_URL, data=body, timeout=_REQUEST_TIMEOUT
        ) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except ValueError:
                payload = {}
            if (
                response.status == 200
                and isinstance(payload, dict)
                and payload.get("access_token")
            ):
                return payload
            error = payload.get("error") if isinstance(payload, dict) else None
            if response.status in (400, 401):
                raise MondialRelayOAuthAuthError(
                    f"the identity provider rejected the token request ({error or response.status})",
                    status_code=response.status,
                    error_code=str(error) if error else None,
                )
            raise MondialRelayOAuthError(
                f"token endpoint returned HTTP {response.status}",
                status_code=response.status,
            )

    def _store_tokens(self, payload: dict[str, Any]) -> None:
        """Cache the access token, its expiry and the ID token from a token response."""
        access_token = payload.get("access_token")
        if not access_token:
            raise MondialRelayOAuthError("token response had no access_token")
        self._access_token = access_token
        self.id_token = payload.get("id_token") or self.id_token
        expires_in = payload.get("expires_in")
        try:
            lifetime = timedelta(seconds=float(expires_in))
        except (TypeError, ValueError):
            lifetime = _FALLBACK_TOKEN_LIFETIME
        self._expires_at = datetime.now(timezone.utc) + lifetime


__all__ = [
    "MondialRelayOAuthAuthError",
    "MondialRelayOAuthError",
    "MondialRelayOAuthSession",
    "decode_id_token_claims",
    "decode_id_token_subject",
    "generate_nonce",
    "generate_pkce",
    "generate_state",
    "is_valid_callback_url",
    "parse_callback_url",
    "sign_in_language",
]
