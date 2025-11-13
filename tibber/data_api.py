"""Tibber Data API client for REST endpoints."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any, TypeAlias

import aiohttp

from .const import DATA_API_ENDPOINT, DEFAULT_TIMEOUT, USERINFO_ENDPOINT
from .exceptions import FatalHttpExceptionError, InvalidLoginError, RetryableHttpExceptionError

_LOGGER = logging.getLogger(__name__)

SensorValue: TypeAlias = bool | int | float | str | None


class TibberDataAPI:
    """Client for Tibber Data API REST endpoints."""

    def __init__(
        self,
        access_token: str,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Initialize the Tibber Data API client.

        :param access_token: The access token to access the Tibber Data API with.
        :param timeout: The timeout in seconds to use when communicating with the API.
        :param websession: The websession to use when communicating with the API.
        :param user_agent: Optional user agent string attached to outgoing requests.
        """
        owns_session = websession is None
        if websession is None:
            websession = aiohttp.ClientSession()

        self.websession = websession
        self._owns_session = owns_session
        self.timeout = timeout
        self._access_token = access_token
        self._user_agent = user_agent

    async def close_connection(self) -> None:
        """Close the Data API connection."""
        if self._owns_session and not self.websession.closed:
            await self.websession.close()

    async def _make_request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        retry: int = 3,
    ) -> dict[str, Any] | None:
        """Make a request to the Data API.

        :param method: HTTP method (GET, POST, etc.).
        :param endpoint: API endpoint path.
        :param params: Query parameters.
        :param retry: Number of retries on failure.
        """
        url = f"{DATA_API_ENDPOINT}{endpoint}"

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._user_agent:
            headers[aiohttp.hdrs.USER_AGENT] = self._user_agent

        try:
            async with self.websession.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status == HTTPStatus.OK:
                    return await response.json()
                if response.status == HTTPStatus.UNAUTHORIZED:
                    raise InvalidLoginError(response.status, "Invalid token")
                if response.status in (HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND):
                    error_data = await response.json() if response.content_type == "application/json" else {}
                    raise FatalHttpExceptionError(
                        response.status,
                        error_data.get("detail", "Bad Request"),
                        error_data.get("type", "BAD_REQUEST"),
                    )
                if response.status in (HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.PRECONDITION_FAILED):
                    error_data = await response.json() if response.content_type == "application/json" else {}
                    raise RetryableHttpExceptionError(
                        response.status,
                        error_data.get("detail", "Rate limited"),
                        error_data.get("type", "RATE_LIMITED"),
                    )
                _LOGGER.error("Unexpected HTTP status: %s", response.status)
                return None

        except (aiohttp.ClientError, TimeoutError):
            if retry > 0:
                _LOGGER.warning("Request failed, retrying... (%d attempts left)", retry, exc_info=True)
                return await self._make_request(method, endpoint, params, retry - 1)
            _LOGGER.exception("Error connecting to Tibber Data API")
            raise
        except (InvalidLoginError, FatalHttpExceptionError) as err:
            _LOGGER.error(
                "Fatal error interacting with Tibber Data API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise
        except RetryableHttpExceptionError as err:
            _LOGGER.warning(
                "Temporary failure interacting with Tibber Data API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise

    async def get_homes(self) -> list[dict[str, Any]]:
        """Get all homes for the user."""
        response = await self._make_request("GET", "/v1/homes")
        if response is None:
            return []
        return response.get("homes", [])

    async def get_devices_for_home(self, home_id: str) -> list[dict[str, Any]]:
        """Get all devices for a specific home."""
        response = await self._make_request("GET", f"/v1/homes/{home_id}/devices")
        if response is None:
            return []
        return response.get("devices", [])

    async def get_device(self, home_id: str, device_id: str) -> TibberDevice | None:
        """Get detailed information about a specific device."""
        try:
            response = await self._make_request("GET", f"/v1/homes/{home_id}/devices/{device_id}")
        except FatalHttpExceptionError:
            return None
        if response is None:
            return None
        return TibberDevice(response)

    async def get_all_devices(self) -> dict[str, TibberDevice]:
        """Get all devices for the user."""
        devices: dict[str, TibberDevice] = {}
        for home in await self.get_homes():
            for raw_device in await self.get_devices_for_home(home["id"]):
                device = await self.get_device(home["id"], raw_device["id"])
                if device is not None:
                    devices[device.id] = device
        return devices

    async def get_userinfo(self) -> dict[str, Any]:
        """Return OpenID Connect user info for the current access token."""
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

        try:
            async with self.websession.get(
                USERINFO_ENDPOINT,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status == HTTPStatus.OK:
                    return await response.json()
                if response.status == HTTPStatus.UNAUTHORIZED:
                    raise InvalidLoginError(response.status, "Invalid token")

                detail: str | None = None
                if response.content_type == "application/json":
                    error_data = await response.json()
                    detail = error_data.get("error_description")
                    if detail is None:
                        detail = error_data.get("detail")
                    if detail is None:
                        detail = error_data.get("error")
                else:
                    detail = await response.text()

                raise FatalHttpExceptionError(
                    response.status,
                    detail or "Failed to retrieve user info",
                    "USERINFO_HTTP_ERROR",
                )
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.exception("Error connecting to Tibber user info endpoint")
            raise


class TibberDevice:
    """Represents a Tibber device from the Data API."""

    def __init__(self, device_data: dict[str, Any]) -> None:
        """Initialize the device."""
        self._data = device_data
        self._sensors = [Sensor(capability) for capability in device_data["capabilities"]]

    @property
    def id(self) -> str:
        """Return the device ID."""
        return self._data["id"]

    @property
    def external_id(self) -> str:
        """Return the device external ID."""
        return self._data["externalId"]

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._data["info"]["name"]

    @property
    def sensors(self) -> list[Sensor]:
        """Return the device capabilities."""
        return self._sensors

    @property
    def brand(self) -> str:
        """Return the device brand."""
        return self._data["info"]["brand"]

    @property
    def model(self) -> str:
        """Return the device model."""
        return self._data["info"]["model"]


class Sensor:
    """Represents a Tibber device capability from the Data API."""

    def __init__(self, capability_data: dict[str, Any]) -> None:
        """Initialize the sensor."""
        self._data = capability_data

    @property
    def id(self) -> str:
        """Return the sensor ID."""
        return self._data["id"]

    @property
    def unit(self) -> str:
        """Return the device capability unit."""
        return self._data["unit"]

    @property
    def value(self) -> SensorValue:
        """Return the device capability value."""
        return self._data["value"]

    @property
    def description(self) -> str:
        """Return the device capability description."""
        description = self._data.get("description")
        if not description:
            return ""
        return f"{description[0].upper()}{description[1:]}"
