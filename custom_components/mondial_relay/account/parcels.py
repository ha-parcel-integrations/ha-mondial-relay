"""Canonical parcel shape for the account source, and its inbox-list helpers.

``expedition.stepSection`` is the integer value of the official app's own
step-section enum; ``raw_status`` carries it as a string. ``0`` and ``4``
have no consumer meaning and, like any value outside the map, publish
``unknown`` with a one-shot warning.

Everything here is a **pure function** — no I/O, no Home Assistant objects —
so the carrier-specific mapping stays unit-testable without Home Assistant.
"""

from __future__ import annotations

import logging

from ..const import NEW_ISSUE_URL, ParcelStatus

_LOGGER = logging.getLogger(__name__)

_overlap_warned = False
_unmapped_steps_logged: set[str] = set()


_STEP_SECTION_MAP: dict[str, ParcelStatus] = {
    "1": ParcelStatus.AT_PICKUP_POINT,
    "2": ParcelStatus.IN_TRANSIT,
    "3": ParcelStatus.DELIVERED,
}


def _warn_unmapped_step(step: str) -> None:
    if step in _unmapped_steps_logged:
        return
    _unmapped_steps_logged.add(step)
    _LOGGER.warning(
        "Unrecognised Mondial Relay stepSection — help us map it. Open an "
        "issue and paste this line: %s\n  stepSection=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        step,
    )


def dedupe_by_shipment_uid(raw_items: list[dict]) -> list[dict]:
    """Drop duplicate ``expedition.shipmentUid`` entries, keeping the first.

    Applied within one list only (received or shipped) — a UID repeated
    *across* both lists is a different situation, handled by
    :func:`warn_inbox_overlap_once`.
    """
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        uid = (item.get("expedition") or {}).get("shipmentUid")
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        deduped.append(item)
    return deduped


def warn_inbox_overlap_once(overlap_count: int) -> None:
    """One-shot, value-free warning when a shipment is listed both ways.

    The plan calls for retaining the server's own buckets rather than
    reconciling them ourselves, but a shipment counted as both incoming and
    outgoing is still worth a single heads-up per session.
    """
    global _overlap_warned  # noqa: PLW0603 - one-shot warning flag, reset by tests
    if _overlap_warned or not overlap_count:
        return
    _overlap_warned = True
    _LOGGER.warning(
        "Mondial Relay listed %d shipment(s) as both incoming and outgoing; "
        "keeping each in whichever list the server returned it in.",
        overlap_count,
    )


def _barcode(expedition: dict) -> str | None:
    shipment_id = expedition.get("shipmentId")
    if shipment_id not in (None, ""):
        return str(shipment_id)
    return expedition.get("tracingCode") or None


def normalize_parcel(raw: dict) -> dict:
    """Return a carrier-agnostic parcel dict for one ``expedition``/``delivery`` item.

    The optional fields stay ``None``/``False`` until real data settles the
    detail payload and what ``locker`` means for the ``pickup`` flag.
    """
    expedition = raw.get("expedition") or {}
    step_section = expedition.get("stepSection")
    raw_status = str(step_section) if step_section is not None else None
    status = _STEP_SECTION_MAP.get(raw_status or "", ParcelStatus.UNKNOWN)
    if status is ParcelStatus.UNKNOWN and raw_status is not None:
        _warn_unmapped_step(raw_status)
    delivered = status is ParcelStatus.DELIVERED
    if not delivered and expedition.get("hasProblem") is True:
        status = ParcelStatus.PROBLEM

    return {
        "carrier": "Mondial Relay",
        "barcode": _barcode(expedition),
        "sender": expedition.get("brandLabel") or None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        # The latest tracing event of a delivered parcel is its delivery.
        "delivered_at": expedition.get("tracingDate") if delivered else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": None,
        "weight": None,
        "dimensions": None,
        "history": None,
        "raw": raw,
    }
