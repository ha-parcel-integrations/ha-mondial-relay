"""Sample Mondial Relay BFF payloads shared by the test modules.

Every list item is an ``{"expedition": ..., "delivery": ...}`` pair — there is
no confirmed ``detail``/``sender``/``recipient`` at the list level. Kept in
one module rather than inline in each test — when the payload shape turns out
to be different from what was assumed here, there is exactly one place to fix.
"""
from __future__ import annotations

ACTIVE_UID = "shp-active-0001"
ACTIVE_CODE = "12345678"
ACTIVE_TRACING = "06180799999999"
DELIVERED_UID = "shp-delivered-0001"
DELIVERED_CODE = "87654321"
DELIVERED_TRACING = "06180712345678"


def expedition(
    *,
    shipment_uid: str = ACTIVE_UID,
    shipment_id: int = int(ACTIVE_CODE),
    tracing_code: str = ACTIVE_TRACING,
    step_section: int = 2,
    brand_label: str | None = "V1VINTNL",
    locker: bool = True,
    has_problem: bool = False,
) -> dict:
    """One ``expedition`` block as returned by either list endpoint."""
    return {
        "shipmentId": shipment_id,
        "shipmentUid": shipment_uid,
        "tracingCode": tracing_code,
        "tracingSubCode": "01",
        "tracingDate": "2026-04-27T23:03:58Z",
        "stepHint": "IN_TRANSIT",
        "stepPictoName": "picto_in_transit",
        "brandLabel": brand_label,
        "markAlphaCode": "AB",
        "markNumCode": 1,
        "numberOfParcels": 1,
        "hasProblem": has_problem,
        "hasBeenReplaced": False,
        "shouldBeReplaced": False,
        "locker": locker,
        "stepSection": step_section,
    }


def delivery(*, country: str = "NL") -> dict:
    """One ``delivery`` block as returned by either list endpoint."""
    return {
        "agencyCode": 12,
        "availabilityDate": "2026-04-29T08:00:00Z",
        "code": "24R",
        "country": country,
        "deadline": "2026-05-05T23:59:59Z",
        "deliveryPointId": 456,
        "retentionPeriodDays": 7,
    }


def list_item(
    *,
    shipment_uid: str = ACTIVE_UID,
    shipment_id: int = int(ACTIVE_CODE),
    tracing_code: str = ACTIVE_TRACING,
    step_section: int = 2,
    brand_label: str | None = "V1VINTNL",
    locker: bool = True,
    has_problem: bool = False,
    country: str = "NL",
) -> dict:
    """One raw ``parcels-list-*`` item: ``{"expedition": ..., "delivery": ...}``."""
    return {
        "expedition": expedition(
            shipment_uid=shipment_uid,
            shipment_id=shipment_id,
            tracing_code=tracing_code,
            step_section=step_section,
            brand_label=brand_label,
            locker=locker,
            has_problem=has_problem,
        ),
        "delivery": delivery(country=country),
    }


def active_item(shipment_uid: str = ACTIVE_UID, shipment_id: int = int(ACTIVE_CODE)) -> dict:
    """A parcel still in transit."""
    return list_item(
        shipment_uid=shipment_uid, shipment_id=shipment_id, tracing_code=ACTIVE_TRACING
    )


def delivered_item(
    shipment_uid: str = DELIVERED_UID, shipment_id: int = int(DELIVERED_CODE)
) -> dict:
    """A delivered parcel, distinct UID/shipment ID."""
    return list_item(
        shipment_uid=shipment_uid,
        shipment_id=shipment_id,
        tracing_code=DELIVERED_TRACING,
        step_section=3,
    )


def list_envelope(items: list[dict], *, page_index: int = 0, total_pages: int = 1) -> dict:
    """The confirmed paginated envelope both list endpoints share."""
    return {
        "list": items,
        "pageIndex": page_index,
        "totalPages": total_pages,
        "elementsPerPage": len(items),
        "totalElements": len(items),
    }
