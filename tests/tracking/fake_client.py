"""A stand-in for the tracking client, scripted per tracking code."""

from contextlib import asynccontextmanager

_MISSING = object()


class FakeClient:
    """A tracking client whose lookups answer from a per-code script."""

    def __init__(self, answers: dict | None = None, *, cycle_error=None) -> None:
        self.answers = answers or {}
        self.cycle_error = cycle_error
        self.cycles = 0
        self.lookups: list[tuple[str, str]] = []

    @asynccontextmanager
    async def async_cycle(self):
        self.cycles += 1
        if self.cycle_error is not None:
            raise self.cycle_error
        yield self

    async def async_get_shipment(self, code: str, postal_code: str):
        self.lookups.append((code, postal_code))
        answer = self.answers.get(code, _MISSING)
        if answer is _MISSING:
            return None
        if isinstance(answer, BaseException):
            raise answer
        return answer
