"""Typed payloads and parsers for the Tibber Data API live events stream (Server-Sent Events)."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, AsyncIterator

MAX_LIVE_EVENT_DEVICES: Final = 5
LIVE_EVENTS_READ_TIMEOUT: Final = 60


@dataclass(frozen=True, slots=True)
class LiveEventDevice:
    """A device that can stream live events."""

    id: str
    type: str


@dataclass(frozen=True, slots=True)
class LiveMeasurement:
    """A live measurement from a single device."""

    device_id: str
    timestamp: dt.datetime | None = None
    power: float | None = None
    power_production: float | None = None
    min_power: float | None = None
    average_power: float | None = None
    max_power: float | None = None
    min_power_production: float | None = None
    max_power_production: float | None = None
    accumulated_consumption: float | None = None
    accumulated_production: float | None = None
    accumulated_consumption_last_hour: float | None = None
    accumulated_production_last_hour: float | None = None
    accumulated_cost: float | None = None
    accumulated_reward: float | None = None
    currency: str | None = None
    last_meter_consumption: float | None = None
    last_meter_production: float | None = None
    voltage_l1: float | None = None
    voltage_l2: float | None = None
    voltage_l3: float | None = None
    current_l1: float | None = None
    current_l2: float | None = None
    current_l3: float | None = None
    power_factor: float | None = None
    signal_strength: int | None = None
    estimated_hour_consumption: float | None = None


@dataclass(frozen=True, slots=True)
class SseEvent:
    """A dispatched Server-Sent Event."""

    event: str
    data: str


def _parse_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_int(value: object) -> int | None:
    number = _parse_number(value)
    return None if number is None else int(number)


def _parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


# Fields that pyTibber parses or derives itself; every other field is a number under its camelCase name.
_SPECIAL_FIELDS: Final = frozenset(
    {"device_id", "timestamp", "currency", "signal_strength", "estimated_hour_consumption"},
)


def _camel_case(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.capitalize() for part in rest)


_NUMBER_FIELDS: Final = tuple(
    (field.name, _camel_case(field.name)) for field in fields(LiveMeasurement) if field.name not in _SPECIAL_FIELDS
)


def parse_live_measurement(data: object) -> LiveMeasurement | None:
    """Parse a ``measurement`` event payload, or return None when it has no device id."""
    if not isinstance(data, dict):
        return None
    device_id = data.get("deviceId")
    if not isinstance(device_id, str) or not device_id:
        return None
    currency = data.get("currency")
    return LiveMeasurement(
        device_id=device_id,
        timestamp=_parse_timestamp(data.get("timestamp")),
        currency=currency if isinstance(currency, str) else None,
        signal_strength=_parse_int(data.get("signalStrength")),
        **{name: _parse_number(data.get(key)) for name, key in _NUMBER_FIELDS},
    )


async def iter_sse_events(lines: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]:
    """Yield events from raw SSE lines; an event still pending at end of input is discarded."""
    name = ""
    data: list[str] = []
    async for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield SseEvent(name or "message", "\n".join(data))
            name, data = "", []
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            data.append(value)
