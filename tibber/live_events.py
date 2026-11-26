"""Typed payloads and parsers for the Tibber Data API live events stream (Server-Sent Events)."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
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
        power=_parse_number(data.get("power")),
        power_production=_parse_number(data.get("powerProduction")),
        min_power=_parse_number(data.get("minPower")),
        average_power=_parse_number(data.get("averagePower")),
        max_power=_parse_number(data.get("maxPower")),
        min_power_production=_parse_number(data.get("minPowerProduction")),
        max_power_production=_parse_number(data.get("maxPowerProduction")),
        accumulated_consumption=_parse_number(data.get("accumulatedConsumption")),
        accumulated_production=_parse_number(data.get("accumulatedProduction")),
        accumulated_consumption_last_hour=_parse_number(data.get("accumulatedConsumptionLastHour")),
        accumulated_production_last_hour=_parse_number(data.get("accumulatedProductionLastHour")),
        accumulated_cost=_parse_number(data.get("accumulatedCost")),
        accumulated_reward=_parse_number(data.get("accumulatedReward")),
        currency=currency if isinstance(currency, str) else None,
        last_meter_consumption=_parse_number(data.get("lastMeterConsumption")),
        last_meter_production=_parse_number(data.get("lastMeterProduction")),
        voltage_l1=_parse_number(data.get("voltageL1")),
        voltage_l2=_parse_number(data.get("voltageL2")),
        voltage_l3=_parse_number(data.get("voltageL3")),
        current_l1=_parse_number(data.get("currentL1")),
        current_l2=_parse_number(data.get("currentL2")),
        current_l3=_parse_number(data.get("currentL3")),
        power_factor=_parse_number(data.get("powerFactor")),
        signal_strength=_parse_int(data.get("signalStrength")),
    )


async def iter_sse_events(lines: AsyncIterable[bytes]) -> AsyncIterator[SseEvent]:
    """Yield events from raw SSE lines; an event still pending at end of input is discarded."""
    name = ""
    data: list[str] = []
    seen = False
    async for raw in lines:
        line = raw.decode("utf-8")
        for ending in ("\r\n", "\n", "\r"):
            if line.endswith(ending):
                line = line.removesuffix(ending)
                break
        if not line:
            if seen:
                yield SseEvent(name or "message", "\n".join(data))
            name, data, seen = "", [], False
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name, seen = value, True
        elif field == "data":
            data.append(value)
            seen = True
