"""Tests for TibberHome."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import pytest

import tibber
from tibber.data_api import PriceDetails, PriceInterval, Prices
from tibber.exceptions import (
    FatalHttpExceptionError,
    InsufficientScopeError,
    InvalidLoginError,
    LiveStreamError,
    PricesNotAvailableError,
    RateLimitExceededError,
    RealTimeConsumptionDisabledError,
    RetryableHttpExceptionError,
)
from tibber.live_events import LiveEventDevice, LiveMeasurement

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

    from .conftest import FixedDateTime

HOME_ID = "test-home-id"


def _prices_payload() -> dict[str, Any]:
    """Return a Data API prices payload."""
    return {
        "currency": "NOK",
        "items": [{"time": "2026-05-06T00:00:00+02:00", "consumption": {"price": 1.0}}],
    }


@pytest.fixture
def tibber_connection(mock_websession: MagicMock) -> tibber.Tibber:
    tibber_client = tibber.Tibber(
        access_token="test-token",
        websession=mock_websession,
        user_agent="test",
    )
    tibber_client._user_agent = "test"  # noqa: SLF001
    return tibber_client


@pytest.fixture
def mock_websession() -> MagicMock:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post = AsyncMock()
    session.request = AsyncMock(return_value=_json_response(_prices_payload()))
    return session


@pytest.fixture
def home(tibber_connection: tibber.Tibber) -> tibber.TibberHome:
    """Return a home."""
    return tibber.TibberHome(HOME_ID, tibber_connection)


def _json_response(payload: dict[str, Any]) -> MagicMock:
    """Return a mocked 200 JSON response carrying the given payload."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value=payload)
    return mock_response


async def test_rt_unsubscribe_noop_when_not_subscribed(home: tibber.TibberHome) -> None:
    """Calling rt_unsubscribe on a fresh home must not raise."""
    assert not home.rt_subscription_running
    home.rt_unsubscribe()  # should be a no-op
    assert not home.rt_subscription_running


def _price_item(time: str, price: float | None, **details: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return a Data API price item."""
    return {"time": time, "consumption": {"price": price, **details}}


def _interval(time: str, price: float) -> PriceInterval:
    return PriceInterval(start=dt.datetime.fromisoformat(time), consumption=PriceDetails(price=price))


def _quarters(start: str, count: int) -> list[PriceInterval]:
    """Return `count` consecutive 15-minute intervals from `start`, priced by their position."""
    first = dt.datetime.fromisoformat(start)
    return [_interval((first + dt.timedelta(minutes=15 * index)).isoformat(), float(index)) for index in range(count)]


async def test_update_prices_fetches_today_and_tomorrow(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Prices are fetched from the start of today, in the home's time zone, and parsed."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    mock_websession.request = AsyncMock(
        return_value=_json_response(
            {
                "currency": "EUR",
                "items": [
                    {
                        "time": "2026-05-06T00:00:00+02:00",
                        "consumption": {"price": 0.45, "subsidizedPrice": 0.0, "vatPercent": 25},
                        "production": {"price": -0.1, "thirdPartyPrice": 0.02},
                    },
                    _price_item("2026-05-06T00:15:00+02:00", 0.5),
                    _price_item("2026-05-08T00:00:00+02:00", 9.0),
                ],
                "next": None,
            },
        ),
    )

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    assert mock_websession.request.await_args.kwargs["params"] == {"since": "2026-05-05T22:00:00Z"}
    assert home.prices == [
        PriceInterval(
            start=dt.datetime.fromisoformat("2026-05-06T00:00:00+02:00"),
            consumption=PriceDetails(price=0.45, subsidized_price=0.0, vat_percent=25.0),
            production=PriceDetails(price=-0.1, third_party_price=0.02),
        ),
        _interval("2026-05-06T00:15:00+02:00", 0.5),
    ]
    assert home.currency == "EUR"
    assert home.price_unit == "EUR/kWh"


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientError("Network error"),
        InvalidLoginError(401, "Invalid token"),
        RateLimitExceededError(429, "Rate limited", "RATE_LIMITED", 1.0),
    ],
)
async def test_update_prices_raises_and_keeps_prices(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A failed price fetch must reach the caller, so it can retry, and keep the known prices."""
    known_prices = [_interval("2026-11-24T00:00:00+01:00", 0.25)]
    home.prices = known_prices
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))

    with pytest.raises(type(error)):
        await home.update_prices()

    assert home.prices == known_prices


async def test_update_prices_only_fetches_the_unknown_prices(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Published prices never change, so a later update asks for what follows the known ones."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    today = _quarters("2026-05-06T00:00:00+02:00", 4)
    yesterday = [_interval("2026-05-05T23:45:00+02:00", 9.0)]
    home.prices = yesterday + today
    new = [_interval("2026-05-06T01:00:00+02:00", 5.0)]
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=new))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    since = dt.datetime(2026, 5, 5, 23, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 5, 8, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    get_prices.assert_awaited_once_with(HOME_ID, since, end)
    assert home.prices == today + new


async def test_update_prices_fetches_nothing_when_the_prices_are_complete(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """With today's and tomorrow's prices known there is nothing to fetch, but yesterday's prices go."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    complete = _quarters("2026-05-06T00:00:00+02:00", 192)
    home.prices = [_interval("2026-05-05T23:45:00+02:00", 9.0), *complete]
    get_prices = AsyncMock()
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    get_prices.assert_not_awaited()
    assert home.prices == complete


async def test_update_prices_fetches_a_hole_in_the_known_prices_again(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A price that was missed once is fetched again, together with what follows it."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    known = _quarters("2026-05-06T00:00:00+02:00", 3)
    after_hole = [_interval("2026-05-06T01:00:00+02:00", 7.0)]
    home.prices = known + after_hole
    new = [_interval("2026-05-06T00:45:00+02:00", 3.0), *after_hole]
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=new))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    assert get_prices.await_args.args[1] == dt.datetime(2026, 5, 5, 22, 45, tzinfo=dt.UTC)
    assert home.prices == known + new


async def test_update_prices_counts_the_intervals_of_a_summer_time_change(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """The day that summer time ends has 100 intervals, and the next one to fetch comes after them."""
    try:
        tibber_connection.time_zone = ZoneInfo("Europe/Oslo")
    except ZoneInfoNotFoundError:
        pytest.skip("No time zone database")
    day = _quarters("2026-10-24T22:00:00+00:00", 100)
    home.prices = day
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=[]))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC)
        await home.update_prices()

    assert get_prices.await_args.args[1] == dt.datetime(2026, 10, 25, 23, 0, tzinfo=dt.UTC)
    assert home.prices == day


async def test_update_prices_clears_prices_when_not_available(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home without prices gets no prices instead of an error."""
    home.prices = [_interval("2026-11-24T00:00:00+01:00", 0.25)]
    error = PricesNotAvailableError(403, "No prices", "prices-not-available")
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))

    await home.update_prices()

    assert home.prices == []


async def test_get_prices_does_not_change_prices(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """Fetching a range of prices returns them without replacing the cached prices."""
    mock_websession.request = AsyncMock(
        return_value=_json_response({"items": [_price_item("2026-01-01T00:00:00+00:00", 1.0)], "next": None}),
    )
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    assert await home.get_prices(start, start + dt.timedelta(days=1)) == [
        _interval("2026-01-01T00:00:00+00:00", 1.0),
    ]
    assert home.prices == []


async def test_current_price_data(
    home: tibber.TibberHome,
    frozen_clock: type[FixedDateTime],
) -> None:
    """The current price and its rank among today's prices, where equal prices share a rank."""
    home.prices = [
        _interval(f"2026-05-06T{time}:00+00:00", price)
        for time, price in {
            "00:00": 0.0,
            "02:00": 1.0,
            "02:15": 3.0,
            "02:30": 2.0,
            "02:45": 2.0,
            "03:00": 4.0,
        }.items()
    ] + [_interval("2026-05-07T02:30:00+00:00", 0.1)]

    with patch("tibber.home.dt.datetime", frozen_clock):
        price, price_time, price_rank = home.current_price_data()
        current = home.current_price_interval

    assert current == home.prices[3]
    assert price == 2.0
    assert price_time == dt.datetime(2026, 5, 6, 2, 30, tzinfo=dt.UTC)
    assert price_rank == 3 / 6


async def test_current_price_data_without_current_interval(home: tibber.TibberHome) -> None:
    """No current interval gives no price."""
    home.prices = [_interval("2020-01-01T00:00:00+00:00", 1.0)]

    assert home.current_price_interval is None
    assert home.current_price_data() == (None, None, None)


async def test_current_attributes(
    home: tibber.TibberHome,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Today's statistics, also for negative prices, and only for today."""
    home.prices = [
        _interval("2026-05-06T01:00:00+00:00", -2.0),
        _interval("2026-05-06T09:00:00+00:00", -1.0),
        _interval("2026-05-06T21:00:00+00:00", -0.5),
        _interval("2026-05-07T09:00:00+00:00", 5.0),
    ]

    with patch("tibber.home.dt.datetime", frozen_clock):
        attributes = home.current_attributes()

    assert attributes == {
        "max_price": -0.5,
        "avg_price": round(-3.5 / 3, 4),
        "min_price": -2.0,
        "off_peak_1": -2.0,
        "peak": -1.0,
        "off_peak_2": -0.5,
    }


async def test_current_attributes_without_prices(home: tibber.TibberHome) -> None:
    """No prices today gives nothing, as zeros would look like real prices."""
    assert home.current_attributes() == {}

    home.prices = [PriceInterval(start=dt.datetime(2026, 5, 6, tzinfo=dt.UTC), production=PriceDetails(price=1.0))]
    assert home.current_attributes() == {}


@pytest.mark.parametrize(
    ("resolution", "prices", "expected"),
    [
        (
            "HOURLY",
            [
                ("2026-05-05T22:00:00Z", 1.0),
                ("2026-05-05T22:15:00Z", 2.0),
                ("2026-05-05T22:30:00Z", 3.0),
                ("2026-05-05T22:45:00Z", 4.0),
                ("2026-05-05T23:00:00Z", 6.0),
            ],
            [("2026-05-06T00:00:00+02:00", 2.5), ("2026-05-06T01:00:00+02:00", 6.0)],
        ),
        (
            "daily",
            [("2026-05-05T21:45:00Z", 1.0), ("2026-05-05T22:00:00Z", 3.0), ("2026-05-05T22:15:00Z", 5.0)],
            [("2026-05-05T00:00:00+02:00", 1.0), ("2026-05-06T00:00:00+02:00", 4.0)],
        ),
        (
            "MONTHLY",
            [("2026-04-30T21:45:00Z", 1.0), ("2026-04-30T22:00:00Z", 3.0), ("2026-05-01T22:00:00Z", 5.0)],
            [("2026-04-01T00:00:00+02:00", 1.0), ("2026-05-01T00:00:00+02:00", 4.0)],
        ),
    ],
)
async def test_get_historic_price_data_averages_the_prices_of_each_period(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolution: str,
    prices: list[tuple[str, float]],
    expected: list[tuple[str, float]],
) -> None:
    """The 15-minute prices are averaged per period of the home's time zone, and a period needs a price."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    intervals = [_interval(time, price) for time, price in prices]
    # A production price alone isn't a price to pay, so it doesn't make a period of its own.
    intervals.append(PriceInterval(start=dt.datetime(2026, 7, 1, tzinfo=dt.UTC), production=PriceDetails(price=9.0)))
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=intervals))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    start = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    entries = await home.get_historic_price_data(start, end, resolution)

    get_prices.assert_awaited_once_with(HOME_ID, start, end)
    assert entries == [{"time": time, "total": total} for time, total in expected]
    assert home.prices == []


async def test_get_historic_price_data_tells_apart_the_hours_of_a_summer_time_change(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hour that summer time ends is there twice, and must not become one period."""
    try:
        tibber_connection.time_zone = ZoneInfo("Europe/Oslo")
    except ZoneInfoNotFoundError:
        pytest.skip("No time zone database")
    intervals = [_interval("2026-10-25T00:00:00+00:00", 1.0), _interval("2026-10-25T01:00:00+00:00", 3.0)]
    monkeypatch.setattr(
        tibber_connection.data_api,
        "get_prices",
        AsyncMock(return_value=Prices(currency="NOK", intervals=intervals)),
    )
    start = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)

    entries = await home.get_historic_price_data(start, start + dt.timedelta(days=1))

    assert entries == [
        {"time": "2026-10-25T02:00:00+02:00", "total": 1.0},
        {"time": "2026-10-25T02:00:00+01:00", "total": 3.0},
    ]


async def test_get_historic_price_data_rejects_unsupported_resolutions(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Data API has no weekly or annual prices, so they aren't made up from the 15-minute ones."""
    get_prices = AsyncMock()
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    with pytest.raises(ValueError, match="Unsupported resolution WEEKLY"):
        await home.get_historic_price_data(start, start + dt.timedelta(days=30), "WEEKLY")

    get_prices.assert_not_awaited()


async def test_get_historic_price_data_raises_when_prices_are_not_available(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A range before the price history of the home is an error, not an empty list."""
    error = PricesNotAvailableError(403, "Before the history", "prices-not-available")
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))
    start = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)

    with pytest.raises(PricesNotAvailableError):
        await home.get_historic_price_data(start, start + dt.timedelta(days=1))


@pytest.mark.parametrize(("country", "currency"), [("SE", "SEK"), ("NO", "NOK"), ("DE", "EUR"), ("XX", "")])
async def test_currency_falls_back_to_country(home: tibber.TibberHome, country: str, currency: str) -> None:
    """The currency is known from the home's country before any prices are fetched."""
    home.info = {"viewer": {"home": {"address": {"country": country}}}}

    assert home.currency == currency


# Live data from the Data API.
#
# `rt_subscribe` runs the live loop in a background task, which waits between connections through
# `tibber.home._sleep`. `_sleeps` replaces that helper with a mock that records the delays without
# waiting and ends the loop by raising CancelledError. Patching `tibber.home.asyncio.sleep` instead
# would replace `asyncio.sleep` everywhere, these tests and pytest-asyncio included. `_sleeps` also
# makes the backoff deterministic by having `random.uniform` return its upper bound.

DEVICE = LiveEventDevice(id="pulse-1", type="tibber pulse")
LIVE_TIME = dt.datetime(2026, 5, 6, 12, 30, tzinfo=dt.UTC)
STAY_OPEN = None
"""A stream script item: the connection stays open without data until it is cancelled."""


def _measurement(**values: Any) -> LiveMeasurement:  # noqa: ANN401
    """Return a 1000 W measurement from the Pulse at 12:30:00Z, with `values` overridden."""
    return LiveMeasurement(**{"device_id": DEVICE.id, "timestamp": LIVE_TIME, "power": 1000.0, **values})


class _ScriptedStream:
    """A fake `stream_live_events` whose connections play scripts.

    A connection yields the measurements of its script and raises its exceptions. It ends normally
    after its last item, unless that item is `STAY_OPEN`. Connections beyond the scripts stay open.
    `closed` counts the connections that have been closed.
    """

    def __init__(self) -> None:
        self.scripts: list[list[LiveMeasurement | Exception | None]] = []
        self.device_ids: list[list[str]] = []
        self.closed = 0

    async def __call__(self, home_id: str, device_ids: Sequence[str]) -> AsyncIterator[LiveMeasurement]:
        assert home_id == HOME_ID
        self.device_ids.append(list(device_ids))
        connection = len(self.device_ids) - 1
        try:
            for item in self.scripts[connection] if connection < len(self.scripts) else [STAY_OPEN]:
                if item is STAY_OPEN:
                    await asyncio.Event().wait()
                elif isinstance(item, Exception):
                    raise item
                else:
                    yield item
        finally:
            self.closed += 1


@pytest.fixture
def discover(tibber_connection: tibber.Tibber, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Fake the live device discovery, which finds the Pulse."""
    mock = AsyncMock(return_value=[DEVICE])
    monkeypatch.setattr(tibber_connection.data_api, "get_live_event_devices", mock)
    return mock


@pytest.fixture
def stream(tibber_connection: tibber.Tibber, monkeypatch: pytest.MonkeyPatch) -> _ScriptedStream:
    """Fake the live stream with scripted connections."""
    fake = _ScriptedStream()
    monkeypatch.setattr(tibber_connection.data_api, "stream_live_events", fake)
    return fake


@contextlib.contextmanager
def _sleeps(stop_at: int) -> Iterator[AsyncMock]:
    """Record the live loop's sleeps without waiting, and end the loop at sleep number `stop_at`."""
    sleep = AsyncMock(side_effect=[*[None] * (stop_at - 1), asyncio.CancelledError])
    with patch("tibber.home._sleep", sleep), patch("tibber.home.random.uniform", lambda _low, high: high):
        yield sleep


def _delays(sleep: AsyncMock) -> list[float]:
    return [awaited.args[0] for awaited in sleep.await_args_list]


async def _run_live_loop(
    home: tibber.TibberHome,
    callback: Callable[[LiveMeasurement], None] | None = None,
    on_error: Callable[[Exception], None] | None = None,
) -> asyncio.Task[None]:
    """Subscribe, wait until the live loop has ended, and return its task."""
    await home.rt_subscribe(callback or MagicMock(), on_error=on_error)
    task = home._rt_task  # noqa: SLF001
    await asyncio.wait([task], timeout=1)
    assert task.done()
    return task


@pytest.mark.usefixtures("discover")
async def test_rt_subscribe_delivers_enriched_measurement(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """The callback gets the enriched measurement, and the subscription is running meanwhile."""
    measurement = _measurement(accumulated_consumption_last_hour=0.5)
    stream.scripts = [[measurement]]
    received: list[tuple[LiveMeasurement, bool]] = []

    def callback(delivered: LiveMeasurement) -> None:
        received.append((delivered, home.rt_subscription_running))

    with _sleeps(stop_at=1):
        task = await _run_live_loop(home, callback)

    enriched = replace(measurement, power_production=0.0, estimated_hour_consumption=1.0)
    assert received == [(enriched, True)]
    assert stream.device_ids == [[DEVICE.id]]
    assert home.live_event_devices == [DEVICE]
    assert task.cancelled()
    assert not home.rt_subscription_running


@pytest.mark.usefixtures("discover", "stream")
async def test_rt_subscribe_twice_raises(home: tibber.TibberHome) -> None:
    """Subscribing while subscribed raises, and after rt_unsubscribe a new subscription starts at once."""
    await home.rt_subscribe(MagicMock())

    with pytest.raises(RuntimeError, match="rt_unsubscribe"):
        await home.rt_subscribe(MagicMock())

    first = home._rt_task  # noqa: SLF001
    home.rt_unsubscribe()
    await home.rt_subscribe(MagicMock())
    second = home._rt_task  # noqa: SLF001
    home.rt_unsubscribe()
    await asyncio.wait([first, second], timeout=1)
    assert first.cancelled()
    assert second.cancelled()


@pytest.mark.usefixtures("discover")
async def test_rt_unsubscribe_cancels_task(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """rt_unsubscribe stops a running subscription at once and ends the live loop."""
    delivered = asyncio.Event()
    stream.scripts = [[_measurement(), STAY_OPEN]]
    await home.rt_subscribe(lambda _: delivered.set())
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert home.rt_subscription_running
    task = home._rt_task  # noqa: SLF001

    home.rt_unsubscribe()

    assert not home.rt_subscription_running
    await asyncio.wait([task], timeout=1)
    assert task.cancelled()


async def test_rt_loop_caps_devices_at_five(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only the first five live devices are streamed, with a warning about the rest."""
    devices = [LiveEventDevice(id=f"pulse-{n}", type="tibber pulse") for n in range(7)]
    discover.return_value = devices
    stream.scripts = [[]]

    with caplog.at_level(logging.WARNING), _sleeps(stop_at=1):
        await _run_live_loop(home)

    assert stream.device_ids == [[device.id for device in devices[:5]]]
    assert home.live_event_devices == devices
    assert "has 7 live devices, streaming the first 5" in caplog.text


async def test_rt_loop_reconnects_after_stream_end(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
) -> None:
    """A stream that ends is discovered and connected again, with a growing backoff."""
    stream.scripts = [[], []]

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home)

    assert _delays(sleep) == [1 + 1, 1 + 2]
    assert discover.await_count == 2
    assert len(stream.device_ids) == 2


@pytest.mark.usefixtures("discover")
@pytest.mark.parametrize(
    "error",
    [TimeoutError(), aiohttp.ClientConnectionError(), RetryableHttpExceptionError(503, "Service Unavailable")],
    ids=["timeout", "connection", "5xx"],
)
async def test_rt_loop_reconnects_after_timeout(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    error: Exception,
) -> None:
    """A timeout, connection error or 5xx is reported, and the loop backs off and reconnects."""
    stream.scripts = [[error], [_measurement()]]
    callback = MagicMock()
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, callback, errors.append)

    assert errors == [error]
    callback.assert_called_once()
    # The measurement of the second connection resets the backoff.
    assert _delays(sleep) == [1 + 1, 1 + 1]


@pytest.mark.usefixtures("discover")
@pytest.mark.parametrize(
    ("first_connection", "measurements"),
    [([_measurement(), STAY_OPEN], 2), ([STAY_OPEN], 1)],
    ids=["after_measurement", "before_first_measurement"],
)
async def test_rt_loop_reconnects_without_measurements(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    first_connection: list[LiveMeasurement | Exception | None],
    measurements: int,
) -> None:
    """A stream that stays open without measurements, such as one with only pings, times out and reconnects."""
    stream.scripts = [first_connection, [_measurement()]]
    callback = MagicMock()
    errors: list[tuple[Exception, bool, int]] = []

    def on_error(error: Exception) -> None:
        errors.append((error, home.rt_subscription_running, stream.closed))

    with patch("tibber.home.LIVE_NO_MEASUREMENT_TIMEOUT", 0.01), _sleeps(stop_at=2) as sleep:
        task = await _run_live_loop(home, callback, on_error)

    assert task.cancelled()
    [(error, running, closed)] = errors
    assert isinstance(error, TimeoutError)
    assert str(error) == f"No live measurement for home {HOME_ID} in 0.01 s"
    assert not running
    assert closed == 1
    assert len(stream.device_ids) == 2
    assert stream.closed == 2
    assert callback.call_count == measurements
    # The timeout backs off like any transient error, and the second connection's measurement resets the backoff.
    assert _delays(sleep) == [1 + 1, 1 + 1]


@pytest.mark.usefixtures("discover")
async def test_rt_loop_evicted_waits_long(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """An evicted stream waits five minutes before it connects again."""
    evictions = [LiveStreamError("evicted", transient=False), LiveStreamError("evicted", transient=False)]
    stream.scripts = [[evictions[0]], [evictions[1]]]
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert _delays(sleep) == [300, 300]
    assert errors == evictions


@pytest.mark.usefixtures("discover")
async def test_rt_loop_transient_uses_retry_after(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """A transient stream error waits as long as the server asks."""
    error = LiveStreamError("upstream_unavailable", transient=True, retry_after=7)
    stream.scripts = [[error]]
    errors: list[Exception] = []

    with _sleeps(stop_at=1) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert _delays(sleep) == [7]
    assert errors == [error]


async def test_rt_loop_stream_error_backs_off(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
) -> None:
    """A transient error without retry_after backs off and discovers again."""
    error = LiveStreamError("upstream_unavailable", transient=True)
    stream.scripts = [[error], []]
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert _delays(sleep) == [1 + 1, 1 + 2]
    assert discover.await_count == 2
    assert errors == [error]


@pytest.mark.usefixtures("discover")
@pytest.mark.parametrize(
    ("error", "delay"),
    [
        (LiveStreamError("evicted", transient=False, retry_after=600), 600),
        (LiveStreamError("evicted", transient=True, retry_after=600), 600),
        (LiveStreamError("evicted", transient=False, retry_after=10), 300),
        (LiveStreamError("device_not_streamable", transient=False, retry_after=42), 42),
        (LiveStreamError("invalid_request", transient=False, retry_after=30), 30),
        (LiveStreamError("invalid_request", transient=False), 900),
        (LiveStreamError("upstream_unavailable", transient=True, retry_after=0.001), 1),
        (LiveStreamError("upstream_unavailable", transient=True, retry_after=1e9), 3600),
        (LiveStreamError("upstream_unavailable", transient=True, retry_after=float("inf")), 3600),
        (LiveStreamError("upstream_unavailable", transient=True, retry_after=float("nan")), 1 + 1),
        (LiveStreamError("upstream_unavailable", transient=True, retry_after=-5), 1 + 1),
    ],
    ids=[
        "evicted_asks_longer",
        "evicted_transient_asks_longer",
        "evicted_asks_shorter",
        "device_not_streamable",
        "permanent_with_retry_after",
        "permanent_without_retry_after",
        "retry_after_too_short",
        "retry_after_too_long",
        "retry_after_infinite",
        "retry_after_nan",
        "retry_after_negative",
    ],
)
async def test_rt_loop_stream_error_delay(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    error: LiveStreamError,
    delay: float,
) -> None:
    """The server's retry_after is kept between 1 s and 1 hour, and an eviction never waits less than 5 minutes."""
    stream.scripts = [[error]]

    with _sleeps(stop_at=1) as sleep:
        await _run_live_loop(home)

    assert _delays(sleep) == [delay]


async def test_rt_loop_leaves_out_device_that_cannot_stream(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
) -> None:
    """A device the server can't stream is left out, so that the other devices stream and a further one joins."""
    devices = [LiveEventDevice(id=f"pulse-{n}", type="tibber pulse") for n in range(6)]
    discover.return_value = devices
    error = LiveStreamError("device_not_streamable", transient=False, device_id="pulse-0")
    stream.scripts = [[error], []]
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert stream.device_ids == [
        ["pulse-0", "pulse-1", "pulse-2", "pulse-3", "pulse-4"],
        [f"pulse-{n}" for n in range(1, 6)],
    ]
    assert _delays(sleep) == [1 + 1, 1 + 2]
    assert errors == [error]


async def test_rt_loop_tries_device_that_cannot_stream_again_later(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    discover: AsyncMock,
) -> None:
    """With no device left to stream, the loop reports it and waits 15 minutes, then tries the device again."""
    error = LiveStreamError("device_not_streamable", transient=False, device_id=DEVICE.id)
    stream.scripts = [[error], [_measurement()]]
    callback = MagicMock()
    errors: list[Exception] = []
    # The clock is read by the first discovery (0 s), when the device can't stream (1 s), by the second discovery,
    # which still leaves it out (2 s), and by the third discovery, which tries it again (1000 s).
    clock = MagicMock(monotonic=MagicMock(side_effect=[0, 1, 2, 1000]))

    with patch("tibber.home.time", clock), _sleeps(stop_at=3) as sleep:
        await _run_live_loop(home, callback, errors.append)

    assert errors[0] is error
    assert isinstance(errors[1], RealTimeConsumptionDisabledError)
    assert len(errors) == 2
    assert _delays(sleep) == [1 + 1, 900, 1 + 1]
    assert stream.device_ids == [[DEVICE.id], [DEVICE.id]]
    assert discover.await_count == 3
    callback.assert_called_once()


async def test_rt_loop_keeps_last_devices_when_discovery_comes_back_empty(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty discovery after a non-empty one is a hiccup of the API: the loop streams the last known devices."""
    discover.side_effect = [[DEVICE], []]
    stream.scripts = [[_measurement()], [_measurement()]]
    callback = MagicMock()
    errors: list[Exception] = []

    with caplog.at_level(logging.WARNING), _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, callback, errors.append)

    assert errors == []
    assert stream.device_ids == [[DEVICE.id], [DEVICE.id]]
    assert callback.call_count == 2
    assert _delays(sleep) == [1 + 1, 1 + 1]
    assert "using the last known ones" in caplog.text


@pytest.mark.usefixtures("discover")
async def test_rt_loop_rate_limited_uses_retry_after(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """A rate limited connect waits as long as the rate limit asks."""
    error = RateLimitExceededError(429, "Rate limited", "RATE_LIMITED", 12.5)
    stream.scripts = [[error]]
    errors: list[Exception] = []

    with _sleeps(stop_at=1) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert _delays(sleep) == [12.5]
    assert errors == [error]


async def test_rt_loop_no_devices_reports_and_retries(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
) -> None:
    """A home without live devices is reported, and discovery is tried again after 15 minutes."""
    discover.side_effect = [[], [DEVICE]]
    stream.scripts = [[]]
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        await _run_live_loop(home, on_error=errors.append)

    assert [type(error) for error in errors] == [RealTimeConsumptionDisabledError]
    assert _delays(sleep)[0] == 900
    assert discover.await_count == 2
    assert stream.device_ids == [[DEVICE.id]]


@pytest.mark.usefixtures("discover")
async def test_rt_loop_401_retries_once_then_backs_off(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """A 401 is retried at once with a fresh token, and every further 401 in a row waits 15 minutes."""
    rejections = [InvalidLoginError(401, "Invalid token") for _ in range(3)]
    stream.scripts = [[rejections[0]], [rejections[1]], [rejections[2]], [_measurement()]]
    callback = MagicMock()
    errors: list[Exception] = []
    connections_at_sleep: list[int] = []

    def record_connections(_delay: float) -> None:
        connections_at_sleep.append(len(stream.device_ids))
        if len(connections_at_sleep) == 3:
            raise asyncio.CancelledError

    with _sleeps(stop_at=3) as sleep:
        sleep.side_effect = record_connections
        task = await _run_live_loop(home, callback, errors.append)

    assert task.cancelled()
    assert errors == rejections
    # Two connects without a sleep, then 15 minutes before each further connect after a 401.
    assert connections_at_sleep == [2, 3, 4]
    assert _delays(sleep) == [900, 900, 1 + 1]
    callback.assert_called_once()


@pytest.mark.usefixtures("discover")
async def test_rt_loop_401_after_reconnect_retries_again(home: tibber.TibberHome, stream: _ScriptedStream) -> None:
    """Only 401s in a row back off, a connection in between starts the count again."""
    stream.scripts = [
        [InvalidLoginError(401, "Invalid token")],
        [_measurement()],
        [InvalidLoginError(401, "Invalid token")],
        [],
    ]

    with _sleeps(stop_at=2):
        task = await _run_live_loop(home, on_error=MagicMock())

    assert task.cancelled()
    assert len(stream.device_ids) == 4


@pytest.mark.usefixtures("discover")
@pytest.mark.parametrize("status", [400, 404, 408, 409])
async def test_rt_loop_other_4xx_retries_slowly(home: tibber.TibberHome, stream: _ScriptedStream, status: int) -> None:
    """A 4xx other than 401, 403 and 429 is reported, and the loop looks again after 15 minutes."""
    error = FatalHttpExceptionError(status, "Nope", f"HTTP_{status}")
    stream.scripts = [[error], [_measurement()]]
    callback = MagicMock()
    errors: list[Exception] = []

    with _sleeps(stop_at=2) as sleep:
        task = await _run_live_loop(home, callback, errors.append)

    assert task.cancelled()
    assert errors == [error]
    assert _delays(sleep) == [900, 1 + 1]
    callback.assert_called_once()


@pytest.mark.usefixtures("discover")
@pytest.mark.parametrize(
    "error",
    [InsufficientScopeError(403, "Forbidden", "HTTP_403"), FatalHttpExceptionError(403, "Forbidden", "HTTP_403")],
    ids=["insufficient_scope", "forbidden"],
)
async def test_rt_loop_403_stops(home: tibber.TibberHome, stream: _ScriptedStream, error: Exception) -> None:
    """A 403, which a missing scope causes and retrying can't fix, is reported and stops the loop."""
    stream.scripts = [[error]]
    errors: list[Exception] = []

    with _sleeps(stop_at=1) as sleep:
        task = await _run_live_loop(home, on_error=errors.append)

    assert not task.cancelled()
    assert task.exception() is None
    assert len(stream.device_ids) == 1
    sleep.assert_not_awaited()
    assert errors == [error]
    assert not home.rt_subscription_running

    # The stopped loop doesn't count as subscribed, so a new one can start.
    await home.rt_subscribe(MagicMock())
    restarted = home._rt_task  # noqa: SLF001
    home.rt_unsubscribe()
    await asyncio.wait([restarted], timeout=1)
    assert restarted.cancelled()


@pytest.mark.usefixtures("discover")
async def test_rt_loop_logs_errors_without_on_error(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without on_error, errors the loop recovers from are warnings, and errors that stop it are errors."""
    stream.scripts = [[TimeoutError()], [InsufficientScopeError(403, "Forbidden", "HTTP_403")]]

    with caplog.at_level(logging.WARNING), _sleeps(stop_at=2):
        task = await _run_live_loop(home)

    assert not task.cancelled()
    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.ERROR]
    assert "TimeoutError" in caplog.records[0].getMessage()
    assert "InsufficientScopeError" in caplog.records[1].getMessage()


async def test_rt_loop_unexpected_error_backs_off(
    home: tibber.TibberHome,
    discover: AsyncMock,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected error in discovery or the stream is logged with its traceback, reported and backed off."""
    discovery_error = AttributeError("malformed discovery payload")
    stream_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    discover.side_effect = [discovery_error, [DEVICE], [DEVICE]]
    stream.scripts = [[stream_error], []]
    errors: list[Exception] = []

    with caplog.at_level(logging.ERROR), _sleeps(stop_at=3) as sleep:
        task = await _run_live_loop(home, on_error=errors.append)

    assert task.cancelled()
    assert errors == [discovery_error, stream_error]
    assert _delays(sleep) == [1 + 1, 1 + 2, 1 + 4]
    assert [record.exc_info[1] for record in caplog.records] == [discovery_error, stream_error]


@pytest.mark.usefixtures("discover")
async def test_rt_loop_enrichment_error_delivers_measurement_as_received(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A measurement that can't be enriched is logged and passed on as received, without dropping the stream."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=-5))
    # The local time of the first instant of year 1 in UTC-5 is before year 1, so enriching raises an OverflowError.
    unenrichable = _measurement(timestamp=dt.datetime.min.replace(tzinfo=dt.UTC), accumulated_consumption_last_hour=0.1)
    stream.scripts = [[unenrichable, unenrichable]]
    callback = MagicMock()
    errors: list[Exception] = []

    with caplog.at_level(logging.ERROR), _sleeps(stop_at=1) as sleep:
        task = await _run_live_loop(home, callback, errors.append)

    assert task.cancelled()
    assert errors == []
    assert [call.args[0] for call in callback.call_args_list] == [unenrichable, unenrichable]
    assert caplog.text.count("Error enriching a live measurement") == 2
    # The measurements reset the backoff, and the stream then ends normally.
    assert _delays(sleep) == [1 + 1]


@pytest.mark.usefixtures("discover")
async def test_rt_callback_exception_does_not_stop_loop(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception in the callback is logged, and the next measurements still arrive."""
    stream.scripts = [[_measurement(), _measurement(power=2000.0)]]
    powers: list[float | None] = []

    def callback(measurement: LiveMeasurement) -> None:
        powers.append(measurement.power)
        if len(powers) == 1:
            raise ValueError("callback boom")

    with caplog.at_level(logging.ERROR), _sleeps(stop_at=1):
        task = await _run_live_loop(home, callback)

    assert powers == [1000.0, 2000.0]
    assert task.cancelled()
    assert "Error in rt_subscribe callback" in caplog.text


@pytest.mark.usefixtures("discover")
async def test_rt_on_error_exception_does_not_stop_loop(
    home: tibber.TibberHome,
    stream: _ScriptedStream,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception in on_error is logged, and the loop carries on."""
    stream.scripts = [[TimeoutError()], []]

    with caplog.at_level(logging.ERROR), _sleeps(stop_at=2):
        task = await _run_live_loop(home, on_error=MagicMock(side_effect=ValueError("on_error boom")))

    assert task.cancelled()
    assert len(stream.device_ids) == 2
    assert "Error in rt_subscribe on_error callback" in caplog.text


def _sse_response(*events: bytes) -> MagicMock:
    """Return a 200 response that streams the given Server-Sent Events."""

    async def content() -> AsyncIterator[bytes]:
        for event in events:
            for line in event.splitlines(keepends=True):
                yield line

    return MagicMock(status=200, content=content(), release=MagicMock())


async def test_rt_loop_refetches_token_each_connect(
    mock_websession: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every connect of the real stream gets a fresh token from the token manager."""
    refresh = AsyncMock(side_effect=["token-1", "token-2"])
    connection = tibber.Tibber(websession=mock_websession, user_agent="test", refresh_access_token=refresh)
    monkeypatch.setattr(connection.data_api, "get_live_event_devices", AsyncMock(return_value=[DEVICE]))
    event = b'event: measurement\ndata: {"deviceId": "pulse-1", "power": 100}\n\n'
    mock_websession.request = AsyncMock(side_effect=lambda *_args, **_kwargs: _sse_response(event))
    home = tibber.TibberHome(HOME_ID, connection)
    callback = MagicMock()

    with _sleeps(stop_at=2):
        await _run_live_loop(home, callback)

    assert refresh.await_count == 2
    authorizations = [awaited.kwargs["headers"]["Authorization"] for awaited in mock_websession.request.await_args_list]
    assert authorizations == ["Bearer token-1", "Bearer token-2"]
    assert callback.call_count == 2


@pytest.mark.parametrize(
    ("time_zone", "expected"),
    [(dt.UTC, 1.499), (dt.timezone(dt.timedelta(hours=5, minutes=30)), 2.499)],
    ids=["utc", "utc+0530"],
)
async def test_enrich_estimated_hour_consumption(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    time_zone: dt.tzinfo,
    expected: float,
) -> None:
    """The estimate adds the 5-minute average power for the rest of the hour in the client time zone."""
    tibber_connection.time_zone = time_zone
    home._enrich_measurement(_measurement(power=1000.0))  # noqa: SLF001
    later = _measurement(
        power=3000.0,
        timestamp=LIVE_TIME + dt.timedelta(seconds=2),
        accumulated_consumption_last_hour=0.5,
    )

    assert home._enrich_measurement(later).estimated_hour_consumption == expected  # noqa: SLF001


async def test_enrich_power_zero_when_producing(home: tibber.TibberHome) -> None:
    """A measurement that only reports production consumes nothing."""
    enriched = home._enrich_measurement(_measurement(power=None, power_production=500.0))  # noqa: SLF001

    assert enriched.power == 0.0
    assert enriched.power_production == 500.0


async def test_enrich_production_zero_when_consuming(home: tibber.TibberHome) -> None:
    """A measurement that only reports consumption produces nothing."""
    enriched = home._enrich_measurement(_measurement(power=800.0, power_production=None))  # noqa: SLF001

    assert enriched.power == 800.0
    assert enriched.power_production == 0.0


@pytest.mark.parametrize(("reported", "expected"), [(-3.0, 0.0), (12.5, 12.5), (None, None)])
async def test_enrich_clamps_last_meter_production(
    home: tibber.TibberHome,
    reported: float | None,
    expected: float | None,
) -> None:
    """A negative meter production reading becomes zero."""
    enriched = home._enrich_measurement(_measurement(last_meter_production=reported))  # noqa: SLF001

    assert enriched.last_meter_production == expected


async def test_enrich_power_window_is_per_device(home: tibber.TibberHome) -> None:
    """The estimate of a device averages only the power of that device."""
    home._enrich_measurement(_measurement(device_id="watty-1", power=1000.0))  # noqa: SLF001
    pulse = _measurement(
        power=3000.0,
        timestamp=LIVE_TIME + dt.timedelta(seconds=2),
        accumulated_consumption_last_hour=0.5,
    )

    assert home._enrich_measurement(pulse).estimated_hour_consumption == 1.998  # noqa: SLF001


async def test_enrich_power_window_drops_old_power(home: tibber.TibberHome) -> None:
    """Power older than five minutes drops out of the estimate."""
    home._enrich_measurement(_measurement(power=1000.0))  # noqa: SLF001
    later = _measurement(
        power=3000.0,
        timestamp=LIVE_TIME + dt.timedelta(minutes=5, seconds=1),
        accumulated_consumption_last_hour=0.5,
    )

    assert home._enrich_measurement(later).estimated_hour_consumption == 1.749  # noqa: SLF001


async def test_enrich_updates_peak_hour(home: tibber.TibberHome) -> None:
    """A current hour above the month's peak hour becomes the peak hour."""
    home._hourly_consumption_data.peak_hour = 1.0  # noqa: SLF001

    home._enrich_measurement(_measurement(accumulated_consumption_last_hour=1.234))  # noqa: SLF001
    assert home.peak_hour == 1.23
    assert home.peak_hour_time == LIVE_TIME

    home._enrich_measurement(  # noqa: SLF001
        _measurement(accumulated_consumption_last_hour=0.5, timestamp=LIVE_TIME + dt.timedelta(minutes=1)),
    )
    assert home.peak_hour == 1.23
    assert home.peak_hour_time == LIVE_TIME
