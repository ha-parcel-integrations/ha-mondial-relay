"""The pre-split import paths must keep resolving.

`api.py`, `coordinator.py` and `parcels.py` at package root became re-export
shims when the account source moved into `account/`. Nothing in this repo
imports them any more, so only a test notices if a rename quietly breaks an
external import.
"""

from custom_components.mondial_relay import api, coordinator, parcels
from custom_components.mondial_relay.account import (
    api as account_api,
    coordinator as account_coordinator,
    parcels as account_parcels,
)


def test_root_coordinator_module_re_exports_the_account_coordinator():
    assert coordinator.MondialRelayCoordinator is (
        account_coordinator.MondialRelayCoordinator
    )


def test_root_api_module_re_exports_the_account_client():
    assert api.MondialRelayApiClient is account_api.MondialRelayApiClient
    assert api.MondialRelayApiError is account_api.MondialRelayApiError
    assert api.MondialRelayAuthError is account_api.MondialRelayAuthError
    assert (
        api.MondialRelaySigningRejectedError
        is account_api.MondialRelaySigningRejectedError
    )


def test_root_parcels_module_re_exports_the_account_normaliser():
    assert parcels.normalize_parcel is account_parcels.normalize_parcel
