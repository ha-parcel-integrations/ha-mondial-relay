"""Client for Mondial Relay's public tracking, keyed on code + postcode.

One poll cycle is one session: a ``GET`` of the tracking page sets the
cookies and carries the value every lookup in that cycle has to send back,
then each due parcel is looked up in turn with the same cookies. The session
is the cycle's own, never Home Assistant's shared one, so those cookies stay
out of every other integration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import logging
import re
from typing import Any

import aiohttp

from ..const import (
    TRACKING_API_PATH,
    TRACKING_HOST,
    TRACKING_PAGE_PATH,
    TRACKING_USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_PAGE_URL = f"{TRACKING_HOST}{TRACKING_PAGE_PATH}"
_API_URL = f"{TRACKING_HOST}{TRACKING_API_PATH}"
_TOKEN_PATTERN = re.compile(r'id="tracking"[^>]*\btoken="([^"]+:[^"]+)"')


class MondialRelayTrackingError(Exception):
    """Raised when a tracking request fails for a reason other than not-found."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Mondial Relay tracking request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class MondialRelayTrackingRefusedError(MondialRelayTrackingError):
    """Raised when Mondial Relay refuses this integration's requests outright.

    Not one parcel's problem and not something a retry fixes: the cycle
    stops, last-good data stays, and only an integration update helps.
    """


def _retry_after(response: aiohttp.ClientResponse) -> float | None:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else None
    except ValueError:
        return None


def _raise_for_status(response: aiohttp.ClientResponse) -> None:
    if response.status == 403:
        raise MondialRelayTrackingRefusedError("HTTP 403", status_code=403)
    if response.status == 429:
        raise MondialRelayTrackingError(
            "HTTP 429", status_code=429, retry_after=_retry_after(response)
        )
    if response.status != 200:
        raise MondialRelayTrackingError(
            f"HTTP {response.status}", status_code=response.status
        )


class MondialRelayTrackingCycle:
    """The lookups of one poll cycle, sharing its cookies and page value."""

    def __init__(
        self, session: aiohttp.ClientSession, token: str, headers: dict[str, str]
    ) -> None:
        """Hold the cycle's session and the headers every lookup sends."""
        self._session = session
        self._headers = {
            **headers,
            "RequestVerificationToken": token,
            "Referer": _PAGE_URL,
            "Accept": "application/json",
        }
        self._not_found_logged = False

    async def async_get_shipment(
        self, tracking_code: str, postal_code: str
    ) -> dict[str, Any] | None:
        """Return the shipment for code + postcode, or ``None`` when not found.

        The brand stays empty on purpose: a shipment number is only unique
        per merchant brand, so a guessed brand can return a stranger's
        parcel, while the recipient's postcode always picks the right one.
        """
        params = {
            "shipment": tracking_code,
            "postcode": postal_code,
            "brand": "",
            "codePays": "",
        }
        async with self._session.get(
            _API_URL, params=params, headers=self._headers, timeout=_REQUEST_TIMEOUT
        ) as response:
            _raise_for_status(response)
            try:
                body = await response.json(content_type=None)
            except ValueError as err:
                raise MondialRelayTrackingRefusedError(
                    "HTTP 200 without JSON", status_code=200
                ) from err

        if not isinstance(body, dict):
            _LOGGER.debug(
                "Mondial Relay tracking returned an unexpected %s instead of an object",
                type(body).__name__,
            )
            raise MondialRelayTrackingError("unexpected response shape")
        expedition = body.get("Expedition")
        if isinstance(expedition, dict):
            return expedition
        status = body.get("status")
        if expedition is None and isinstance(status, list):
            # Both "no such parcel" and "invalid request" answer this way,
            # told apart only by translated text, so neither is branched on.
            if not self._not_found_logged:
                self._not_found_logged = True
                first = status[0] if status and isinstance(status[0], dict) else {}
                _LOGGER.debug(
                    "Mondial Relay tracking found no parcel: %s", first.get("message")
                )
            return None
        _LOGGER.debug(
            "Mondial Relay tracking returned an unexpected object with keys %s",
            sorted(body),
        )
        raise MondialRelayTrackingError("unexpected response shape")


class MondialRelayTrackingClient:
    """Opens one tracking session per poll cycle."""

    def __init__(
        self,
        session_factory: Callable[[], aiohttp.ClientSession],
        language: str | None,
    ) -> None:
        """Store how to make a cycle's session and the language to ask for."""
        self._session_factory = session_factory
        self._headers = {"User-Agent": TRACKING_USER_AGENT}
        if language:
            self._headers["Accept-Language"] = language

    @asynccontextmanager
    async def async_cycle(self) -> AsyncIterator[MondialRelayTrackingCycle]:
        """Open the tracking page, yield its lookups, and close the session."""
        session = self._session_factory()
        try:
            async with session.get(
                _PAGE_URL, headers=self._headers, timeout=_REQUEST_TIMEOUT
            ) as response:
                _raise_for_status(response)
                html = await response.text()
            match = _TOKEN_PATTERN.search(html)
            if match is None:
                raise MondialRelayTrackingRefusedError(
                    "tracking page without its request value", status_code=200
                )
            yield MondialRelayTrackingCycle(session, match.group(1), self._headers)
        finally:
            await session.close()
