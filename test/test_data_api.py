"""Tests for Tibber Data API."""

import datetime as dt
import json
from collections.abc import AsyncIterator
from email.utils import format_datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tibber.const import DATA_API_ENDPOINT
from tibber.data_api import PriceDetails, PriceInterval, TibberDataAPI, TibberDevice
from tibber.exceptions import (
    FatalHttpExceptionError,
    InsufficientScopeError,
    InvalidLoginError,
    LiveStreamError,
    PricesNotAvailableError,
    RateLimitExceededError,
    RetryableHttpExceptionError,
)
from tibber.live_events import LIVE_EVENTS_READ_TIMEOUT, LiveEventDevice, LiveMeasurement
from tibber.token_manager import TokenManager


@pytest.fixture
def data_api() -> TibberDataAPI:
    """Provide a TibberDataAPI instance with a mocked websession."""
    websession = MagicMock()
    return TibberDataAPI(
        token_manager=TokenManager("test-token"),
        timeout=10,
        websession=websession,
        user_agent="test-agent",
    )


@pytest.mark.asyncio
async def test_get_homes_uses_data_api(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify homes are fetched via the REST API wrapper."""
    mock_response = {"homes": [{"id": "home-1"}, {"id": "home-2"}]}
    mocked_make_request = AsyncMock(return_value=mock_response)
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    homes = await data_api.get_homes()

    mocked_make_request.assert_awaited_once_with("GET", "/v1/homes")
    assert homes == mock_response["homes"]


@pytest.mark.asyncio
async def test_get_devices_for_home_returns_raw_devices(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure device list is returned as received from the API."""
    home_id = "home-1"
    mock_devices = {
        "devices": [
            {
                "id": "device-1",
                "info": {"name": "Device 1", "brand": "Brand", "model": "Model"},
                "capabilities": [],
            },
            {
                "id": "device-2",
                "info": {"name": "Device 2", "brand": "Brand", "model": "Model"},
                "capabilities": [],
            },
        ],
    }
    mocked_make_request = AsyncMock(return_value=mock_devices)
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    devices = await data_api.get_devices_for_home(home_id)

    mocked_make_request.assert_awaited_once_with("GET", f"/v1/homes/{home_id}/devices")
    assert devices == mock_devices["devices"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_info", "expected_model"),
    [
        pytest.param({"name": "Device 1", "brand": "Brand", "model": "Model"}, "Model", id="with-model"),
        pytest.param({"name": "Device 1", "brand": "Brand", "model": ""}, "", id="empty-model"),
        pytest.param({"name": "Device 1", "brand": "Brand"}, None, id="without-model"),
    ],
)
async def test_get_device_returns_tibber_device(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
    device_info: dict[str, str],
    expected_model: str | None,
) -> None:
    """Confirm a detailed device request produces a TibberDevice instance."""
    home_id = "home-1"
    device_id = "device-1"
    device_payload = {
        "id": device_id,
        "externalId": "external-id",
        "info": device_info,
        "capabilities": [],
    }
    mocked_make_request = AsyncMock(return_value=device_payload)
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    device = await data_api.get_device(home_id, device_id)

    mocked_make_request.assert_awaited_once_with("GET", f"/v1/homes/{home_id}/devices/{device_id}")
    assert isinstance(device, TibberDevice)
    assert device.id == device_id
    assert device.external_id == "external-id"
    assert device.name == "Device 1"
    assert device.brand == "Brand"
    assert device.model == expected_model
    assert device.home_id == home_id


@pytest.mark.asyncio
async def test_get_all_devices_flattens_device_lists(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_all_devices should merge devices from all homes keyed by device id."""
    homes_payload = {"homes": [{"id": "home-1"}, {"id": "home-2"}]}
    devices_payload = {
        "home-1": {
            "devices": [
                {"id": "device-1", "info": {"name": "D1", "brand": "B", "model": "M"}, "capabilities": []},
            ],
        },
        "home-2": {
            "devices": [
                {"id": "device-2", "info": {"name": "D2", "brand": "B", "model": "M"}, "capabilities": []},
            ],
        },
    }
    device_detail_payload = {
        "device-1": {
            "id": "device-1",
            "externalId": "ext-1",
            "info": {"name": "D1", "brand": "B", "model": "M"},
            "capabilities": [],
        },
        "device-2": {
            "id": "device-2",
            "externalId": "ext-2",
            "info": {"name": "D2", "brand": "B", "model": "M"},
            "capabilities": [],
        },
    }

    async def fake_make_request(
        _method: str,
        endpoint: str,
        _params: dict[str, Any] | None = None,
        _retry: int = 3,
        _rate_limit_attempt: int = 0,
    ) -> dict[str, Any]:
        if endpoint == "/v1/homes":
            return homes_payload
        if endpoint.endswith("/devices"):
            home_id = endpoint.split("/")[3]
            return devices_payload[home_id]
        device_id = endpoint.split("/")[-1]
        return device_detail_payload[device_id]

    monkeypatch.setattr(data_api, "_make_request", AsyncMock(side_effect=fake_make_request))

    devices = await data_api.get_all_devices()

    assert set(devices.keys()) == {"device-1", "device-2"}
    assert all(isinstance(value, TibberDevice) for value in devices.values())
    assert {value.home_id for value in devices.values()} == {"home-1", "home-2"}


def test_tibber_device_properties_match_payload() -> None:
    """Ensure TibberDevice exposes expected basic properties."""
    device_data = {
        "id": "test-device-id",
        "externalId": "test-external-id",
        "info": {"name": "Test Device", "brand": "Test Brand", "model": "Test Model"},
        "capabilities": [
            {"id": "sensor-1", "unit": "W", "value": 10, "description": "power usage"},
        ],
    }

    device = TibberDevice(device_data, home_id="home-1")

    assert device.id == "test-device-id"
    assert device.external_id == "test-external-id"
    assert device.name == "Test Device"
    assert device.brand == "Test Brand"
    assert device.model == "Test Model"
    assert device.home_id == "home-1"
    assert [sensor.id for sensor in device.sensors] == ["sensor-1"]


PRICES_URL = "https://data-api.tibber.com/v1/homes/home-1/prices"


def _response(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock(status=status, content_type="application/json", headers=headers or {})
    response.json = AsyncMock(return_value=payload)
    return response


def _page(times: list[str], next_link: str | None = None) -> MagicMock:
    return _response(
        {
            "currency": "NOK",
            "items": [{"time": time, "consumption": {"price": 1.0}} for time in times],
            "next": next_link,
        },
    )


def _quarters(start: str, count: int) -> list[str]:
    first = dt.datetime.fromisoformat(start)
    return [(first + dt.timedelta(minutes=15 * index)).isoformat() for index in range(count)]


@pytest.mark.asyncio
async def test_get_prices_pages_forward_until_end(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Request from `start` in UTC, follow relative `next` links and stop at `end`."""
    start = dt.datetime(2026, 10, 25, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    end = start + dt.timedelta(hours=49)
    request = AsyncMock(
        side_effect=[
            _page(_quarters("2026-10-24T22:00:00+00:00", 192), "/v1/homes/home-1/prices?since=2026-10-26T22%3A00Z"),
            _page(_quarters("2026-10-26T22:00:00+00:00", 96), "/v1/homes/home-1/prices?since=later"),
        ],
    )
    monkeypatch.setattr(data_api.websession, "request", request)

    prices = await data_api.get_prices("home-1", start, end)

    assert prices.currency == "NOK"
    assert len(prices.intervals) == 196
    assert prices.intervals[0] == PriceInterval(
        start=dt.datetime(2026, 10, 24, 22, 0, tzinfo=dt.UTC),
        consumption=PriceDetails(price=1.0),
    )
    assert prices.intervals[-1].start == dt.datetime(2026, 10, 26, 22, 45, tzinfo=dt.UTC)
    assert [(c.args, c.kwargs["params"]) for c in request.await_args_list] == [
        (("GET", PRICES_URL), {"since": "2026-10-24T22:00:00Z"}),
        (("GET", f"{PRICES_URL}?since=2026-10-26T22%3A00Z"), None),
    ]


@pytest.mark.asyncio
async def test_get_prices_stops_at_unpublished_prices(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty page at the present means newer prices aren't published, so stop."""
    start = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    link = f"{PRICES_URL}?since=tomorrow"
    request = AsyncMock(
        side_effect=[_page(_quarters(start.isoformat(), 96), link), _page([], link), _page([], link)],
    )
    monkeypatch.setattr(data_api.websession, "request", request)

    prices = await data_api.get_prices("home-1", start, start + dt.timedelta(days=2))

    assert len(prices.intervals) == 96
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_get_prices_skips_gaps_in_history(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty page in the past is a gap, and its `next` link skips ahead."""
    start = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    request = AsyncMock(
        side_effect=[
            _page([], f"{PRICES_URL}?since=2020-01-05T00%3A00Z"),
            _page(_quarters("2020-01-05T00:00:00+00:00", 4)),
        ],
    )
    monkeypatch.setattr(data_api.websession, "request", request)

    prices = await data_api.get_prices("home-1", start, start + dt.timedelta(days=7))

    assert [interval.start.day for interval in prices.intervals] == [5, 5, 5, 5]
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_get_prices_rejects_naive_times(data_api: TibberDataAPI) -> None:
    """Naive times are ambiguous, the Data API needs an offset."""
    with pytest.raises(ValueError, match="timezone aware"):
        await data_api.get_prices("home-1", dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2))  # noqa: DTZ001


@pytest.mark.asyncio
async def test_get_prices_rejects_links_to_other_origins(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep authenticated pagination requests on the Data API origin."""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    request = AsyncMock(return_value=_page(_quarters(start.isoformat(), 1), "https://example.com/prices"))
    monkeypatch.setattr(data_api.websession, "request", request)

    with pytest.raises(FatalHttpExceptionError, match="another origin") as error:
        await data_api.get_prices("home-1", start, start + dt.timedelta(days=1))

    assert error.value.extension_code == "INVALID_NEXT_LINK"
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_get_prices_without_a_range_makes_no_request(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `end` that is not after `start` is an empty range."""
    request = AsyncMock()
    monkeypatch.setattr(data_api.websession, "request", request)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    for end in (start, start - dt.timedelta(days=1)):
        prices = await data_api.get_prices("home-1", start, end)
        assert prices.intervals == []

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_prices_warns_when_the_page_limit_is_reached(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pages that keep linking on without reaching `end` must not end silently."""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    # A day needs one page, and the limit adds a margin of five for gaps in the price history.
    request = AsyncMock(side_effect=[_page(_quarters(start.isoformat(), 1), f"{PRICES_URL}?since=more")] * 6)
    monkeypatch.setattr(data_api.websession, "request", request)
    caplog.set_level("WARNING", logger="tibber.data_api")

    prices = await data_api.get_prices("home-1", start, start + dt.timedelta(days=1))

    assert request.await_count == 6
    assert len(prices.intervals) == 1
    assert "stopped after 6 price pages" in caplog.text


@pytest.mark.asyncio
async def test_get_prices_warns_about_malformed_items(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Items that aren't valid prices are skipped, loudly, instead of looking like missing prices."""
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    page = _response(
        {
            "currency": "NOK",
            "items": [
                {"time": "2026-01-01T00:00:00Z", "consumption": {"price": 1.0}},
                {"time": "not a time", "consumption": {"price": 1.0}},
                {"time": "2026-01-01T00:30:00", "consumption": {"price": 1.0}},
                "not an item",
            ],
        },
    )
    monkeypatch.setattr(data_api.websession, "request", AsyncMock(return_value=page))
    caplog.set_level("WARNING", logger="tibber.data_api")

    prices = await data_api.get_prices("home-1", start, start + dt.timedelta(days=1))

    assert [interval.start for interval in prices.intervals] == [start]
    assert "skipped 3 malformed price items" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"type": "https://developer.tibber.com/errors/prices-not-available", "detail": "No"}, PricesNotAvailableError),
        ({"detail": "Missing scope"}, InsufficientScopeError),
    ],
)
async def test_get_prices_maps_forbidden_errors(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    error: type[Exception],
) -> None:
    """A 403 is either no prices for the home, or a missing scope."""
    response = _response(payload, status=403)
    response.content_type = "application/problem+json"
    monkeypatch.setattr(data_api.websession, "request", AsyncMock(return_value=response))
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    with pytest.raises(error, match=payload["detail"]):
        await data_api.get_prices("home-1", start, start + dt.timedelta(days=1))


@pytest.mark.asyncio
async def test_rate_limit_attempts_are_per_request(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """An exhausted rate limit budget must not leak into the next request."""
    request = AsyncMock(
        side_effect=[
            *[_response({}, status=429, headers={"Retry-After": "0"}) for _ in range(4)],
            _response({"homes": []}),
        ],
    )
    monkeypatch.setattr(data_api.websession, "request", request)

    with patch("tibber.data_api.asyncio.sleep", AsyncMock()):
        with pytest.raises(RateLimitExceededError):
            await data_api.get_homes()
        assert await data_api.get_homes() == []

    assert request.await_count == 5


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("5", 5.0),
        ("1.9", 1.9),
        ("-5", 0.0),
        ("inf", None),
        ("1e999", None),
        ("nan", None),
        ("not a date", None),
    ],
)
def test_parse_retry_after_seconds(retry_after: str, expected: float | None) -> None:
    """Retry-After in seconds is never negative, and a value that isn't a finite wait is no wait at all."""
    assert TibberDataAPI._parse_retry_after(retry_after) == expected  # noqa: SLF001


def test_parse_retry_after_http_date() -> None:
    """An HTTP-date is the seconds until then, or 0 when it has passed."""
    # The header has whole seconds, which takes up to a second off the wait.
    retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)

    assert TibberDataAPI._parse_retry_after(format_datetime(retry_at, usegmt=True)) == pytest.approx(  # noqa: SLF001
        30,
        abs=1.5,
    )
    assert TibberDataAPI._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # noqa: SLF001


def test_infinite_retry_after_falls_back_to_backoff(data_api: TibberDataAPI) -> None:
    """A Retry-After that can't be used must not break the wait calculation."""
    assert 1.0 <= data_api._calculate_429_wait_time("inf", 0) <= 31.0  # noqa: SLF001


def _set_request(data_api: TibberDataAPI, request: AsyncMock) -> AsyncMock:
    data_api.websession.request = request  # type: ignore[method-assign]
    return request


def _fake_stream_response(frames: list[bytes], status: int = 200) -> MagicMock:
    async def _content() -> AsyncIterator[bytes]:
        for frame in frames:
            for line in frame.splitlines(keepends=True):
                yield line

    return MagicMock(status=status, content=_content(), release=MagicMock())


async def _collect(data_api: TibberDataAPI, device_ids: list[str] | None = None) -> list[LiveMeasurement]:
    return [m async for m in data_api.stream_live_events("h1", ["a"] if device_ids is None else device_ids)]


_MEASUREMENT = b'event: measurement\ndata: {"deviceId":"a","power":100}\n\n'


@pytest.mark.asyncio
async def test_get_live_event_devices(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Devices with non-string ids or types are dropped."""
    mocked = AsyncMock(
        return_value={
            "devices": [{"id": "p1", "type": "tibber pulse"}, {"id": 2, "type": "x"}, {"id": "w1", "type": "watty"}],
        },
    )
    monkeypatch.setattr(data_api, "_make_request", mocked)

    devices = await data_api.get_live_event_devices("h1")

    assert devices == [LiveEventDevice("p1", "tibber pulse"), LiveEventDevice("w1", "watty")]
    mocked.assert_awaited_once_with("GET", "/v1/homes/h1/live-events/devices")


@pytest.mark.asyncio
async def test_stream_live_events_request_shape(data_api: TibberDataAPI) -> None:
    """The stream request carries auth, SSE accept header, device ids and is released."""
    response = _fake_stream_response([b"event: ping\ndata:\n\n"])
    request = _set_request(data_api, AsyncMock(return_value=response))

    await _collect(data_api, ["a", "b"])

    args, kwargs = request.await_args
    assert args == ("GET", f"{DATA_API_ENDPOINT}/v1/homes/h1/live-events")
    assert kwargs["params"] == [("deviceId", "a"), ("deviceId", "b")]
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["headers"]["Accept"] == "text/event-stream"
    assert kwargs["headers"]["User-Agent"] == data_api._user_agent  # noqa: SLF001
    response.release.assert_called_once()


@pytest.mark.asyncio
async def test_stream_live_events_sets_read_timeout(data_api: TibberDataAPI) -> None:
    """The stream has no total timeout but a socket read timeout."""
    request = _set_request(data_api, AsyncMock(return_value=_fake_stream_response([])))

    await _collect(data_api)

    timeout = request.await_args.kwargs["timeout"]
    assert timeout.total is None
    assert timeout.sock_read == LIVE_EVENTS_READ_TIMEOUT
    assert timeout.sock_connect == 10


@pytest.mark.asyncio
async def test_stream_live_events_yields_measurements_skips_ping_and_junk(data_api: TibberDataAPI) -> None:
    """Only valid measurement events are yielded."""
    frames = [
        b"event: ping\ndata:\n\n",
        _MEASUREMENT,
        b"event: measurement\ndata: not json\n\n",
        b'event: measurement\ndata: {"power":1}\n\n',
    ]
    _set_request(data_api, AsyncMock(return_value=_fake_stream_response(frames)))

    assert await _collect(data_api) == [LiveMeasurement(device_id="a", power=100.0)]


@pytest.mark.asyncio
async def test_stream_live_events_stream_error(data_api: TibberDataAPI) -> None:
    """A stream_error event raises LiveStreamError."""
    payload = json.dumps({"code": "upstream_unavailable", "transient": True, "retryAfter": 7})
    frames = [_MEASUREMENT, f"event: stream_error\ndata: {payload}\n\n".encode()]
    response = _fake_stream_response(frames)
    _set_request(data_api, AsyncMock(return_value=response))

    with pytest.raises(LiveStreamError) as exc_info:
        await _collect(data_api)

    assert exc_info.value.code == "upstream_unavailable"
    assert exc_info.value.transient is True
    assert exc_info.value.retry_after == 7
    response.release.assert_called_once()


@pytest.mark.asyncio
async def test_stream_live_events_malformed_stream_error(data_api: TibberDataAPI) -> None:
    """Malformed stream_error data becomes a transient unknown error."""
    _set_request(
        data_api,
        AsyncMock(
            return_value=_fake_stream_response([b"event: stream_error\ndata: nope\n\n"]),
        ),
    )

    with pytest.raises(LiveStreamError) as exc_info:
        await _collect(data_api)

    assert exc_info.value.code == "unknown"
    assert exc_info.value.transient is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(401, InvalidLoginError), (403, InsufficientScopeError), (503, RetryableHttpExceptionError)],
)
async def test_stream_live_events_http_errors(data_api: TibberDataAPI, status: int, error: type[Exception]) -> None:
    """Non-200 responses raise through _handle_error_response and are released."""
    response = _fake_stream_response([], status=status)
    response.headers = {}
    response.closed = True
    response.text = AsyncMock(return_value="")
    response.json = AsyncMock(return_value={})
    _set_request(data_api, AsyncMock(return_value=response))

    with pytest.raises(error):
        await _collect(data_api)

    response.release.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_ids", [[], ["a", "b", "c", "d", "e", "f"]])
async def test_stream_live_events_validates_device_count(data_api: TibberDataAPI, device_ids: list[str]) -> None:
    """Device counts outside 1..5 are rejected before any request."""
    request = _set_request(data_api, AsyncMock())

    with pytest.raises(ValueError, match="device"):
        await _collect(data_api, device_ids)

    request.assert_not_called()
