"""Tibber Data API client for REST endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from http import HTTPStatus
from typing import Any, NoReturn, TypeAlias

import aiohttp

from .const import DATA_API_ENDPOINT, DEFAULT_TIMEOUT, USERINFO_ENDPOINT, __version__
from .exceptions import (
    FatalHttpExceptionError,
    InvalidLoginError,
    RetryableHttpExceptionError,
    UserAgentMissingError,
)
from .response_handler import extract_response_data

_LOGGER = logging.getLogger(__name__)

MAX_RATE_LIMIT_ATTEMPTS = 2

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
            if user_agent is None:
                raise UserAgentMissingError("Please provide value for HTTP user agent")
            websession = aiohttp.ClientSession()
        elif user_agent is None:
            user_agent = websession.headers.get(aiohttp.hdrs.USER_AGENT)

        if user_agent is None:
            raise UserAgentMissingError("Please provide value for HTTP user agent")

        self.websession = websession
        self._owns_session = owns_session
        self.timeout = timeout
        self._access_token = access_token
        self._user_agent = f"{user_agent} pyTibber/{__version__} "
        self._devices: dict[str, TibberDevice] = {}

    def set_access_token(self, access_token: str) -> None:
        """Set the access token."""
        self._access_token = access_token

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
        rate_limit_attempt: int = 0,
    ) -> dict[str, Any]:
        """Make a request to the Data API.

        :param method: HTTP method (GET, POST, etc.).
        :param endpoint: API endpoint path.
        :param params: Query parameters.
        :param retry: Number of retries on failure.
        :param rate_limit_attempt: Current attempt number for 429 rate limiting (max MAX_RATE_LIMIT_ATTEMPTS).
        """
        url = f"{DATA_API_ENDPOINT}{endpoint}"

        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._user_agent:
            headers[aiohttp.hdrs.USER_AGENT] = self._user_agent

        response: aiohttp.ClientResponse | None = None
        try:
            response = await self.websession.request(
                method,
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
            status = response.status
            if status == HTTPStatus.OK:
                data = await extract_response_data(response)
                response.close()
                return data

            if status == HTTPStatus.TOO_MANY_REQUESTS:
                if rate_limit_attempt >= MAX_RATE_LIMIT_ATTEMPTS:
                    _LOGGER.error("Rate limit exceeded: max attempts (%d) reached", MAX_RATE_LIMIT_ATTEMPTS)
                    await self._handle_error_response(response)
                retry_after = response.headers.get("Retry-After")
                wait_time = self._calculate_429_wait_time(retry_after, rate_limit_attempt)

                _LOGGER.warning(
                    "Rate limited (429), waiting %.2f seconds before retry (attempt %d/%d)",
                    wait_time,
                    rate_limit_attempt + 1,
                    MAX_RATE_LIMIT_ATTEMPTS,
                )
                await asyncio.sleep(wait_time)

                return await self._make_request(method, endpoint, params, retry, rate_limit_attempt + 1)

            await self._handle_error_response(response)

        except (aiohttp.ClientError, TimeoutError):
            if retry > 0:
                _LOGGER.warning("Request failed, retrying... (%d attempts left)", retry, exc_info=True)
                return await self._make_request(method, endpoint, params, retry - 1, rate_limit_attempt)
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
        finally:
            if response is not None and not response.closed:
                response.close()

    def _calculate_429_wait_time(self, retry_after: str | None, attempt: int) -> float:
        """Calculate wait time for 429 rate limiting.

        :param retry_after: Retry-After header value (seconds or HTTP-date).
        :param attempt: Current attempt number (0-based).
        :return: Wait time in seconds.
        """
        if retry_after:
            wait_seconds: int | float | None
            try:
                wait_seconds = int(retry_after)
            except ValueError:
                try:
                    retry_time = dt.datetime.fromisoformat(retry_after)
                    if retry_time.tzinfo is None:
                        retry_time = retry_time.replace(tzinfo=dt.UTC)
                    else:
                        retry_time = retry_time.astimezone(dt.UTC)
                    now = dt.datetime.now(dt.UTC)
                    wait_seconds = max(0, (retry_time - now).total_seconds())
                except ValueError:
                    wait_seconds = None

            if wait_seconds is not None:
                jitter = random.uniform(0, 0.25)  # noqa: S311
                return wait_seconds + jitter

        base = 1.0
        max_wait = base * (2**attempt)
        return random.uniform(0, max_wait)  # noqa: S311

    async def _handle_error_response(self, response: aiohttp.ClientResponse) -> NoReturn:
        """Handle non-OK HTTP responses from the Data API."""
        status = response.status
        if status == HTTPStatus.UNAUTHORIZED:
            response.close()
            raise InvalidLoginError(status, "Invalid token")

        detail, extension_code = await self._read_error_response(response)
        response.close()

        fatal_codes: dict[int, str] = {
            HTTPStatus.BAD_REQUEST.value: "BAD_REQUEST",
            HTTPStatus.NOT_FOUND.value: "NOT_FOUND",
        }
        if status in fatal_codes:
            raise FatalHttpExceptionError(
                status,
                detail,
                extension_code or fatal_codes[status],
            )
        if status in (HTTPStatus.TOO_MANY_REQUESTS, HTTPStatus.PRECONDITION_FAILED):
            self._rate_limited_until = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self.timeout)
            raise RetryableHttpExceptionError(
                status,
                detail or "Rate limited",
                extension_code or "RATE_LIMITED",
            )
        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            raise RetryableHttpExceptionError(status, detail, extension_code)
        if status >= HTTPStatus.BAD_REQUEST and status < HTTPStatus.INTERNAL_SERVER_ERROR:
            raise FatalHttpExceptionError(status, detail, extension_code)

        _LOGGER.error("Unexpected HTTP status: %s", status)
        raise FatalHttpExceptionError(status, detail, extension_code)

    async def _read_error_response(self, response: aiohttp.ClientResponse) -> tuple[str, str]:
        """Extract detail and extension code from an error HTTP response."""
        extension_code = f"HTTP_{response.status}"
        detail: str | None = response.reason

        if response.content_type == "application/json":
            try:
                error_data = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                pass
            else:
                preferred_detail = (
                    error_data.get("detail") or error_data.get("error_description") or error_data.get("error")
                )
                if preferred_detail is not None:
                    detail = preferred_detail
                extension_code = error_data.get("type") or extension_code
                return detail or "HTTP error", extension_code

        try:
            text = await response.text()
        except (aiohttp.ClientError, UnicodeDecodeError):
            text = None
        if text:
            detail = text
        return detail or "HTTP error", extension_code

    async def get_homes(self) -> list[dict[str, Any]]:
        """Get all homes for the user."""
        response = await self._make_request("GET", "/v1/homes")
        return response.get("homes", [])

    async def get_devices_for_home(self, home_id: str) -> list[dict[str, Any]]:
        """Get all devices for a specific home."""
        response = await self._make_request("GET", f"/v1/homes/{home_id}/devices")
        return response.get("devices", [])

    async def get_device(self, home_id: str, device_id: str) -> TibberDevice | None:
        """Get detailed information about a specific device."""
        try:
            response = await self._make_request("GET", f"/v1/homes/{home_id}/devices/{device_id}")
        except FatalHttpExceptionError:
            _LOGGER.error("Error getting device %s for home %s", device_id, home_id)
            return None
        return TibberDevice(response, home_id)

    async def get_all_devices(self) -> dict[str, TibberDevice]:
        """Get all devices for the user."""
        homes = await self.get_homes()
        if not homes:
            return {}

        devices: dict[str, TibberDevice] = {}
        for home in homes:
            _LOGGER.debug("home data: %s", home)
            raw_devices = await self.get_devices_for_home(home["id"])
            _LOGGER.debug("raw devices data: %s", raw_devices)
            if not raw_devices:
                continue
            for raw_device in raw_devices:
                device = await self.get_device(home["id"], raw_device["id"])
                if device is None:
                    _LOGGER.error("Error getting device %s", raw_device)
                    continue
                devices[device.id] = device
        self._devices = devices
        return devices

    async def update_devices(self) -> dict[str, TibberDevice]:
        """Update the devices."""
        tasks = [self.get_device(device.home_id, device.id) for device in self._devices.values()]
        for device in await asyncio.gather(*tasks, return_exceptions=False):
            if device is not None:
                self._devices[device.id] = device
        return self._devices

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

    def __init__(self, device_data: dict[str, Any], home_id: str) -> None:
        """Initialize the device."""
        self._data = device_data
        self._sensors = [Sensor(capability) for capability in device_data["capabilities"]]
        self._home_id = home_id

    @property
    def home_id(self) -> str:
        """Return the home ID."""
        return self._home_id

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

    def __repr__(self) -> str:
        """Return the representation of the device."""
        return f"TibberDevice(id={self.id}, name={self.name}, brand={self.brand}, model={self.model})"


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
        return self._data.get("unit")

    @property
    def value(self) -> SensorValue:
        """Return the device capability value."""
        return self._data.get("value")

    @property
    def description(self) -> str:
        """Return the device capability description."""
        description = self._data.get("description")
        if not description:
            return ""
        return description.capitalize()

    def __repr__(self) -> str:
        """Return the representation of the sensor."""
        return f"Sensor(id={self.id}, unit={self.unit}, value={self.value}, description={self.description})"
