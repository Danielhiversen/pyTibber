"""Tests for Tibber Data API."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tibber.data_api import TibberDataAPI, TibberDevice


@pytest.fixture
def data_api() -> TibberDataAPI:
    """Provide a TibberDataAPI instance with a mocked websession."""
    websession = MagicMock()
    return TibberDataAPI(
        access_token="test-token",
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
    data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch
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
        ]
    }
    mocked_make_request = AsyncMock(return_value=mock_devices)
    monkeypatch.setattr(data_api, "_make_request", mocked_make_request)

    devices = await data_api.get_devices_for_home(home_id)

    mocked_make_request.assert_awaited_once_with("GET", f"/v1/homes/{home_id}/devices")
    assert devices == mock_devices["devices"]


@pytest.mark.asyncio
async def test_get_device_returns_tibber_device(data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirm a detailed device request produces a TibberDevice instance."""
    home_id = "home-1"
    device_id = "device-1"
    device_payload = {
        "id": device_id,
        "externalId": "external-id",
        "info": {"name": "Device 1", "brand": "Brand", "model": "Model"},
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
    assert device.model == "Model"


@pytest.mark.asyncio
async def test_get_all_devices_flattens_device_lists(
    data_api: TibberDataAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_all_devices should merge devices from all homes keyed by device id."""
    homes_payload = {"homes": [{"id": "home-1"}, {"id": "home-2"}]}
    devices_payload = {
        "home-1": {
            "devices": [
                {"id": "device-1", "info": {"name": "D1", "brand": "B", "model": "M"}, "capabilities": []}
            ]
        },
        "home-2": {
            "devices": [
                {"id": "device-2", "info": {"name": "D2", "brand": "B", "model": "M"}, "capabilities": []}
            ]
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
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        retry: int = 3,
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

    device = TibberDevice(device_data)

    assert device.id == "test-device-id"
    assert device.external_id == "test-external-id"
    assert device.name == "Test Device"
    assert device.brand == "Test Brand"
    assert device.model == "Test Model"
    assert [sensor.id for sensor in device.sensors] == ["sensor-1"]
