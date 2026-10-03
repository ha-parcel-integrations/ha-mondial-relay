"""Mondial Relay mobile account backend client.

Fetches the account's own record (``user-infos``, at setup only) and the two
confirmed list endpoints. ``parcels-detail`` is not called (its response
envelope is unconfirmed), ``parcels-search`` is never probed, and the
``*-not-migrated`` pair is deliberately left alone — it only ever holds a
shipment the user created minutes ago. Every request needs both a bearer access
token (``oauth.py``) and a derived signing header (``signing.py``); either
one being wrong or stale produces a different, distinguishable failure (see
the response table in the module-level docstring of ``coordinator.py``).
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import (
    MR_ACCEPT_LANGUAGE,
    MR_BFF_BASE_URL,
    MR_LIST_RECEIVED_PATH,
    MR_LIST_SHIPPED_PATH,
    MR_ORIGIN_APP,
    MR_PAGE_SIZE,
    MR_USER_INFO_PATH,
    NEW_ISSUE_URL,
    USER_AGENT,
)
from .oauth import MondialRelayOAuthError, MondialRelayOAuthSession
from .signing import build_signature_headers

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


class MondialRelayApiError(Exception):
    """Raised when a Mondial Relay BFF call fails for a non-auth reason."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Mondial Relay API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class MondialRelayAuthError(MondialRelayApiError):
    """Raised when the user's own token is rejected even after one refresh.

    Distinct from :class:`MondialRelaySigningRejectedError`: this means the
    *user's* session is gone and Home Assistant should start reauth. A 403 is a
    different failure entirely — see that
    class's docstring.
    """


class MondialRelaySigningRejectedError(MondialRelayApiError):
    """Raised on a 403 with an otherwise-valid bearer token.

    The user's session is fine, so signing in again cannot fix this and it
    must never reach Home Assistant's reauth flow; the integration itself
    needs an update.
    """


# One-shot, so an unexpected user-infos shape is reported once per session
# rather than on every setup attempt.
_user_info_shape_warned = False


def has_confirmed_phone(user_info: dict[str, Any]) -> bool:
    """Whether the account's phone number counts as confirmed.

    Only an explicit ``false`` means unconfirmed. A missing or differently
    shaped ``phone`` object must never block a setup that would otherwise
    work, so it warns once and returns ``True``.
    """
    global _user_info_shape_warned
    phone = user_info.get("phone")
    if isinstance(phone, dict) and "valid" in phone:
        return phone["valid"] is not False
    if not _user_info_shape_warned:
        _user_info_shape_warned = True
        _LOGGER.warning(
            "Mondial Relay's account record carried no phone.valid flag; "
            "treating the account as usable. Please report this at %s",
            NEW_ISSUE_URL,
        )
    return True


def account_type(user_info: dict[str, Any]) -> str | None:
    """Return ``userType`` from the account record, if it carries one."""
    value = user_info.get("userType")
    return str(value) if value else None


class MondialRelayApiClient:
    """Client for the Mondial Relay mobile account backend."""

    def __init__(
        self,
        oauth: MondialRelayOAuthSession,
        session: aiohttp.ClientSession,
        *,
        device_uid: str,
    ) -> None:
        """Initialise the client with its OAuth session, HTTP session and device id."""
        self._oauth = oauth
        self._session = session
        self._device_uid = device_uid

    async def _async_headers(self) -> dict[str, str]:
        token = await self._oauth.async_get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            **build_signature_headers(
                device_uid=self._device_uid,
                language=MR_ACCEPT_LANGUAGE,
                origin_app=MR_ORIGIN_APP,
            ),
        }

    async def _async_get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        """GET one BFF path and return its parsed body; refreshes once on 401."""
        url = f"{MR_BFF_BASE_URL}/{path}"

        for attempt in range(2):
            headers = await self._async_headers()
            async with self._session.get(
                url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT
            ) as response:
                if response.status == 401:
                    if attempt == 0:
                        try:
                            await self._oauth.async_handle_unauthorized()
                        except MondialRelayOAuthError as err:
                            raise MondialRelayAuthError(
                                "session refresh failed", status_code=401
                            ) from err
                        continue
                    raise MondialRelayAuthError("HTTP 401", status_code=401)
                if response.status == 403:
                    raise MondialRelaySigningRejectedError("HTTP 403", status_code=403)
                if response.status == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    try:
                        retry_after = float(retry_after_header) if retry_after_header else None
                    except ValueError:
                        retry_after = None
                    raise MondialRelayApiError(
                        "HTTP 429", status_code=429, retry_after=retry_after
                    )
                if response.status != 200:
                    raise MondialRelayApiError(
                        f"HTTP {response.status}", status_code=response.status
                    )
                try:
                    return await response.json(content_type=None)
                except ValueError as err:
                    raise MondialRelayApiError(f"unparseable body ({err})") from err

        raise MondialRelayAuthError("HTTP 401", status_code=401)

    async def _async_fetch_page(self, path: str, *, page_index: int) -> dict[str, Any]:
        """Fetch one page of a list endpoint."""
        payload = await self._async_get(
            path,
            params={"pageIndex": str(page_index), "pageSize": str(MR_PAGE_SIZE)},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
            raise MondialRelayApiError("unexpected body (no list envelope)")
        return payload

    async def _async_fetch_list(self, path: str) -> list[dict[str, Any]]:
        """Fetch every page of a list endpoint, stopping once fully paginated."""
        items: list[dict[str, Any]] = []
        page_index = 0
        while True:
            envelope = await self._async_fetch_page(path, page_index=page_index)
            items.extend(item for item in envelope["list"] if isinstance(item, dict))
            total_pages = envelope.get("totalPages")
            if not isinstance(total_pages, int) or page_index >= total_pages - 1:
                break
            page_index += 1
        return items

    async def async_get_user_info(self) -> dict[str, Any]:
        """Return the signed-in account's own record.

        The first call the official app makes after a login, and the only one
        that distinguishes "this token is not accepted at all" from "the
        token is fine but the parcel feed refused" — so setup asks for it
        before it ever looks at a parcel list.
        """
        payload = await self._async_get(MR_USER_INFO_PATH)
        if not isinstance(payload, dict):
            raise MondialRelayApiError("unexpected body (user-infos is not an object)")
        return payload

    async def async_validate_parcel_access(self) -> None:
        """Bounded, single-page call proving the account's parcel feed is readable."""
        await self._async_fetch_page(MR_LIST_RECEIVED_PATH, page_index=0)

    async def async_get_list_received(self) -> list[dict[str, Any]]:
        """Return every raw item from ``parcels-list-received`` (incoming)."""
        return await self._async_fetch_list(MR_LIST_RECEIVED_PATH)

    async def async_get_list_shipped(self) -> list[dict[str, Any]]:
        """Return every raw item from ``parcels-list-shipped`` (outgoing)."""
        return await self._async_fetch_list(MR_LIST_SHIPPED_PATH)
