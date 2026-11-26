"""Tests for Tibber Data API."""

import datetime as dt
from email.utils import format_datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tibber.data_api import PriceDetails, PriceInterval, TibberDataAPI, TibberDevice
from tibber.exceptions import InsufficientScopeError, PricesNotAvailableError, RateLimitExceededError
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

    with pytest.raises(ValueError, match="Data API origin"):
        await data_api.get_prices("home-1", start, start + dt.timedelta(days=1))

    assert request.await_count == 1


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
        (format_datetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30), usegmt=True), 30.0),
        ("not a date", None),
    ],
)
def test_parse_retry_after(retry_after: str, expected: float | None) -> None:
    """Retry-After is either seconds or an HTTP-date."""
    seconds = TibberDataAPI._parse_retry_after(retry_after)  # noqa: SLF001

    if expected is None:
        assert seconds is None
    else:
        assert seconds == pytest.approx(expected, abs=1.5)
