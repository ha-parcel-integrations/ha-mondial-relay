"""Coordinator for the Mondial Relay parcel tracker integration.

Response handling per poll:

* a valid 200 list is normalized and paginated while more pages remain;
* 401 (rejected user token, after one refresh already tried in ``api.py``)
  starts Home Assistant reauth;
* 403 with an otherwise-valid token means the account backend rejected this
  integration's request headers, not the user's session — the poll aborts
  immediately (never calling the second list endpoint either), last-good data
  is kept, and exactly one WARNING is logged per config entry rather than
  reauth or any fallback;
* 429 backs off honoring ``Retry-After``;
* anything else is a bounded update failure that keeps last-good data.

Fetching, the inbox split and event firing live here; the parcel mapping
itself is in :mod:`.parcels`.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MondialRelayApiClient,
    MondialRelayApiError,
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
)
from .const import (
    DOMAIN,
    HOT_INTERVAL_MINUTES,
    HOT_LOOKAHEAD_HOURS,
    MID_INTERVAL_MINUTES,
    NEW_ISSUE_URL,
    QUIET_WINDOW_END_HOUR,
    QUIET_WINDOW_START_HOUR,
    STAGGER_MINUTES,
    ParcelStatus,
)
from .parcels import (
    apply_delivered_filter,
    dedupe_by_shipment_uid,
    normalize_parcel,
    sort_parcels_by_ts,
    warn_inbox_overlap_once,
)

_LOGGER = logging.getLogger(__name__)

# Base for the 429 backoff when the carrier's response carries no
# ``Retry-After`` of its own: ``BACKOFF_BASE_SECONDS * 2**consecutive_429``,
# capped at ``BACKOFF_CAP_SECONDS``.
BACKOFF_BASE_SECONDS = 60
BACKOFF_CAP_SECONDS = 3600


def _stagger_minutes(entry_id: str) -> int:
    """Deterministic per-install offset, stable across restarts."""
    digest = hashlib.sha256(entry_id.encode()).hexdigest()
    return int(digest, 16) % STAGGER_MINUTES


def _in_quiet_window(moment: datetime) -> bool:
    """Whether ``moment`` (local time) falls in the no-polling window."""
    return QUIET_WINDOW_START_HOUR <= moment.hour < QUIET_WINDOW_END_HOUR


def _next_anchor(now: datetime) -> datetime:
    """Return the next of the two daily anchors (00:00 / 06:00 local)."""
    six_today = now.replace(
        hour=QUIET_WINDOW_END_HOUR, minute=0, second=0, microsecond=0
    )
    if now < six_today:
        return six_today
    midnight_tomorrow = (now + timedelta(days=1)).replace(
        hour=QUIET_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
    )
    return midnight_tomorrow


def _hottest_tier_minutes(active_parcels: list[dict], now: datetime) -> int:
    """Tier for the account-based model.

    Never returns ``None`` — a full account fetch already returns the whole
    state, so the mid-tier poll is also the only way to discover a new
    shipment. ``active_parcels`` is expected to be incoming + outgoing,
    not-yet-delivered.
    """
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


def _next_update_interval(now: datetime, tier_minutes: int, entry_id: str) -> timedelta:
    """Turn a tier into the coordinator's next ``update_interval``.

    Clamp the naive next-due time forward to the next anchor whenever it
    would land inside the quiet window — including when ``now`` itself is
    already inside it (an anchor poll computing its own follow-up).
    """
    if _in_quiet_window(now):
        return _next_anchor(now) - now

    stagger = timedelta(minutes=_stagger_minutes(entry_id))
    candidate = now + timedelta(minutes=tier_minutes) + stagger
    if _in_quiet_window(candidate):
        return _next_anchor(now) - now
    return candidate - now


def _shipment_uids(raw_items: list[dict]) -> set[str]:
    """Return every non-empty ``expedition.shipmentUid`` in a raw list."""
    uids = set()
    for item in raw_items:
        uid = (item.get("expedition") or {}).get("shipmentUid")
        if uid:
            uids.add(uid)
    return uids


class MondialRelayCoordinator(DataUpdateCoordinator[list[dict]]):
    """Polls both inbox lists and publishes the canonical incoming/outgoing sets.

    ``coordinator.data`` is the active (not-yet-delivered) incoming parcels,
    ``self.delivered`` the rest of incoming, ``self.outgoing``/
    ``self.delivered_outgoing`` the equivalent split for parcels this account
    sent.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: MondialRelayApiClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Passing config_entry makes self.config_entry available on the
            # base class, which every helper below relies on.
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
        self._consecutive_429 = 0
        self._current_tier_minutes: int | None = None
        # barcode -> last seen ParcelStatus / (planned_from, planned_to).
        # ``None`` on the first refresh so events are suppressed for parcels
        # that already existed when the integration started.
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._known_outgoing_state: dict[str, ParcelStatus] | None = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None
        # Whether any poll has ever succeeded — a signing rejection before
        # this is true has no last-good data to fall back to, so it must
        # still fail the update (and, at first refresh, setup).
        self._ever_succeeded = False
        self._signing_outage_warned = False
        # The last successful active-incoming result, tracked independently
        # of the base class's own ``self.data`` — that is only assigned by
        # ``DataUpdateCoordinator``'s own refresh wrapper, one layer above
        # this method, so this must not rely on it to know its own last
        # result.
        self._last_active: list[dict] = []

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """Always empty — nothing per-parcel to skip in the account model."""
        return set()

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

    def _warn_signing_outage_once(self) -> None:
        if self._signing_outage_warned:
            return
        self._signing_outage_warned = True
        _LOGGER.warning(
            "Mondial Relay rejected this integration's request — this is not "
            "a problem with your account, and signing in again will not fix "
            "it. An integration update is likely needed; please check for one "
            "and, if none is available, open an issue: %s",
            NEW_ISSUE_URL,
        )

    async def _async_update_data(self) -> list[dict]:
        try:
            received_raw = await self._client.async_get_list_received()
            shipped_raw = await self._client.async_get_list_shipped()
        except MondialRelaySigningRejectedError as err:
            self._warn_signing_outage_once()
            if not self._ever_succeeded:
                raise UpdateFailed(
                    "Mondial Relay rejected the request; an integration "
                    "update may be needed"
                ) from err
            # Abort the poll and keep the last-good lists exactly as they
            # were — never reauth, never fall back to another route.
            return self._last_active
        except MondialRelayAuthError as err:
            raise ConfigEntryAuthFailed("Mondial Relay session expired") from err
        except MondialRelayApiError as err:
            if err.status_code != 429:
                raise UpdateFailed(f"Mondial Relay error: {err}") from err
            self._consecutive_429 += 1
            retry_after = err.retry_after or min(
                BACKOFF_BASE_SECONDS * 2**self._consecutive_429, BACKOFF_CAP_SECONDS
            )
            raise UpdateFailed(
                "Mondial Relay rate-limited (429)", retry_after=retry_after
            ) from err
        self._consecutive_429 = 0

        received_raw = dedupe_by_shipment_uid(received_raw)
        shipped_raw = dedupe_by_shipment_uid(shipped_raw)
        overlap = _shipment_uids(received_raw) & _shipment_uids(shipped_raw)
        warn_inbox_overlap_once(len(overlap))

        incoming_normalized = [normalize_parcel(item) for item in received_raw]
        outgoing_normalized = [normalize_parcel(item) for item in shipped_raw]

        active_incoming = [p for p in incoming_normalized if not p["delivered"]]
        delivered_incoming = [p for p in incoming_normalized if p["delivered"]]
        active_outgoing = [p for p in outgoing_normalized if not p["delivered"]]
        delivered_outgoing = [p for p in outgoing_normalized if p["delivered"]]

        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered_incoming, "delivered_at", descending=True),
            self.config_entry,
        )
        self.delivered_outgoing = apply_delivered_filter(
            sort_parcels_by_ts(delivered_outgoing, "delivered_at", descending=True),
            self.config_entry,
        )
        normalized_active = sort_parcels_by_ts(active_incoming, "planned_from")
        self.outgoing = sort_parcels_by_ts(active_outgoing, "planned_from")

        # Incoming/outgoing = active + delivered, combined so the transition
        # to delivered is visible in one set.
        incoming = normalized_active + self.delivered
        outgoing = self.outgoing + self.delivered_outgoing
        self._fire_change_events(incoming)
        self._fire_outgoing_change_events(outgoing)

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
        self._known_outgoing_state = {
            parcel["barcode"]: parcel["status"]
            for parcel in outgoing
            if parcel.get("barcode")
        }

        self._ever_succeeded = True
        self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = _hottest_tier_minutes(
            normalized_active + self.outgoing, now
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        self._last_active = normalized_active
        return normalized_active

    def _fire_change_events(self, parcels: list[dict]) -> None:
        """Fire registered / status-changed / delivered / delivery-time events.

        Silent on the very first refresh — we cannot know which parcels are
        genuinely new versus already present before HA started.
        """
        if self._known_state is None:
            return

        known_times = self._known_delivery_times or {}
        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode:
                continue
            new_status = parcel["status"]
            if barcode not in self._known_state:
                if new_status != ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_registered",
                        {**parcel, "device_id": device_id},
                    )
                continue

            if self._known_state[barcode] != new_status:
                if new_status == ParcelStatus.DELIVERED:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_delivered",
                        {**parcel, "device_id": device_id},
                    )
                else:
                    self.hass.bus.async_fire(
                        f"{DOMAIN}_parcel_status_changed",
                        {
                            **parcel,
                            "device_id": device_id,
                            "old_status": self._known_state[barcode],
                            "new_status": new_status,
                        },
                    )

            old_from, old_to = known_times.get(barcode, (None, None))
            new_from = parcel.get("planned_from")
            new_to = parcel.get("planned_to")
            from_changed = new_from is not None and new_from != old_from
            to_changed = new_to is not None and new_to != old_to
            if from_changed or to_changed:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivery_time_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_planned_from": old_from,
                        "new_planned_from": new_from,
                        "old_planned_to": old_to,
                        "new_planned_to": new_to,
                    },
                )

    def _fire_outgoing_change_events(self, parcels: list[dict]) -> None:
        """Fire status/delivered events for outgoing parcels.

        Silent on the very first refresh, matching ``_fire_change_events``.
        There is no outgoing ``registered`` or ``delivery_time_changed``
        event — those are intentionally out of scope.
        """
        if self._known_outgoing_state is None:
            return

        device_id = self._device_id()

        for parcel in parcels:
            barcode = parcel.get("barcode")
            if not barcode or barcode not in self._known_outgoing_state:
                continue
            old_status = self._known_outgoing_state[barcode]
            new_status = parcel["status"]
            if new_status == old_status:
                continue

            if new_status == ParcelStatus.DELIVERED:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                self.hass.bus.async_fire(
                    f"{DOMAIN}_outgoing_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": old_status,
                        "new_status": new_status,
                    },
                )
