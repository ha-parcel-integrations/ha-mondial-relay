"""Request signing for the Mondial Relay mobile account backend."""
from __future__ import annotations

import hashlib
import time
import uuid

_TRANSPORT_SIGNING_VALUE = "VCzt4PzS8ynJE2yy7zNBiQbTE3pkncqEyvrUsCDbXupGn8yqRrPDov2FYiAVfuUx"


def _derive_api_key(param1: str, param2: str) -> str:
    inner = hashlib.sha256(
        f"{_TRANSPORT_SIGNING_VALUE}{param1}{param2}".encode()
    ).hexdigest()
    return hashlib.sha256(inner.encode()).hexdigest()


def build_signature_headers(*, device_uid: str, language: str, origin_app: str) -> dict[str, str]:
    """Return the per-request headers the backend validates alongside the token.

    ``X-MR-Param1``/``X-MR-Param2`` (a fresh UUID and epoch-second timestamp)
    are generated on every call — a signature computed once and reused would
    be rejected as stale, and reusing the nonce would make requests
    replayable.
    """
    param1 = str(uuid.uuid4())
    param2 = str(int(time.time()))
    return {
        "X-MR-Param1": param1,
        "X-MR-Param2": param2,
        "X-MR-API-KEY": _derive_api_key(param1, param2),
        "device-uid": device_uid,
        "Accept-Language": language,
        "X-OriginApp": origin_app,
    }
