"""The HA-bus event contract, shared by both parcel sources.

An account inbox and a list of tracking codes are two different fetches but
one event contract. Firing lives here rather than in either coordinator so
the two cannot drift apart on it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN, ParcelStatus


def fire_incoming_change_events(
    hass: HomeAssistant,
    parcels: list[dict],
    known_state: dict[str, ParcelStatus] | None,
    known_delivery_times: dict[str, tuple[str | None, str | None]] | None,
    device_id: str | None,
) -> None:
    """Fire registered / status-changed / delivered / delivery-time events.

    Silent while ``known_state`` is ``None`` — on the very first refresh we
    cannot know which parcels are genuinely new versus already present before
    HA started.

    The event contract, identical across the suite:

    * every payload is the full normalised parcel plus ``device_id``;
    * the hop **to** ``delivered`` fires only ``_parcel_delivered``, never
      also ``_parcel_status_changed``;
    * a barcode first seen already-delivered fires nothing;
    * ``registered`` only fires for a new, not-yet-delivered barcode;
    * an ETA going ``value → null`` is intentionally silent — the carrier
      just lost the window, which is not worth waking someone up for.
    """
    if known_state is None:
        return

    known_times = known_delivery_times or {}

    for parcel in parcels:
        barcode = parcel.get("barcode")
        if not barcode:
            continue
        new_status = parcel["status"]
        if barcode not in known_state:
            if new_status != ParcelStatus.DELIVERED:
                hass.bus.async_fire(
                    f"{DOMAIN}_parcel_registered",
                    {**parcel, "device_id": device_id},
                )
            continue

        if known_state[barcode] != new_status:
            if new_status == ParcelStatus.DELIVERED:
                hass.bus.async_fire(
                    f"{DOMAIN}_parcel_delivered",
                    {**parcel, "device_id": device_id},
                )
            else:
                hass.bus.async_fire(
                    f"{DOMAIN}_parcel_status_changed",
                    {
                        **parcel,
                        "device_id": device_id,
                        "old_status": known_state[barcode],
                        "new_status": new_status,
                    },
                )

        old_from, old_to = known_times.get(barcode, (None, None))
        new_from = parcel.get("planned_from")
        new_to = parcel.get("planned_to")
        from_changed = new_from is not None and new_from != old_from
        to_changed = new_to is not None and new_to != old_to
        if from_changed or to_changed:
            hass.bus.async_fire(
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


def fire_outgoing_change_events(
    hass: HomeAssistant,
    parcels: list[dict],
    known_state: dict[str, ParcelStatus] | None,
    device_id: str | None,
) -> None:
    """Fire status/delivered events for outgoing parcels.

    Silent on the very first refresh, matching the incoming events. There is
    no outgoing ``registered`` or ``delivery_time_changed`` event — those are
    intentionally out of scope.
    """
    if known_state is None:
        return

    for parcel in parcels:
        barcode = parcel.get("barcode")
        if not barcode or barcode not in known_state:
            continue
        old_status = known_state[barcode]
        new_status = parcel["status"]
        if new_status == old_status:
            continue

        if new_status == ParcelStatus.DELIVERED:
            hass.bus.async_fire(
                f"{DOMAIN}_outgoing_parcel_delivered",
                {**parcel, "device_id": device_id},
            )
        else:
            hass.bus.async_fire(
                f"{DOMAIN}_outgoing_parcel_status_changed",
                {
                    **parcel,
                    "device_id": device_id,
                    "old_status": old_status,
                    "new_status": new_status,
                },
            )
