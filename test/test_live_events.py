"""Tests for the Data API live events payload and SSE parsing."""

import datetime as dt
from collections.abc import AsyncIterator, Iterable

import pytest

from tibber.live_events import LiveMeasurement, SseEvent, iter_sse_events, parse_live_measurement


async def _lines(items: Iterable[bytes]) -> AsyncIterator[bytes]:
    for item in items:
        yield item


async def _collect(items: Iterable[bytes]) -> list[SseEvent]:
    return [event async for event in iter_sse_events(_lines(items))]


def test_parse_full_measurement() -> None:
    """Every documented field is mapped to its attribute."""
    measurement = parse_live_measurement(
        {
            "deviceId": "dev-1",
            "timestamp": "2026-04-27T15:34:30+02:00",
            "power": 1500.5,
            "powerProduction": 10,
            "minPower": 100,
            "averagePower": 800.25,
            "maxPower": 3000,
            "minPowerProduction": 1,
            "maxPowerProduction": 20,
            "accumulatedConsumption": 12.5,
            "accumulatedProduction": 2.5,
            "accumulatedConsumptionLastHour": 0.75,
            "accumulatedProductionLastHour": 0.25,
            "accumulatedCost": 30.5,
            "accumulatedReward": 1.5,
            "currency": "EUR",
            "lastMeterConsumption": 12345.5,
            "lastMeterProduction": 678.5,
            "voltageL1": 230.1,
            "voltageL2": 231.2,
            "voltageL3": 232.3,
            "currentL1": 1.1,
            "currentL2": -2.2,
            "currentL3": 3.3,
            "powerFactor": 0.95,
            "signalStrength": -71,
        },
    )
    assert measurement is not None
    assert measurement.device_id == "dev-1"
    assert measurement.timestamp == dt.datetime(2026, 4, 27, 13, 34, 30, tzinfo=dt.UTC)
    assert measurement.power == 1500.5
    assert measurement.power_production == 10.0
    assert measurement.min_power == 100.0
    assert measurement.average_power == 800.25
    assert measurement.max_power == 3000.0
    assert measurement.min_power_production == 1.0
    assert measurement.max_power_production == 20.0
    assert measurement.accumulated_consumption == 12.5
    assert measurement.accumulated_production == 2.5
    assert measurement.accumulated_consumption_last_hour == 0.75
    assert measurement.accumulated_production_last_hour == 0.25
    assert measurement.accumulated_cost == 30.5
    assert measurement.accumulated_reward == 1.5
    assert measurement.currency == "EUR"
    assert measurement.last_meter_consumption == 12345.5
    assert measurement.last_meter_production == 678.5
    assert measurement.voltage_l1 == 230.1
    assert measurement.voltage_l2 == 231.2
    assert measurement.voltage_l3 == 232.3
    assert measurement.current_l1 == 1.1
    assert measurement.current_l2 == -2.2
    assert measurement.current_l3 == 3.3
    assert measurement.power_factor == 0.95
    assert measurement.signal_strength == -71
    assert isinstance(measurement.signal_strength, int)
    assert measurement.estimated_hour_consumption is None


def test_parse_numeric_strings() -> None:
    """Numbers may arrive as numeric strings."""
    measurement = parse_live_measurement({"deviceId": "d", "power": "275.5", "signalStrength": "-71"})
    assert measurement is not None
    assert measurement.power == 275.5
    assert measurement.signal_strength == -71


def test_parse_rejects_bool_and_garbage() -> None:
    """Booleans, junk strings and nulls become None."""
    measurement = parse_live_measurement({"deviceId": "d", "power": True, "voltageL1": "abc", "currentL1": None})
    assert measurement is not None
    assert measurement.power is None
    assert measurement.voltage_l1 is None
    assert measurement.current_l1 is None


def test_parse_ignores_unknown_fields() -> None:
    """Unknown fields do not affect parsing."""
    assert parse_live_measurement({"deviceId": "d", "powerReactive": 5, "internalFoo": 1}) == LiveMeasurement(
        device_id="d",
    )


@pytest.mark.parametrize("data", [{}, {"deviceId": ""}, {"deviceId": 3}, None, []])
def test_parse_requires_device_id(data: object) -> None:
    """A payload without a non-empty string deviceId is rejected."""
    assert parse_live_measurement(data) is None


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-04-27T15:34:30"])
def test_parse_bad_or_naive_timestamp(timestamp: str) -> None:
    """Unparseable or offset-less timestamps become None."""
    measurement = parse_live_measurement({"deviceId": "d", "timestamp": timestamp})
    assert measurement is not None
    assert measurement.timestamp is None


async def test_sse_measurement_and_ping() -> None:
    """Named events are dispatched on blank lines."""
    events = await _collect([b"event: ping\n", b"\n", b"event: measurement\n", b'data: {"a":1}\n', b"\n"])
    assert events == [SseEvent("ping", ""), SseEvent("measurement", '{"a":1}')]


async def test_sse_multiline_crlf_comments() -> None:
    """CRLF endings, comments, multi-line data and unknown fields are handled."""
    events = await _collect([b": keepalive\r\n", b"data: a\r\n", b"data:b\r\n", b"id: 7\r\n", b"\r\n"])
    assert events == [SseEvent("message", "a\nb")]


async def test_sse_discards_unterminated_event_at_eof() -> None:
    """An event without its terminating blank line is dropped."""
    assert await _collect([b"event: measurement\n", b"data: x\n"]) == []
