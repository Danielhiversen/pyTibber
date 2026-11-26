"""Tests for Tibber Data API."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tibber.data_api import TibberDataAPI, TibberDevice
from tibber.exceptions import PricesNotAvailableError
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
        device_id = endpoint.rsplit("/", maxsplit=1)[-1]
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


@pytest.mark.asyncio
async def test_get_prices_builds_query_params(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_prices forwards datetime/string params to _make_request."""
    mocked_make_request = AsyncMock(return_value={"currency": "NOK", "items": []})
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    await data_api.get_prices(
        "home-1",
        since="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00Z",
        cursor="cursor-token-123",
    )

    mocked_make_request.assert_awaited_once_with(
        "GET",
        "/v1/homes/home-1/prices",
        params={
            "since": "2026-01-01T00:00:00Z",
            "until": "2026-01-02T00:00:00Z",
            "cursor": "cursor-token-123",
        },
    )


@pytest.mark.asyncio
async def test_get_prices_defaults_to_no_params(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_prices without params requests the endpoint with no query params."""
    mocked_make_request = AsyncMock(return_value={})
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    await data_api.get_prices("home-1")

    mocked_make_request.assert_awaited_once_with("GET", "/v1/homes/home-1/prices", params=None)


@pytest.mark.asyncio
async def test_get_prices_follows_next_link(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Follow a returned link directly and keep its query intact."""
    next_url = "https://data-api.tibber.com/v1/homes/home-1/prices?cursor=abc%2B123"
    first_page = MagicMock(status=200, content_type="application/json")
    first_page.json = AsyncMock(return_value={"next": next_url, "items": []})
    second_page = MagicMock(status=200, content_type="application/json")
    second_page.json = AsyncMock(return_value={"next": None, "items": []})
    request = AsyncMock(side_effect=[first_page, second_page])
    monkeypatch.setattr(data_api.websession, "request", request)

    page = await data_api.get_prices_for_home("home-1")
    assert await data_api.get_prices("home-1", cursor=page["next"]) == {"next": None, "items": []}
    assert request.await_args.args == ("GET", next_url)
    assert request.await_args.kwargs.get("params") is None


@pytest.mark.asyncio
async def test_get_prices_rejects_links_to_other_origins(
    data_api: TibberDataAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep authenticated pagination requests on the Data API origin."""
    request = AsyncMock()
    monkeypatch.setattr(data_api.websession, "request", request)

    with pytest.raises(ValueError, match="Data API origin"):
        await data_api.get_prices("home-1", url="https://example.com/prices")

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_prices_maps_unavailable_error(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose the Tibber problem type as a dedicated exception."""
    response = MagicMock(status=403, content_type="application/problem+json")
    response.json = AsyncMock(
        return_value={
            "type": "https://developer.tibber.com/errors/prices-not-available",
            "detail": "Prices unavailable",
        },
    )
    monkeypatch.setattr(data_api.websession, "request", AsyncMock(return_value=response))

    with pytest.raises(PricesNotAvailableError, match="Prices unavailable"):
        await data_api.get_prices("home-1")
