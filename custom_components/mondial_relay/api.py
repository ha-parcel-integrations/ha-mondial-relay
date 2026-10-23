"""Compatibility imports for the account source.

New code imports from :mod:`mondial_relay.account.api`; this module keeps the
import path from before the sources were split.
"""

from .account.api import (
    MondialRelayApiClient,
    MondialRelayApiError,
    MondialRelayAuthError,
    MondialRelaySigningRejectedError,
)

__all__ = [
    "MondialRelayApiClient",
    "MondialRelayApiError",
    "MondialRelayAuthError",
    "MondialRelaySigningRejectedError",
]
