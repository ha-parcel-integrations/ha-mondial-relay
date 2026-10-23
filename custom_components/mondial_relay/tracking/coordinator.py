"""Coordinator for a Mondial Relay tracking hub (code + postcode).

Response handling per poll:

* nothing due (nothing tracked, or everything delivered) fetches nothing and
  suspends polling until a parcel is added;
* otherwise one session opens the tracking page and looks each due parcel up
  in turn — never in parallel;
* a found parcel is normalised; a not-found one keeps its last payload, or a
  placeholder so the parcel the user asked for still shows;
* a refusal (a ``403``, or a ``200`` that is not JSON or has no page value)
  stops the cycle, keeps last-good data and logs one WARNING per entry until
  a cycle succeeds again;
* a ``429`` stops the cycle and backs off honoring ``Retry-After``;
* any other failure of one lookup is that parcel's problem alone.

Fetching and the event contract live here and in :mod:`..events`; the parcel
mapping itself is in :mod:`.parcels`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import NoReturn

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_TRACKING_CODE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    NEW_ISSUE_URL,
    ParcelStatus,
)
from ..events import fire_incoming_change_events
from ..parcel_lists import apply_delivered_filter, sort_parcels_by_ts
from ..polling import backoff_seconds, next_update_interval
from .api import (
    MondialRelayTrackingClient,
    MondialRelayTrackingError,
    MondialRelayTrackingRefusedError,
)
from .parcels import normalize_parcel, tracking_page

_LOGGER = logging.getLogger(__name__)


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int | None:
    """Tier for the code-based model.

    ``None`` means "stop polling entirely" — nothing is tracked, or every
    tracked parcel is already delivered (already filtered out of
    ``active_parcels`` by the caller).
    """
    if not active_parcels:
        return None

    for parcel in active_parcels:
        if parcel["status"] != ParcelStatus.OUT_FOR_DELIVERY:
            continue
        planned_from = parcel.get("planned_from")
        if not planned_from:
            return HOT_INTERVAL_MINUTES
        planned_dt = dt_util.parse_datetime(planned_from)
        if planned_dt is None:
            return HOT_INTERVAL_MINUTES
        if dt_util.as_utc(now) >= dt_util.as_utc(planned_dt) - timedelta(
            hours=HOT_LOOKAHEAD_HOURS
        ):
            return HOT_INTERVAL_MINUTES

    return MID_INTERVAL_MINUTES


class MondialRelayTrackingCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls each tracked code against the hub's postcode.

    ``coordinator.data`` is the active (not-yet-delivered) parcels,
    ``self.delivered`` the rest. A tracking hub follows incoming parcels
    only, so ``outgoing``/``delivered_outgoing`` are always empty.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        client: MondialRelayTrackingClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            # Recomputed at the end of every refresh — start with the hot
            # cadence so the very first poll, right after setup, happens
            # promptly regardless of what it finds.
            update_interval=timedelta(minutes=HOT_INTERVAL_MINUTES),
        )
        self._client = client
        self.delivered: list[dict] = []
        self.outgoing: list[dict] = []
        self.delivered_outgoing: list[dict] = []
        # tracking_code -> last found ``Expedition``, so a transient failure
        # or a not-found blip keeps the parcel visible instead of dropping
        # its sensor. Lives for the integration's lifetime.
        self._raw_cache: dict[str, dict] = {}
        # Codes confirmed delivered on a prior refresh — their payload can
        # never change again, so they are left out of the fetch.
        self._delivered_codes: set[str] = set()
        self._consecutive_429 = 0
        self._current_tier_minutes: int | None = None
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: dict[str, tuple[str | None, str | None]] | None = (
            None
        )
        self._cached_device_id: str | None = None
        self._refusal_warned = False
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Tracking codes currently skipped from the fetch (diagnostics only)."""
        return self._delivered_codes

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    def _tracked(self) -> list[str]:
        """Return the configured tracking codes."""
        return [
            item[CONF_TRACKING_CODE]
            for item in self.config_entry.options.get(CONF_PARCELS, [])
            if item.get(CONF_TRACKING_CODE)
        ]

    @property
    def _include_history(self) -> bool:
        """Whether the opt-in per-parcel history option is enabled."""
        return bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )

    def _warn_refusal_once(self) -> None:
        if self._refusal_warned:
            return
        self._refusal_warned = True
        _LOGGER.warning(
            "Mondial Relay's public tracking refused this integration's "
            "request. Your tracking codes and postcode are not the problem; "
            "an integration update is likely needed. Please check for one "
            "and, if none is available, open an issue: %s",
            NEW_ISSUE_URL,
        )

    def _raise_backoff(self, err: MondialRelayTrackingError) -> NoReturn:
        self._consecutive_429 += 1
        retry_after = err.retry_after or backoff_seconds(self._consecutive_429)
        raise UpdateFailed(
            "Mondial Relay rate-limited (429)", retry_after=retry_after
        ) from err

    async def _async_fetch(
        self, codes: list[str], postal_code: str
    ) -> tuple[dict[str, dict], int]:
        """Look every code up in one cycle; return payloads and failed lookups."""
        raws_by_code: dict[str, dict] = {}
        errors = 0
        async with self._client.async_cycle() as cycle:
            for code in codes:
                try:
                    result = await cycle.async_get_shipment(code, postal_code)
                except MondialRelayTrackingRefusedError:
                    raise
                except MondialRelayTrackingError as err:
                    if err.status_code == 429:
                        self._raise_backoff(err)
                    errors += 1
                    _LOGGER.warning("Mondial Relay lookup failed for %s: %s", code, err)
                    result = self._raw_cache.get(code)
                except aiohttp.ClientError as err:
                    errors += 1
                    _LOGGER.warning("Mondial Relay lookup failed for %s: %s", code, err)
                    result = self._raw_cache.get(code)
                else:
                    if result is None:
                        result = self._raw_cache.get(code) or {"Numero": code}
                    else:
                        self._raw_cache[code] = result
                if result is not None:
                    raws_by_code[code] = result
        return raws_by_code, errors

    async def _async_update_data(self) -> list[dict]:
        """Fetch every due parcel and split into active vs delivered."""
        codes = self._tracked()
        postal_code = self.config_entry.options.get(CONF_POSTAL_CODE, "")

        tracked_codes = set(codes)
        self._raw_cache = {
            code: raw for code, raw in self._raw_cache.items() if code in tracked_codes
        }
        self._delivered_codes &= tracked_codes
        codes_to_fetch = [code for code in codes if code not in self._delivered_codes]

        raws_by_code: dict[str, dict] = {}
        errors = 0
        if codes_to_fetch:
            try:
                raws_by_code, errors = await self._async_fetch(
                    codes_to_fetch, postal_code
                )
            except MondialRelayTrackingRefusedError as err:
                self._warn_refusal_once()
                raise UpdateFailed(
                    "Mondial Relay refused the request; an integration update "
                    "may be needed"
                ) from err
            except MondialRelayTrackingError as err:
                if err.status_code == 429:
                    self._raise_backoff(err)
                raise UpdateFailed(f"Mondial Relay error: {err}") from err
        self._consecutive_429 = 0
        self._refusal_warned = False

        if codes_to_fetch and errors == len(codes_to_fetch) and not raws_by_code:
            raise UpdateFailed("Mondial Relay unreachable for all tracked parcels")

        for code in self._delivered_codes:
            cached = self._raw_cache.get(code)
            if cached is not None:
                raws_by_code[code] = cached

        page = tracking_page(self.hass.config.country, self.hass.config.language)
        include_history = self._include_history
        entries = [
            (
                code,
                normalize_parcel(raw, page=page, include_history=include_history),
            )
            for code, raw in raws_by_code.items()
        ]
        active = [parcel for _, parcel in entries if not parcel["delivered"]]
        delivered = [parcel for _, parcel in entries if parcel["delivered"]]
        self._delivered_codes = {
            code for code, parcel in entries if parcel["delivered"]
        }

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active, "planned_from")

        incoming = normalized_active + self.delivered
        fire_incoming_change_events(
            self.hass,
            incoming,
            self._known_state,
            self._known_delivery_times,
            self._device_id(),
        )
        self._known_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in incoming
            if parcel.get("barcode")
        }
        self._known_delivery_times = {
            parcel["barcode"]: (parcel.get("planned_from"), parcel.get("planned_to"))
            for parcel in incoming
            if parcel.get("barcode")
        }

        # A poll served entirely from cache must not present itself as a
        # successful update.
        if not codes_to_fetch or errors < len(codes_to_fetch):
            self.last_success_time = datetime.now(UTC)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(normalized_active, now)
        self.update_interval = next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return normalized_active
