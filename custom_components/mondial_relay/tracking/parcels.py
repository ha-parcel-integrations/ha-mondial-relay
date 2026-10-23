"""Canonical parcel shape for the tracking source.

Input is the ``Expedition`` object of one code + postcode lookup. The status
comes from the highest step of ``SuiviParEtapes`` that carries an event,
mapped by its integer key — never by its label or any event text, which are
translated. ``raw_status`` carries that step as a string.

Everything here is a **pure function** — no I/O, no Home Assistant objects —
so the carrier-specific mapping stays unit-testable without Home Assistant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from ..const import (
    HISTORY_MAX_EVENTS,
    NEW_ISSUE_URL,
    TRACKING_PAGE_BE_FR,
    TRACKING_PAGE_BE_NL,
    TRACKING_PAGE_FR,
    TRACKING_PAGE_NL,
    ParcelStatus,
)
from ..parcel_lists import parse_iso

_LOGGER = logging.getLogger(__name__)

# Timestamps arrive as naive local time. France, Belgium, the Netherlands and
# Spain share this zone; Portugal is an hour behind and is accepted as such.
CARRIER_TZ = ZoneInfo("Europe/Paris")

# The only delivery mode seen on a real parcel so far: delivery to a pickup
# point, where step 4 means "ready to collect".
PICKUP_POINT_MODE = "24R"

_STEP_MAP: dict[int, ParcelStatus] = {
    1: ParcelStatus.REGISTERED,
    2: ParcelStatus.IN_TRANSIT,
    # Spans days of hub sorting, so it is not out for delivery.
    3: ParcelStatus.IN_TRANSIT,
    4: ParcelStatus.AT_PICKUP_POINT,
    5: ParcelStatus.DELIVERED,
}
_STEP_AVAILABLE = 4
_STEP_DELIVERED = 5

_unmapped_steps_logged: set[str] = set()
_delivery_modes_logged: set[str] = set()


def _warn_unmapped_step(step: str) -> None:
    if step in _unmapped_steps_logged:
        return
    _unmapped_steps_logged.add(step)
    _LOGGER.warning(
        "Unrecognised Mondial Relay tracking step — help us map it. Open an "
        "issue and paste this line: %s\n  step=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        step,
    )


def _warn_delivery_mode(mode: str) -> None:
    if mode in _delivery_modes_logged:
        return
    _delivery_modes_logged.add(mode)
    _LOGGER.warning(
        "Unconfirmed Mondial Relay delivery mode — help us map it. Open an "
        "issue and paste this line: %s\n  DeliveryMode=%s → not treated as a "
        "pickup point delivery, and its step 4 is reported as 'in_transit'",
        NEW_ISSUE_URL,
        mode,
    )


def local_timestamp(value: Any) -> str | None:
    """Return a carrier timestamp as an aware ISO 8601 string, or ``None``.

    The fraction has no fixed width (none, two, three digits), and a value
    without an offset is local time in :data:`CARRIER_TZ`.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CARRIER_TZ)
    return parsed.isoformat()


def _reached_steps(expedition: dict[str, Any]) -> list[tuple[int, str]]:
    """Return every step that carries an event, highest first."""
    steps = expedition.get("SuiviParEtapes")
    reached: list[tuple[int, str]] = []
    for key, step in (steps if isinstance(steps, dict) else {}).items():
        if not isinstance(step, dict) or not step.get("Evenement"):
            continue
        try:
            reached.append((int(key), str(key)))
        except (TypeError, ValueError):
            _warn_unmapped_step(str(key))
    return sorted(reached, reverse=True)


def map_status(
    expedition: dict[str, Any],
) -> tuple[ParcelStatus, str | None, int | None]:
    """Return ``(status, raw_status, mapped step)`` for one ``Expedition``.

    An unknown highest step falls back to the highest one that maps, and
    only then to ``unknown``; either way the unknown step warns once.
    """
    reached = _reached_steps(expedition)
    if not reached:
        _warn_unmapped_step("none")
        return ParcelStatus.UNKNOWN, None, None
    raw_status = reached[0][1]
    for number, raw in reached:
        status = _STEP_MAP.get(number)
        if status is not None:
            return status, raw_status, number
        _warn_unmapped_step(raw)
    return ParcelStatus.UNKNOWN, raw_status, None


def _step_date(expedition: dict[str, Any], step: int) -> str | None:
    """Return a reached step's own date; only called once the step is reached."""
    event = expedition["SuiviParEtapes"][str(step)]["Evenement"]
    return local_timestamp(event.get("Date")) if isinstance(event, dict) else None


def _pickup_point(expedition: dict[str, Any]) -> str | None:
    """Return the pickup point's name once an event at it names it.

    Only the event dated exactly as step 5 (collected) or step 4 (ready) can
    name the parcel's own pickup point; the drop-off event names the
    sender's drop-off point, so events are matched by date, never position.
    """
    steps = expedition.get("SuiviParEtapes")
    events = expedition.get("Evenements")
    if not isinstance(steps, dict) or not isinstance(events, list):
        return None
    for step in (str(_STEP_DELIVERED), str(_STEP_AVAILABLE)):
        reached = steps.get(step)
        mark = reached.get("Evenement") if isinstance(reached, dict) else None
        date = mark.get("Date") if isinstance(mark, dict) else None
        if not date:
            continue
        for event in events:
            if not isinstance(event, dict) or event.get("Date") != date:
                continue
            relay = event.get("DetailPointRelais")
            address = relay.get("Adresse") if isinstance(relay, dict) else None
            name = address.get("Libelle") if isinstance(address, dict) else None
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _planned_window(expedition: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the latest expected delivery day as a whole-day local window.

    The value is the drop-off moment plus a fixed term, written in UTC
    without an offset, so it is converted before its local day is taken.
    Year 1 is the carrier's placeholder for "no estimate yet".
    """
    estimate = expedition.get("EstimatedDeliveryDate")
    if not isinstance(estimate, str):
        return None, None
    try:
        parsed = datetime.fromisoformat(estimate)
    except ValueError:
        return None, None
    if parsed.year == 1:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    day = parsed.astimezone(CARRIER_TZ).date()
    start = datetime(day.year, day.month, day.day, tzinfo=CARRIER_TZ)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.isoformat(), end.isoformat()


def build_history(
    events: Any, *, max_events: int = HISTORY_MAX_EVENTS
) -> list[dict[str, Any]]:
    """Build the canonical ``history`` from ``Evenements``.

    Events carry no status code of their own, so ``status`` stays ``None``
    and ``raw_status`` is the event's own text. Sorted oldest → newest and
    capped to the most recent ``max_events``, like every carrier in the suite.
    """
    parseable: list[tuple[datetime, dict[str, Any]]] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict):
            continue
        timestamp = local_timestamp(event.get("Date"))
        parsed = parse_iso(timestamp)
        if parsed is None:
            continue
        label = event.get("Libelle")
        parseable.append(
            (
                parsed,
                {
                    "timestamp": timestamp,
                    "status": None,
                    "raw_status": label.strip() if isinstance(label, str) else None,
                },
            )
        )
    parseable.sort(key=lambda item: item[0])
    return [entry for _, entry in parseable][-max_events:]


def tracking_page(country: str | None, language: str | None) -> str:
    """Return the tracking page that suits Home Assistant's country and language."""
    french = (language or "").lower().startswith("fr")
    if country == "NL":
        return TRACKING_PAGE_NL
    if country == "FR":
        return TRACKING_PAGE_FR
    return TRACKING_PAGE_BE_FR if french else TRACKING_PAGE_BE_NL


def tracking_url(expedition: dict[str, Any], page: str) -> str | None:
    """Return a link that opens this parcel for anyone it is shared with.

    Built from the response's own number and brand, never the postcode, so
    the link carries no personal data.
    """
    number = expedition.get("Numero")
    brand = expedition.get("Enseigne")
    if number in (None, "") or not isinstance(brand, str) or len(brand) < 2:
        return None
    query = urlencode({"numeroExpedition": number, "codeMarque": brand[:2]})
    return f"{page}?{query}"


def normalize_parcel(
    expedition: dict[str, Any],
    *,
    page: str,
    include_history: bool = False,
) -> dict[str, Any]:
    """Return a carrier-agnostic parcel dict for one ``Expedition``."""
    # A code that was never found has only the placeholder the coordinator
    # keeps for it, with nothing to map or warn about yet.
    has_steps = "SuiviParEtapes" in expedition
    status, raw_status, step = (
        map_status(expedition) if has_steps else (ParcelStatus.UNKNOWN, None, None)
    )

    mode = expedition.get("DeliveryMode")
    pickup = mode == PICKUP_POINT_MODE
    if has_steps and not pickup:
        _warn_delivery_mode(str(mode))
        if step == _STEP_AVAILABLE:
            status = ParcelStatus.IN_TRANSIT

    delivered = status is ParcelStatus.DELIVERED
    planned_from, planned_to = _planned_window(expedition)
    number = expedition.get("Numero")
    merchant = expedition.get("Enseigne")

    return {
        "carrier": "Mondial Relay",
        "barcode": str(number) if number not in (None, "") else None,
        # The merchant's own code (e.g. V1VINTNL) is the only sender the
        # response carries; no name is behind it.
        "sender": (merchant.strip() or None) if isinstance(merchant, str) else None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": _step_date(expedition, _STEP_DELIVERED) if delivered else None,
        "planned_from": None if delivered else planned_from,
        "planned_to": None if delivered else planned_to,
        "pickup": pickup,
        "pickup_point": _pickup_point(expedition) if pickup else None,
        "url": tracking_url(expedition, page),
        "weight": None,
        "dimensions": None,
        "history": (
            build_history(expedition.get("Evenements")) if include_history else None
        ),
        "raw": expedition,
    }
