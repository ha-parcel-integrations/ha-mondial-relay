"""Tests for update_contents routing, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_DEVICE_UID,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.mondial_relay.services import async_setup_services

from .payloads import ACTIVE_CODE, DELIVERED_CODE, active_item, delivered_item
from .tracking.fake_client import FakeClient
from .tracking.payloads import POSTAL_CODE, TRACKING_CODE, available

ACCOUNT_CLIENT = "custom_components.mondial_relay.account.api.MondialRelayApiClient"
TRACKING_CLIENT = "custom_components.mondial_relay.MondialRelayTrackingClient"
UNTRACKED = "99999999"
HUB_ONLY = "55555555"


@contextmanager
def _polls():
    with (
        patch(
            TRACKING_CLIENT,
            side_effect=lambda *a: FakeClient({TRACKING_CODE: available()}),
        ),
        patch(
            f"{ACCOUNT_CLIENT}.async_get_list_received",
            new=AsyncMock(return_value=[active_item(), delivered_item()]),
        ),
        patch(
            f"{ACCOUNT_CLIENT}.async_get_list_shipped", new=AsyncMock(return_value=[])
        ),
    ):
        yield


async def _setup(hass, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _hub(hass, postal_code=POSTAL_CODE, codes=(TRACKING_CODE,)):
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            title=f"Mondial Relay ({postal_code})",
            unique_id=f"tracking:{postal_code}",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_POSTAL_CODE: postal_code,
                CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
            },
        ),
    )


async def _account(hass, subject="subject-1"):
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            title="Mondial Relay",
            unique_id=subject,
            data={
                CONF_REFRESH_TOKEN: "refresh-token-1",
                CONF_ACCOUNT_SUBJECT: subject,
                CONF_DEVICE_UID: "device-1",
            },
        ),
    )


async def _call(hass, service: str, data: dict) -> None:
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry, code):
    coordinator = entry.runtime_data.coordinator
    parcels = {p["barcode"]: p for p in coordinator.data + coordinator.delivered}
    return parcels[code]["contents"]


def _store(entry):
    return entry.runtime_data.coordinator.contents_store


def _update(code, contents=None, **extra):
    data = {CONF_TRACKING_CODE: code, **extra}
    if contents is not None:
        data["contents"] = contents
    return data


async def test_a_hub_parcel_has_no_contents_without_a_stored_value(hass):
    hub = await _hub(hass)
    assert _contents(hub, TRACKING_CODE) is None


async def test_update_contents_sets_replaces_and_clears_on_a_hub(hass):
    hub = await _hub(hass)

    await _call(hass, "update_contents", _update(TRACKING_CODE, " Running  shoes "))
    assert _contents(hub, TRACKING_CODE) == "Running shoes"

    await _call(hass, "update_contents", _update(TRACKING_CODE, "Winter socks"))
    assert _contents(hub, TRACKING_CODE) == "Winter socks"

    await _call(hass, "update_contents", _update(TRACKING_CODE, "  "))
    assert _contents(hub, TRACKING_CODE) is None

    await _call(hass, "update_contents", _update(TRACKING_CODE, "Winter socks"))
    await _call(hass, "update_contents", _update(TRACKING_CODE))
    assert _contents(hub, TRACKING_CODE) is None


async def test_update_contents_sets_replaces_and_clears_on_an_account(hass):
    account = await _account(hass)

    await _call(hass, "update_contents", _update(ACTIVE_CODE, "Running shoes"))
    await _call(hass, "update_contents", _update(DELIVERED_CODE, "Old books"))
    assert _contents(account, ACTIVE_CODE) == "Running shoes"
    assert _store(account).get(DELIVERED_CODE) == "Old books"

    await _call(hass, "update_contents", _update(ACTIVE_CODE, "Socks"))
    assert _contents(account, ACTIVE_CODE) == "Socks"
    await _call(hass, "update_contents", _update(ACTIVE_CODE))
    assert _contents(account, ACTIVE_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(f" {TRACKING_CODE.lower()} ", "Shoes"))
    assert _contents(hub, TRACKING_CODE) == "Shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(UNTRACKED, "Shoes"))
    assert _store(hub).get(UNTRACKED) == "Shoes"


async def test_an_unchanged_value_writes_nothing_and_requests_no_refresh(hass):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
    coordinator = hub.runtime_data.coordinator
    with (
        patch.object(coordinator, "async_request_refresh", new=AsyncMock()) as refresh,
        patch.object(_store(hub)._store, "async_delay_save") as save,
    ):
        await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
        await _call(hass, "update_contents", _update(UNTRACKED))
    refresh.assert_not_called()
    save.assert_not_called()


async def test_update_contents_rejects_an_empty_code(hass):
    await _hub(hass)
    with pytest.raises(ServiceValidationError, match="tracking code"):
        await _call(hass, "update_contents", _update("  ", "Shoes"))


async def test_update_contents_needs_a_loaded_entry(hass):
    hub = await _hub(hass)
    async_setup_services(hass)
    assert await hass.config_entries.async_unload(hub.entry_id)
    async_setup_services(hass)  # a stale registration must still refuse cleanly
    with pytest.raises(ServiceValidationError, match="not set up"):
        await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))


async def test_a_single_loaded_entry_is_targeted_directly(hass):
    account = await _account(hass)
    await _call(hass, "update_contents", _update(UNTRACKED, "Shoes"))
    assert _store(account).get(UNTRACKED) == "Shoes"


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    hub = await _hub(hass, codes=(HUB_ONLY,))
    account = await _account(hass)

    await _call(hass, "update_contents", _update(HUB_ONLY, "Hub shoes"))
    assert _store(hub).get(HUB_ONLY) == "Hub shoes"
    assert _store(account).get(HUB_ONLY) is None

    await _call(hass, "update_contents", _update(ACTIVE_CODE, "Account shoes"))
    assert _store(account).get(ACTIVE_CODE) == "Account shoes"
    assert _store(hub).get(ACTIVE_CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    first = await _hub(hass, "1000", codes=(ACTIVE_CODE,))
    second = await _hub(hass, "2000", codes=(ACTIVE_CODE,))
    account = await _account(hass)

    await _call(hass, "update_contents", _update(ACTIVE_CODE, "Shoes"))
    for entry in (first, second, account):
        assert _store(entry).get(ACTIVE_CODE) == "Shoes"


async def test_several_entries_and_no_match_need_an_entry(hass):
    hub = await _hub(hass)
    account = await _account(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "update_contents", _update(UNTRACKED, "Shoes"))
    assert err.value.translation_key == "update_contents_entry_required"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(account).get(UNTRACKED) is None


async def test_config_entry_id_targets_one_entry_even_for_an_unknown_code(hass):
    hub = await _hub(hass)
    account = await _account(hass)
    await _call(
        hass,
        "update_contents",
        _update(UNTRACKED, "Shoes", config_entry_id=account.entry_id),
    )
    assert _store(account).get(UNTRACKED) == "Shoes"
    assert _store(hub).get(UNTRACKED) is None


async def test_config_entry_id_restricts_a_code_listed_twice(hass):
    first = await _hub(hass, "1000", codes=(ACTIVE_CODE,))
    second = await _hub(hass, "2000", codes=(ACTIVE_CODE,))
    await _call(
        hass,
        "update_contents",
        _update(ACTIVE_CODE, "Shoes", config_entry_id=second.entry_id),
    )
    assert _store(second).get(ACTIVE_CODE) == "Shoes"
    assert _store(first).get(ACTIVE_CODE) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _hub(hass)
    with pytest.raises(ServiceValidationError, match="not a loaded"):
        await _call(
            hass,
            "update_contents",
            _update(TRACKING_CODE, "Shoes", config_entry_id="nope"),
        )


async def test_postal_code_is_not_an_update_contents_field(hass):
    await _hub(hass)
    with pytest.raises(Exception, match="postal_code"):
        await _call(
            hass, "update_contents", _update(TRACKING_CODE, "Shoes", postal_code="1000")
        )


async def test_an_account_alone_registers_only_update_contents(hass):
    await _account(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")


async def test_a_hub_after_an_account_still_registers_the_hub_services(hass):
    await _account(hass)
    await _hub(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    hub = await _hub(hass)
    account = await _account(hass)

    assert await hass.config_entries.async_unload(hub.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_an_account_keeps_the_hub_services(hass):
    hub = await _hub(hass)
    account = await _account(hass)
    assert await hass.config_entries.async_unload(account.entry_id)
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert hub.state is ConfigEntryState.LOADED


async def test_track_parcel_stores_contents_for_a_new_code(hass):
    hub = await _hub(hass, codes=())
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: TRACKING_CODE, "contents": "Shoes"},
        )
    assert _store(hub).get(TRACKING_CODE) == "Shoes"
    assert [p[CONF_TRACKING_CODE] for p in hub.options[CONF_PARCELS]] == [TRACKING_CODE]


async def test_track_parcel_without_contents_stores_nothing(hass):
    hub = await _hub(hass, codes=())
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: TRACKING_CODE})
    assert _store(hub).get(TRACKING_CODE) is None


async def test_track_parcel_sets_and_clears_contents_of_a_tracked_code(hass):
    hub = await _hub(hass)
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: TRACKING_CODE, "contents": "Shoes"}
    )
    assert _contents(hub, TRACKING_CODE) == "Shoes"
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: TRACKING_CODE, "contents": ""}
    )
    assert _contents(hub, TRACKING_CODE) is None


async def test_track_parcel_unchanged_contents_requests_no_refresh(hass):
    hub = await _hub(hass)
    await _call(
        hass, "track_parcel", {CONF_TRACKING_CODE: TRACKING_CODE, "contents": "Shoes"}
    )
    with patch.object(
        hub.runtime_data.coordinator, "async_request_refresh", new=AsyncMock()
    ) as refresh:
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: TRACKING_CODE, "contents": "Shoes"},
        )
    refresh.assert_not_called()


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    hub = await _hub(hass, codes=())
    assert await hass.config_entries.async_unload(hub.entry_id)
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: TRACKING_CODE, "contents": "Shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: TRACKING_CODE})
    assert _store(hub).get(TRACKING_CODE) is None


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    hub = await _hub(hass)
    other = await _hub(hass, "2000")
    assert await hass.config_entries.async_unload(other.entry_id)
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: TRACKING_CODE})
    assert hub.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()

    with _polls():
        assert await hass.config_entries.async_reload(hub.entry_id)
        await hass.async_block_till_done()
    assert _contents(hub, TRACKING_CODE) == "Shoes"


async def test_user_contents_overlays_the_tracking_carrier_value(hass):
    hub = await _hub(hass)
    coordinator = hub.runtime_data.coordinator
    assert coordinator.known_codes == {TRACKING_CODE}
    assert coordinator.data[0]["contents"] is None
    await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
    assert coordinator.data[0]["contents"] == "Shoes"


async def test_account_known_codes_list_every_code_the_entry_holds(hass):
    account = await _account(hass)
    assert account.runtime_data.coordinator.known_codes == {ACTIVE_CODE, DELIVERED_CODE}


async def test_removing_the_entry_removes_its_store(hass, hass_storage):
    hub = await _hub(hass)
    await _call(hass, "update_contents", _update(TRACKING_CODE, "Shoes"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    key = f"{DOMAIN}.contents.{hub.entry_id}"
    assert key in hass_storage

    await hass.config_entries.async_remove(hub.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    hub = await _hub(hass)
    assert f"{DOMAIN}.contents.{hub.entry_id}" not in hass_storage
    assert _store(hub).get(TRACKING_CODE) is None
