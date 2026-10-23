"""Tests for the track_parcel / untrack_parcel services."""

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mondial_relay.const import (
    CONF_ACCOUNT_SUBJECT,
    CONF_PARCELS,
    CONF_POSTAL_CODE,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)
from custom_components.mondial_relay.services import (
    async_setup_services,
    async_unload_services,
)


def _hub(hass, postal_code: str, codes=()) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Mondial Relay ({postal_code})",
        unique_id=f"tracking:{postal_code}",
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={
            CONF_POSTAL_CODE: postal_code,
            CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _account(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Mondial Relay",
        unique_id="subject-1",
        data={CONF_ACCOUNT_SUBJECT: "subject-1"},
    )
    entry.add_to_hass(hass)
    return entry


def _codes(entry: MockConfigEntry) -> list[str]:
    return [p[CONF_TRACKING_CODE] for p in entry.options[CONF_PARCELS]]


async def _call(hass, service: str, **data) -> None:
    await hass.services.async_call(DOMAIN, service, data, blocking=True)


@pytest.fixture(autouse=True)
def services(hass):
    async_setup_services(hass)
    # Registering twice is a no-op.
    async_setup_services(hass)


async def test_one_tracking_hub_is_the_implicit_target(hass):
    _account(hass)
    hub = _hub(hass, "1000")
    await _call(hass, "track_parcel", tracking_code=" 12345678 ")
    assert _codes(hub) == ["12345678"]


async def test_tracking_a_code_twice_is_a_no_op(hass):
    hub = _hub(hass, "1000", codes=("12345678",))
    await _call(hass, "track_parcel", tracking_code="12345678")
    assert _codes(hub) == ["12345678"]


async def test_several_hubs_need_a_postal_code(hass):
    first, second = _hub(hass, "1000"), _hub(hass, "1234 ab")
    with pytest.raises(ServiceValidationError, match="postal_code"):
        await _call(hass, "track_parcel", tracking_code="12345678")

    await _call(hass, "track_parcel", tracking_code="12345678", postal_code=" 1234 ab ")
    assert _codes(first) == []
    assert _codes(second) == ["12345678"]


async def test_an_unknown_postal_code_is_refused(hass):
    _hub(hass, "1000")
    with pytest.raises(ServiceValidationError, match="2000"):
        await _call(hass, "track_parcel", tracking_code="12345678", postal_code="2000")


async def test_no_tracking_hub_is_an_error_and_an_account_is_never_a_target(hass):
    account = _account(hass)
    with pytest.raises(ServiceValidationError, match="tracking hub"):
        await _call(hass, "track_parcel", tracking_code="12345678")
    with pytest.raises(ServiceValidationError, match="tracking hub"):
        await _call(hass, "untrack_parcel", tracking_code="12345678")
    assert CONF_PARCELS not in account.options


async def test_an_empty_code_is_refused(hass):
    _hub(hass, "1000")
    with pytest.raises(ServiceValidationError, match="tracking code"):
        await _call(hass, "track_parcel", tracking_code="  ")


async def test_untrack_without_a_postal_code_removes_it_everywhere(hass):
    first = _hub(hass, "1000", codes=("12345678", "87654321"))
    second = _hub(hass, "2000", codes=("12345678",))
    await _call(hass, "untrack_parcel", tracking_code="12345678")
    assert _codes(first) == ["87654321"]
    assert _codes(second) == []


async def test_untrack_with_a_postal_code_removes_it_from_that_hub_only(hass):
    first = _hub(hass, "1000", codes=("12345678",))
    second = _hub(hass, "2000", codes=("12345678",))
    await _call(hass, "untrack_parcel", tracking_code="12345678", postal_code="2000")
    assert _codes(first) == ["12345678"]
    assert _codes(second) == []


async def test_unload_removes_both_services(hass):
    async_unload_services(hass)
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")
