"""Tibber Data API client for REST endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, NoReturn, TypeAlias
from urllib.parse import urljoin, urlsplit

import aiohttp

from .const import DATA_API_ENDPOINT, DEFAULT_TIMEOUT, USERINFO_ENDPOINT, __version__
from .exceptions import (
    FatalHttpExceptionError,
    InsufficientScopeError,
    InvalidLoginError,
    LiveStreamError,
    PricesNotAvailableError,
    RateLimitExceededError,
    RetryableHttpExceptionError,
    UserAgentMissingError,
)
from .live_events import (
    LIVE_EVENTS_READ_TIMEOUT,
    MAX_LIVE_EVENT_DEVICES,
    LiveEventDevice,
    LiveMeasurement,
    iter_sse_events,
    parse_live_measurement,
)
from .response_handler import extract_response_data

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)

MAX_RATE_LIMIT_ATTEMPTS = 2
PRICES_NOT_AVAILABLE_ERROR_TYPE = "https://developer.tibber.com/errors/prices-not-available"
PRICE_INTERVAL: Final = dt.timedelta(minutes=15)
PRICE_PAGE_SPAN: Final = dt.timedelta(hours=48)

SensorValue: TypeAlias = bool | int | float | str | None


@dataclass(frozen=True, slots=True)
class PriceDetails:
    """Prices per kWh, VAT included, for consumption or production in one interval."""

    price: float | None = None
    """What Tibber bills, including markup and fees."""
    subsidized_price: float | None = None
    """`price` after government subsidy, e.g. Norgespris or strømstøtte. None when no subsidy applies."""
    third_party_price: float | None = None
    """What other parties bill, usually the grid company. None when Tibber bills these as part of `price`."""
    vat_percent: float | None = None


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """Prices for one 15-minute interval."""

    start: dt.datetime
    consumption: PriceDetails | None = None
    production: PriceDetails | None = None


@dataclass(frozen=True, slots=True)
class Prices:
    """Price intervals for a home, sorted by start time."""

    currency: str | None
    intervals: list[PriceInterval]


def _format_price_time(value: dt.datetime) -> str:
    """Format an aware time for the Data API, which needs an offset."""
    return value.astimezone(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_data_api_url(url: str) -> bool:
    """Return whether an absolute URL is on the Data API origin."""
    return urlsplit(url)[:2] == urlsplit(DATA_API_ENDPOINT)[:2]


def _parse_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _parse_price_details(data: object) -> PriceDetails | None:
    if not isinstance(data, dict):
        return None
    return PriceDetails(
        price=_parse_float(data.get("price")),
        subsidized_price=_parse_float(data.get("subsidizedPrice")),
        third_party_price=_parse_float(data.get("thirdPartyPrice")),
        vat_percent=_parse_float(data.get("vatPercent")),
    )


def _parse_price_interval(item: object) -> PriceInterval | None:
    if not isinstance(item, dict) or not isinstance(time := item.get("time"), str):
        return None
    try:
        start = dt.datetime.fromisoformat(time)
    except ValueError:
        return None
    if start.utcoffset() is None:
        return None
    return PriceInterval(
        start=start,
        consumption=_parse_price_details(item.get("consumption")),
        production=_parse_price_details(item.get("production")),
    )


def _parse_stream_error(data: str) -> LiveStreamError:
    """Build a LiveStreamError from `stream_error` event data, tolerating malformed data."""
    try:
        payload = json.loads(data)
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("code"), str):
        return LiveStreamError("unknown", transient=True)
    retry_after = payload.get("retryAfter")
    if isinstance(retry_after, bool) or not isinstance(retry_after, int | float):
        retry_after = None
    device_id = payload.get("deviceId")
    return LiveStreamError(
        payload["code"],
        transient=payload.get("transient") is True,
        retry_after=None if retry_after is None else float(retry_after),
        device_id=device_id if isinstance(device_id, str) else None,
    )


class TibberDataAPI:
    """Client for Tibber Data API REST endpoints."""

    def __init__(
        self,
        token_manager: TokenManager,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Initialize the Tibber Data API client.

        :param token_manager: Shared token manager that provides the access token and optional
            refresh callback.  The token is fetched fresh before every request.
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
        self._token_manager = token_manager
        self._user_agent = f"{user_agent} pyTibber/{__version__} "
        self._devices: dict[str, TibberDevice] = {}

    def set_access_token(self, access_token: str) -> None:
        """Set the access token.

        Deprecated: prefer providing a ``refresh_access_token`` callback via
        ``TokenManager`` so all clients share a single token source.

        Do not combine this with a ``refresh_access_token`` callback: when a callback is
        configured it is the authoritative token source and will overwrite any manually
        supplied value on the next refresh invocation.
        """
        self._token_manager.set_access_token(access_token)

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
        :param endpoint: API endpoint path or absolute Data API URL.
        :param params: Query parameters.
        :param retry: Number of retries on failure.
        :param rate_limit_attempt: Number of 429 responses already retried for this request.
        """
        url = urljoin(DATA_API_ENDPOINT, endpoint)
        if not _is_data_api_url(url):
            raise ValueError("Request URL must use the Tibber Data API origin")
        access_token = await self._token_manager.async_get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._user_agent:
            headers[aiohttp.hdrs.USER_AGENT] = self._user_agent

        response: aiohttp.ClientResponse | None = None
        _LOGGER.debug("Request %s: %s with params %s", method, url, params)
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
                    await self._handle_error_response(response, rate_limit_attempt)
                retry_after = response.headers.get("Retry-After")
                wait_time = self._calculate_429_wait_time(retry_after, rate_limit_attempt)
                wait_time = min(wait_time, 10)

                _LOGGER.warning(
                    "Rate limited (429), waiting %.2f seconds before retry (attempt %d/%d)",
                    wait_time,
                    rate_limit_attempt + 1,
                    MAX_RATE_LIMIT_ATTEMPTS,
                )
                await asyncio.sleep(wait_time)

                return await self._make_request(method, endpoint, params, retry, rate_limit_attempt + 1)

            await self._handle_error_response(response, rate_limit_attempt)

        except (aiohttp.ClientError, TimeoutError):
            if retry > 0:
                _LOGGER.debug("Request failed, retrying... (%d attempts left)", retry, exc_info=True)
                return await self._make_request(method, endpoint, params, retry - 1, rate_limit_attempt)
            _LOGGER.debug("Error connecting to Tibber Data API", exc_info=True)
            raise
        finally:
            if response is not None and not response.closed:
                response.close()

    @staticmethod
    def _parse_retry_after(retry_after: str) -> float | None:
        """Parse a Retry-After header value (seconds or HTTP-date) into seconds from now.

        Returns None when the value isn't a usable wait, such as `inf`, `nan` or text that isn't a date.
        A wait that is already over, like a negative number of seconds or a past date, is 0.
        """
        try:
            seconds = float(retry_after)
        except ValueError:
            pass
        else:
            return max(0.0, seconds) if math.isfinite(seconds) else None
        try:
            retry_time = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            try:
                retry_time = dt.datetime.fromisoformat(retry_after)
            except ValueError:
                return None
        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(tzinfo=dt.UTC)
        return max(0.0, (retry_time - dt.datetime.now(dt.UTC)).total_seconds())

    def _calculate_429_wait_time(self, retry_after: str | None, rate_limit_attempt: int = 0) -> float:
        """Calculate wait time for 429 rate limiting.

        :param retry_after: Retry-After header value (seconds or HTTP-date).
        :param rate_limit_attempt: Number of 429 responses already retried for this request.
        :return: Wait time in seconds.
        """
        if retry_after:
            wait_seconds = self._parse_retry_after(retry_after)

            if wait_seconds is not None:
                wait_seconds = min(wait_seconds, 10 * 60)
                jitter = random.uniform(0, 1)  # noqa: S311
                return wait_seconds + jitter

        base = 1.0
        max_wait = base * (2**rate_limit_attempt)
        max_wait = min(max_wait, 10 * 60)
        return random.uniform(0, 30) + max_wait  # noqa: S311

    async def _handle_error_response(self, response: aiohttp.ClientResponse, rate_limit_attempt: int = 0) -> NoReturn:
        """Handle non-OK HTTP responses from the Data API."""
        status = response.status
        if status == HTTPStatus.UNAUTHORIZED:
            response.close()
            raise InvalidLoginError(status, "Invalid token")

        detail, extension_code = await self._read_error_response(response)
        response.close()

        if status == HTTPStatus.FORBIDDEN:
            if extension_code == PRICES_NOT_AVAILABLE_ERROR_TYPE:
                raise PricesNotAvailableError(status, detail, extension_code)
            raise InsufficientScopeError(status, detail, extension_code)

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
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            retry_after = response.headers.get("Retry-After")
            wait_time = self._calculate_429_wait_time(retry_after, rate_limit_attempt)
            raise RateLimitExceededError(
                status,
                detail or "Rate limited",
                extension_code or "RATE_LIMITED",
                wait_time,
            )
        if status == HTTPStatus.PRECONDITION_FAILED:
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

        if response.content_type in {"application/json", "application/problem+json"}:
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

    async def get_prices(self, home_id: str, start: dt.datetime, end: dt.datetime) -> Prices:
        """Get the 15-minute energy prices for a home from `start` up to `end`.

        Requires the data-api-prices-read scope. Pages forward from `start` and follows `next` links
        until `end` or the newest published prices. Published prices never change, so callers should
        store them: tomorrow's prices are published from about 13:00 CET.

        :param home_id: The ID of the home.
        :param start: Timezone aware start, rounded down to 15 minutes by the API.
        :param end: Timezone aware end (exclusive).
        :raises PricesNotAvailableError: The account has no access to prices, the home has no prices,
            or `start` is before the start of the home's price history.
        :raises InsufficientScopeError: The access token lacks the data-api-prices-read scope.
        :raises FatalHttpExceptionError: A page links to another origin than the Data API.
        :raises ValueError: `start` or `end` is not timezone aware.

        Items that aren't valid prices are skipped with a warning. When the page limit is reached before
        `end` while the Data API still links on, that is logged as a warning too and the result can be
        incomplete. A range without prices, such as one where `end` is not after `start`, gives no intervals.
        """
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("Price times must be timezone aware")
        if end <= start:
            return Prices(currency=None, intervals=[])
        endpoint = f"{DATA_API_ENDPOINT}/v1/homes/{home_id}/prices"
        url = endpoint
        params: dict[str, Any] | None = {"since": _format_price_time(start)}
        # Each page holds up to 48 hours. The margin covers price history gaps.
        max_pages = math.ceil((end - start) / PRICE_PAGE_SPAN) + 5
        currency: str | None = None
        intervals: dict[dt.datetime, PriceInterval] = {}
        cursor = start
        for _ in range(max_pages):
            page = await self._make_request("GET", url, params=params)
            currency = page.get("currency") or currency
            items = page.get("items")
            if not isinstance(items, list):
                items = []
            page_intervals = [interval for item in items if (interval := _parse_price_interval(item)) is not None]
            if skipped := len(items) - len(page_intervals):
                _LOGGER.warning("Home %s: skipped %d malformed price items", home_id, skipped)
            intervals.update((interval.start, interval) for interval in page_intervals if start <= interval.start < end)
            if page_intervals:
                cursor = max(interval.start for interval in page_intervals) + PRICE_INTERVAL
            elif cursor >= dt.datetime.now(dt.UTC):
                # An empty page at the present means newer prices aren't published yet.
                break
            # An empty page in the past is a gap in the price history, and `next` skips it.
            next_link = page.get("next")
            if cursor >= end or not next_link or not isinstance(next_link, str):
                break
            url, params = urljoin(endpoint, next_link), None
            if not _is_data_api_url(url):
                raise FatalHttpExceptionError(
                    HTTPStatus.OK,
                    f"A price page links to another origin: {urlsplit(url).netloc!r}",
                    "INVALID_NEXT_LINK",
                )
        else:
            _LOGGER.warning(
                "Home %s: stopped after %d price pages before reaching %s, so the prices may be incomplete",
                home_id,
                max_pages,
                end,
            )

        return Prices(currency=currency, intervals=sorted(intervals.values(), key=lambda interval: interval.start))

    async def get_devices_for_home(self, home_id: str) -> list[dict[str, Any]]:
        """Get all devices for a specific home."""
        response = await self._make_request("GET", f"/v1/homes/{home_id}/devices")
        return response.get("devices", [])

    async def get_live_event_devices(self, home_id: str) -> list[LiveEventDevice]:
        """Get the devices of a home that can stream live events.

        Requires the data-api-meters-read scope; without it the API returns an empty list.
        """
        response = await self._make_request("GET", f"/v1/homes/{home_id}/live-events/devices")
        return [
            LiveEventDevice(id=device["id"], type=device["type"])
            for device in response.get("devices") or []
            if isinstance(device, dict)
            and isinstance(device.get("id"), str)
            and device["id"]
            and isinstance(device.get("type"), str)
            and device["type"]
        ]

    async def stream_live_events(self, home_id: str, device_ids: Sequence[str]) -> AsyncIterator[LiveMeasurement]:
        """Stream live measurements for up to five devices of a home.

        Yields measurements until the server closes the stream. Only one live events connection per
        home is allowed, a new connection evicts the old one.

        :raises ValueError: `device_ids` is empty or has more than five ids.
        :raises LiveStreamError: The server sent a `stream_error` event.
        :raises HttpExceptionError: The server answered with a non-200 status.
        """
        if not 1 <= len(device_ids) <= MAX_LIVE_EVENT_DEVICES:
            raise ValueError(f"Live events need between 1 and {MAX_LIVE_EVENT_DEVICES} device ids")
        access_token = await self._token_manager.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "text/event-stream",
            aiohttp.hdrs.USER_AGENT: self._user_agent,
        }
        response = await self.websession.request(
            "GET",
            f"{DATA_API_ENDPOINT}/v1/homes/{home_id}/live-events",
            headers=headers,
            params=[("deviceId", device_id) for device_id in device_ids],
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.timeout, sock_read=LIVE_EVENTS_READ_TIMEOUT),
        )
        try:
            if response.status != HTTPStatus.OK:
                await self._handle_error_response(response)
            async for event in iter_sse_events(response.content):
                if event.event == "stream_error":
                    raise _parse_stream_error(event.data)
                if event.event != "measurement":
                    continue
                try:
                    payload = json.loads(event.data)
                except ValueError:
                    _LOGGER.debug("Skipping live event with invalid JSON")
                    continue
                measurement = parse_live_measurement(payload)
                if measurement is None:
                    _LOGGER.debug("Skipping unparsable live measurement")
                    continue
                yield measurement
        finally:
            response.release()

    async def get_device(self, home_id: str, device_id: str) -> TibberDevice | None:
        """Get detailed information about a specific device."""
        try:
            response = await self._make_request("GET", f"/v1/homes/{home_id}/devices/{device_id}")
        except FatalHttpExceptionError:
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
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, BaseException):
                _LOGGER.debug("Error getting device %s", result, exc_info=result)
                raise result
            if result is not None:
                self._devices[result.id] = result
        return self._devices

    async def get_userinfo(self) -> dict[str, Any]:
        """Return OpenID Connect user info for the current access token."""
        access_token = await self._token_manager.async_get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
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
            _LOGGER.debug("Error connecting to Tibber user info endpoint", exc_info=True)
            raise


class TibberDevice:
    """Represents a Tibber device from the Data API."""

    def __init__(self, device_data: dict[str, Any], home_id: str) -> None:
        """Initialize the device."""
        self._data = device_data
        self._sensors = [Sensor(capability) for capability in device_data.get("capabilities", [])]
        attributes = device_data.get("attributes", [])
        for attribute in attributes:
            if "id" not in attribute:
                continue
            attr_value = attribute.get("value") if "value" in attribute else attribute.get("status")
            if attr_value is None:
                continue
            sensor_data = {
                "id": attribute["id"],
                "value": attr_value,
            }
            if "description" in attribute:
                sensor_data["description"] = attribute["description"]
            if "unit" in attribute:
                sensor_data["unit"] = attribute["unit"]
            self._sensors.append(Sensor(sensor_data))
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
        """Return the device sensors from both capabilities and attributes."""
        return self._sensors

    @property
    def brand(self) -> str:
        """Return the device brand."""
        return self._data["info"]["brand"]

    @property
    def model(self) -> str | None:
        """Return the device model, if reported."""
        return self._data["info"].get("model")

    def __repr__(self) -> str:
        """Return the representation of the device."""
        return f"TibberDevice(id={self.id}, name={self.name}, brand={self.brand}, model={self.model})"


class Sensor:
    """Represents a Tibber device sensor from capabilities or attributes in the Data API."""

    def __init__(self, capability_data: dict[str, Any]) -> None:
        """Initialize the sensor from capability or attribute data."""
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
