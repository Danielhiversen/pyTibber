"""Shared test fixtures."""

from __future__ import annotations

import datetime as dt
from typing import ClassVar, Self

import pytest

FROZEN_NOW = dt.datetime(2026, 5, 6, 2, 30, 0, tzinfo=dt.UTC)


class FixedDateTime(dt.datetime):
    """Controllable datetime for deterministic time dependent behaviour."""

    current: ClassVar[dt.datetime] = FROZEN_NOW

    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> Self:
        if tz is None:
            return cls(
                cls.current.year,
                cls.current.month,
                cls.current.day,
                cls.current.hour,
                cls.current.minute,
                cls.current.second,
                cls.current.microsecond,
            )
        return cls.fromtimestamp(cls.current.timestamp(), tz=tz)


@pytest.fixture
def frozen_clock() -> type[FixedDateTime]:
    """Return a controllable datetime subclass, freshly built for each test.

    Install it with `patch("tibber.home.dt.datetime", frozen_clock)` and advance time by assigning
    `frozen_clock.current`. Building a new subclass per test gives each test its own `current`, so
    that the class level state cannot leak between tests.

    Use whole second values, so that arithmetic against a timedelta compares exactly. The patch
    target resolves to the real datetime module, so keep the `with` block as narrow as possible.
    """

    class FreshClock(FixedDateTime):
        """A per test clock, so that `current` is not shared with any other test."""

        current: ClassVar[dt.datetime] = FROZEN_NOW

    return FreshClock
