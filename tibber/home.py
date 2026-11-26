"""Tibber home"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import random
from collections import deque
from contextlib import aclosing
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final, cast

import aiohttp

from .const import CURRENCY_BY_COUNTRY, RESOLUTION_DAILY, RESOLUTION_HOURLY, RESOLUTION_MONTHLY, RESOLUTION_WEEKLY
from .data_api import PRICE_INTERVAL
from .exceptions import (
    FatalHttpExceptionError,
    HttpExceptionError,
    InvalidLoginError,
    LiveStreamError,
    PricesNotAvailableError,
    RateLimitExceededError,
    RealTimeConsumptionDisabledError,
)
from .gql_queries import (
    HISTORIC_DATA,
    HISTORIC_DATA_DATE,
    REAL_TIME_CONSUMPTION_ENABLED,
    UPDATE_INFO,
)
from .live_events import MAX_LIVE_EVENT_DEVICES

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from . import Tibber
    from .data_api import PriceInterval
    from .live_events import LiveEventDevice, LiveMeasurement

_LOGGER = logging.getLogger(__name__)

REAL_TIME_CONSUMPTION_DISABLED_GRACE = dt.timedelta(hours=1)

MIN_REQUESTED_CONSUMPTION_HOURS: int = 3

RESUBSCRIBE_MAX_ATTEMPTS: int = 5


def _price_period_start(local_start: dt.datetime, resolution: str) -> dt.datetime:
    """Return the start of the hour, day or month that a time in the home's time zone is in."""
    if resolution == RESOLUTION_HOURLY:
        return local_start.replace(minute=0, second=0, microsecond=0)
    day = dt.datetime.combine(local_start.date(), dt.time(), tzinfo=local_start.tzinfo)
    return day if resolution == RESOLUTION_DAILY else day.replace(day=1)


LIVE_RECONNECT_MIN_DELAY: Final = 1
LIVE_RECONNECT_MAX_DELAY: Final = 60
EVICTED_RETRY_DELAY: Final = 300
NO_LIVE_DEVICES_RETRY_DELAY: Final = 900
LIVE_POWER_WINDOW: Final = dt.timedelta(minutes=5)

# Errors the live stream is expected to raise. Anything else is logged with its traceback.
_EXPECTED_LIVE_ERRORS: Final = (
    aiohttp.ClientError,
    TimeoutError,
    HttpExceptionError,
    LiveStreamError,
    RealTimeConsumptionDisabledError,
)


def _reconnect_delay(attempt: int) -> float:
    """Return a random backoff for reconnect `attempt`: 1 s plus up to 2**attempt s, at most 1 + 60 s."""
    # Capping the exponent keeps 2**attempt small after a long outage, without changing the result.
    exponent = min(attempt, LIVE_RECONNECT_MAX_DELAY.bit_length())
    return LIVE_RECONNECT_MIN_DELAY + random.uniform(0, min(2**exponent, LIVE_RECONNECT_MAX_DELAY))  # noqa: S311


def _live_retry_delay(error: Exception | None, attempt: int, consecutive_401: int) -> float | None:  # noqa: PLR0911
    """Return the seconds to wait before connecting again after a stream ended with `error`, or None to stop."""
    if isinstance(error, InvalidLoginError):
        # Connecting again fetches a fresh token, so the first 401 in a row is retried at once.
        return 0 if consecutive_401 == 1 else None
    if isinstance(error, FatalHttpExceptionError):
        return None
    if isinstance(error, RateLimitExceededError):
        return error.retry_after
    if isinstance(error, LiveStreamError):
        if error.transient and (error.retry_after or 0) > 0:
            return error.retry_after
        if error.transient or error.code == "device_not_streamable":
            # The next discovery leaves out a device that can't stream.
            return _reconnect_delay(attempt)
        # `evicted`: another client took over the home's only live connection, so don't fight over it.
        return EVICTED_RETRY_DELAY
    if isinstance(error, RealTimeConsumptionDisabledError):
        return NO_LIVE_DEVICES_RETRY_DELAY
    return _reconnect_delay(attempt)


async def _sleep(delay: float) -> None:
    """Wait between live stream connections. A function of its own, so that tests can replace it."""
    await asyncio.sleep(delay)


class HourlyData:
    """Holds hourly data for consumption or production."""

    def __init__(self, production: bool = False) -> None:
        self.is_production: bool = production
        self.month_energy: float | None = None
        self.month_money: float | None = None
        self.peak_hour: float | None = None
        self.peak_hour_time: dt.datetime | None = None
        self.last_data_timestamp: dt.datetime | None = None
        self.data: list[dict[Any, Any]] = []

    @property
    def direction_name(self) -> str:
        """Return the direction name."""
        if self.is_production:
            return "production"
        return "consumption"

    @property
    def money_name(self) -> str:
        """Return the money name."""
        if self.is_production:
            return "profit"
        return "cost"


class TibberHome:
    """Instance of Tibber home."""

    def __init__(self, home_id: str, tibber_control: Tibber) -> None:
        """Initialize the Tibber home class.

        :param home_id: The ID of the home.
        :param tibber_control: The Tibber instance associated with
            this instance of TibberHome.
        """
        self._tibber_control = tibber_control
        self._home_id: str = home_id
        self._currency: str | None = None
        self.prices: list[PriceInterval] = []
        self.live_event_devices: list[LiveEventDevice] = []
        self.info: dict[str, dict[Any, Any]] = {}
        self.last_data_timestamp: dt.datetime | None = None

        self._hourly_consumption_data = HourlyData()
        self._hourly_production_data = HourlyData(production=True)
        self._rt_task: asyncio.Task[None] | None = None
        self._rt_streaming = False
        # The (timestamp, kW) power readings of the last five minutes, per live device.
        self._rt_power: dict[str, deque[tuple[dt.datetime, float]]] = {}
        self._has_real_time_consumption: None | bool = None
        self._real_time_consumption_suggested_disabled: dt.datetime | None = None

    def _merge_hourly_data(
        self,
        existing_data: list[dict[Any, Any]],
        new_data: list[dict[Any, Any]],
    ) -> list[dict[Any, Any]]:
        """Merge hourly payloads by timestamp and prefer the newest entry."""
        merged_by_timestamp: dict[str, dict[Any, Any]] = {}

        for entry in existing_data:
            if (timestamp := entry.get("from")) and isinstance(timestamp, str):
                merged_by_timestamp[timestamp] = entry

        for entry in new_data:
            if (timestamp := entry.get("from")) and isinstance(timestamp, str):
                merged_by_timestamp[timestamp] = entry

        return list(merged_by_timestamp.values())

    async def _fetch_data(self, hourly_data: HourlyData) -> None:
        """Update hourly consumption or production data asynchronously."""
        now = dt.datetime.now(tz=dt.UTC)
        local_now = now.astimezone(self._tibber_control.time_zone)
        n_hours = 60 * 24

        if (
            not hourly_data.data
            or hourly_data.last_data_timestamp is None
            or dt.datetime.fromisoformat(hourly_data.data[0]["from"]) < now - dt.timedelta(hours=n_hours + 24)
        ):
            hourly_data.data = []
        else:
            time_diff = now - hourly_data.last_data_timestamp
            seconds_diff = time_diff.total_seconds()
            n_hours = int(seconds_diff / 3600)
            if n_hours < 1:
                return
            n_hours = max(MIN_REQUESTED_CONSUMPTION_HOURS, int(n_hours))

        data = await self.get_historic_data(
            n_hours,
            resolution=RESOLUTION_HOURLY,
            production=hourly_data.is_production,
        )

        if not data:
            _LOGGER.error("Could not find %s data.", hourly_data.direction_name)
            return

        if not hourly_data.data:
            hourly_data.data = data
        else:
            hourly_data.data = self._merge_hourly_data(hourly_data.data, data)

        _month_energy = 0
        _month_money = 0
        _month_hour_max_month_hour_energy = 0
        _month_hour_max_month_hour: dt.datetime | None = None

        for node in hourly_data.data:
            _time = dt.datetime.fromisoformat(node["from"])
            if _time.month != local_now.month or _time.year != local_now.year:
                continue
            if (energy := node.get(hourly_data.direction_name)) is None:
                continue

            if (
                hourly_data.last_data_timestamp is None
                or _time + dt.timedelta(hours=1) > hourly_data.last_data_timestamp
            ):
                hourly_data.last_data_timestamp = _time + dt.timedelta(hours=1)
            if energy > _month_hour_max_month_hour_energy:
                _month_hour_max_month_hour_energy = energy
                _month_hour_max_month_hour = _time
            _month_energy += energy

            if node.get(hourly_data.money_name) is not None:
                _month_money += node[hourly_data.money_name]

        hourly_data.month_energy = round(_month_energy, 2)
        hourly_data.month_money = round(_month_money, 2)
        hourly_data.peak_hour = round(_month_hour_max_month_hour_energy, 2)
        hourly_data.peak_hour_time = _month_hour_max_month_hour

    async def fetch_consumption_data(self) -> None:
        """Update consumption info asynchronously."""
        return await self._fetch_data(self._hourly_consumption_data)

    async def fetch_production_data(self) -> None:
        """Update consumption info asynchronously."""
        return await self._fetch_data(self._hourly_production_data)

    @property
    def month_cons(self) -> float | None:
        """Get consumption for current month."""
        return self._hourly_consumption_data.month_energy

    @property
    def month_cost(self) -> float | None:
        """Get total cost for current month."""
        return self._hourly_consumption_data.month_money

    @property
    def peak_hour(self) -> float | None:
        """Get consumption during peak hour for the current month."""
        return self._hourly_consumption_data.peak_hour

    @property
    def peak_hour_time(self) -> dt.datetime | None:
        """Get the time for the peak consumption during the current month."""
        return self._hourly_consumption_data.peak_hour_time

    @property
    def last_cons_data_timestamp(self) -> dt.datetime | None:
        """Get last consumption data timestampt."""
        return self._hourly_consumption_data.last_data_timestamp

    @property
    def hourly_consumption_data(self) -> list[dict[Any, Any]]:
        """Get consumption data for the last 30 days."""
        return self._hourly_consumption_data.data

    @property
    def hourly_production_data(self) -> list[dict[Any, Any]]:
        """Get production data for the last 30 days."""
        return self._hourly_production_data.data

    async def update_info(self) -> None:
        """Update home info asynchronously."""
        if not (data := await self._tibber_control.execute(UPDATE_INFO % self._home_id)):
            _LOGGER.error("Could not get the data.")
            return
        self.info = data

        self._update_has_real_time_consumption(self._extract_real_time_consumption_enabled(data))

    async def get_prices(self, start: dt.datetime, end: dt.datetime) -> list[PriceInterval]:
        """Get the 15-minute prices from `start` up to `end` from the Data API.

        Does not change `prices`. See `TibberDataAPI.get_prices` for the errors raised.
        """
        prices = await self._tibber_control.data_api.get_prices(self._home_id, start, end)
        if prices.currency:
            self._currency = prices.currency
        return prices.intervals

    def _first_unknown_price_start(self, start: dt.datetime) -> dt.datetime:
        """Return the start of the first 15-minute interval from `start` on that `prices` doesn't have.

        A hole in `prices` counts as unknown, so a price that was missed once is fetched again.
        """
        # Calculating in UTC keeps the intervals of a day with a change to or from summer time 15 minutes apart.
        expected = start.astimezone(dt.UTC)
        for interval in self.prices:
            if interval.start < start:
                continue
            if interval.start != expected:
                break
            expected += PRICE_INTERVAL
        return expected

    async def update_prices(self) -> None:
        """Update `prices` with today's and, once published, tomorrow's prices.

        Published prices never change, so only the prices that `prices` doesn't have yet are fetched: the
        first call of a day gets today's, and later ones only add tomorrow's once they are published, from
        about 13:00 CET. Nothing is fetched when `prices` is complete. Yesterday's prices are dropped.

        A home without prices gets no prices, other errors are raised and the known prices are kept.
        """
        today = dt.datetime.now(self._tibber_control.time_zone).date()
        start = dt.datetime.combine(today, dt.time(), tzinfo=self._tibber_control.time_zone)
        end = dt.datetime.combine(today + dt.timedelta(days=2), dt.time(), tzinfo=self._tibber_control.time_zone)
        since = self._first_unknown_price_start(start)
        known = [interval for interval in self.prices if start <= interval.start < since]
        try:
            new = await self.get_prices(since, end) if since < end else []
        except PricesNotAvailableError as err:
            _LOGGER.debug("No prices for home %s: %s", self._home_id, err)
            self.prices = []
            return
        self.prices = known + new

    async def update_real_time_consumption_enabled(self) -> None:
        """Update the real time consumption enabled status."""
        if not (data := await self._tibber_control.execute(REAL_TIME_CONSUMPTION_ENABLED % self._home_id)):
            _LOGGER.error("Could not get real time consumption enabled status.")
            return
        self._update_has_real_time_consumption(self._extract_real_time_consumption_enabled(data))

    @staticmethod
    def _extract_real_time_consumption_enabled(data: dict[str, Any]) -> bool | None:
        """Safely extract the real time consumption enabled flag from an info payload."""
        try:
            value = data["viewer"]["home"]["features"]["realTimeConsumptionEnabled"]
        except (KeyError, TypeError):
            return None
        # Anything but a bool carries no usable information, treat it like a malformed payload.
        return value if isinstance(value, bool) else None

    def _update_has_real_time_consumption(self, enabled: bool | None) -> None:
        """Update the believed real time consumption status from a single reading.

        `enabled` is None when the reading carried no usable information (a malformed or
        degraded API payload) and must not change the believed status at all.

        A `False` reading following a known `True` does not immediately flip the status: it only
        arms a grace-period timer and the home keeps reporting its last known `True`, so a single
        bad or transiently-correct reading during a Tibber-side outage cannot strand the real time
        subscription. The status flips to `False` only when a later `False` reading arrives more
        than REAL_TIME_CONSUMPTION_DISABLED_GRACE after the timer was armed. Both a `True` reading
        and incoming real time data (see `_handle_subscription_data`) clear the timer, so the grace
        period always measures the time since the last evidence that real time consumption works.
        """
        if enabled is None:
            return

        if enabled is True:
            self._real_time_consumption_suggested_disabled = None
            self._has_real_time_consumption = True
            return

        # enabled is False from here on.
        if self._has_real_time_consumption is not True:
            self._real_time_consumption_suggested_disabled = None
            self._has_real_time_consumption = False
            return

        now = dt.datetime.now(tz=dt.UTC)
        if self._real_time_consumption_suggested_disabled is None:
            self._real_time_consumption_suggested_disabled = now
        elif now - self._real_time_consumption_suggested_disabled > REAL_TIME_CONSUMPTION_DISABLED_GRACE:
            self._real_time_consumption_suggested_disabled = None
            self._has_real_time_consumption = False

    @property
    def home_id(self) -> str:
        """Return home id."""
        return self._home_id

    @property
    def has_active_subscription(self) -> bool:
        """Return home id."""
        try:
            sub = self.info["viewer"]["home"]["currentSubscription"]["status"]
        except (KeyError, TypeError):
            return False
        return sub in [
            "running",
            "awaiting market",
            "awaiting time restriction",
            "awaiting termination",
        ]

    @property
    def has_real_time_consumption(self) -> None | bool:
        """Return home id."""
        return self._has_real_time_consumption

    @property
    def has_production(self) -> bool:
        """Return true if the home has a production metering point."""
        try:
            return bool(self.info["viewer"]["home"]["meteringPointData"]["productionEan"])
        except (KeyError, TypeError):
            return False

    @property
    def address1(self) -> str:
        """Return the home adress1."""
        try:
            return self.info["viewer"]["home"]["address"]["address1"]
        except (KeyError, TypeError):
            _LOGGER.error("Could not find address1.")
        return ""

    @property
    def consumption_unit(self) -> str:
        """Return the consumption unit."""
        return "kWh"

    @property
    def currency(self) -> str:
        """Return the currency of the prices, from the Data API or else the home's country."""
        return self._currency or CURRENCY_BY_COUNTRY.get(self.country, "")

    @property
    def country(self) -> str:
        """Return the country."""
        try:
            return self.info["viewer"]["home"]["address"]["country"]
        except (KeyError, TypeError):
            _LOGGER.error("Could not find country.")
            return ""

    @property
    def name(self) -> str:
        """Return the name."""
        try:
            return self.info["viewer"]["home"]["appNickname"]
        except (KeyError, TypeError):
            return self.info["viewer"]["home"]["address"].get("address1", "")

    @property
    def price_unit(self) -> str:
        """Return the price unit (e.g. NOK/kWh)."""
        if not self.currency or not self.consumption_unit:
            _LOGGER.error("Could not find price_unit.")
            return ""
        return self.currency + "/" + self.consumption_unit

    def _local_start(self, interval: PriceInterval) -> dt.datetime:
        return interval.start.astimezone(self._tibber_control.time_zone)

    def prices_for_day(self, day: dt.date) -> list[PriceInterval]:
        """Return the price intervals that start on a day in the home's time zone."""
        return [interval for interval in self.prices if self._local_start(interval).date() == day]

    @property
    def current_price_interval(self) -> PriceInterval | None:
        """Return the prices for the current 15-minute interval."""
        now = dt.datetime.now(dt.UTC)
        for interval in self.prices:
            if interval.start <= now < interval.start + PRICE_INTERVAL:
                return interval
        return None

    def current_price_data(self) -> tuple[float | None, dt.datetime | None, float | None]:
        """Return the current consumption price, its start time and its rank (0-1] among today's prices."""
        interval = self.current_price_interval
        if interval is None or interval.consumption is None or (price := interval.consumption.price) is None:
            return None, None, None
        price_time = self._local_start(interval)
        prices_today = sorted(
            consumption.price
            for other in self.prices_for_day(price_time.date())
            if (consumption := other.consumption) is not None and consumption.price is not None
        )
        rank = (prices_today.index(price) + 1) / len(prices_today)
        return round(price, 4), price_time, rank

    async def update_live_event_devices(self) -> list[LiveEventDevice]:
        """Update `live_event_devices` with the devices of the home that can stream live data.

        Without the data-api-meters-read scope the home has no live devices.
        """
        self.live_event_devices = await self._tibber_control.data_api.get_live_event_devices(self._home_id)
        return self.live_event_devices

    async def rt_subscribe(
        self,
        callback: Callable[[LiveMeasurement], None],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Stream live measurements from the home's Pulse or Watty in a background task.

        Returns at once. The task discovers the live devices, streams the first five, and connects
        again whenever the stream ends or fails, until `rt_unsubscribe` or an error that retrying
        can't fix.

        :param callback: Called with every measurement. Exceptions it raises are logged.
        :param on_error: Called with every error instead of logging it, though unexpected errors are
            logged with their traceback as well. The task stops after a second `InvalidLoginError` in
            a row or any other `FatalHttpExceptionError`, such as an `InsufficientScopeError`, and
            carries on after anything else. A home without live devices is reported as a
            `RealTimeConsumptionDisabledError` every 15 minutes. Once the task has stopped,
            `rt_subscribe` can start a new one.
        """
        if self._rt_task is not None and not self._rt_task.done():
            raise RuntimeError("Already subscribed to real time data, call rt_unsubscribe first")
        _LOGGER.debug("Subscribe, %s", self._home_id)
        self._rt_task = asyncio.create_task(self._rt_run(callback, on_error))

    def rt_unsubscribe(self) -> None:
        """Stop streaming live measurements. The task is cancelled and ends in the background."""
        _LOGGER.debug("Unsubscribe, %s", self._home_id)
        if self._rt_task is not None:
            self._rt_task.cancel()
            self._rt_task = None
        self._rt_streaming = False

    @property
    def rt_subscription_running(self) -> bool:
        """Return True while a live stream is open and has delivered a measurement."""
        return self._rt_streaming

    async def _rt_run(
        self,
        callback: Callable[[LiveMeasurement], None],
        on_error: Callable[[Exception], None] | None,
    ) -> None:
        """Stream live measurements and connect again until cancelled or an error retrying can't fix."""
        # The number of connections since the last measurement, which sets the backoff.
        attempt = 0
        consecutive_401 = 0
        while True:
            error: Exception | None = None
            try:
                device_ids = await self._rt_device_ids()
                stream = cast(
                    "AsyncGenerator[LiveMeasurement, None]",
                    self._tibber_control.data_api.stream_live_events(self._home_id, device_ids),
                )
                # aclosing releases the response at once when the loop body raises.
                async with aclosing(stream):
                    async for measurement in stream:
                        enriched = self._enrich_measurement(measurement)
                        # Only a measurement that enriched cleanly resets the backoff.
                        attempt = 0
                        self._rt_streaming = True
                        try:
                            callback(enriched)
                        except Exception:
                            _LOGGER.exception("Error in rt_subscribe callback")
            except Exception as err:  # noqa: BLE001
                error = err
            finally:
                self._rt_streaming = False

            consecutive_401 = consecutive_401 + 1 if isinstance(error, InvalidLoginError) else 0
            delay = _live_retry_delay(error, attempt, consecutive_401)
            if error is not None:
                self._rt_report(error, on_error, stopping=delay is None)
            if delay is None:
                return
            if delay > 0:
                await _sleep(delay)
            attempt += 1

    async def _rt_device_ids(self) -> list[str]:
        """Discover the home's live devices and return the ids of the ones to stream."""
        devices = await self.update_live_event_devices()
        if not devices:
            raise RealTimeConsumptionDisabledError(
                f"Home {self._home_id} has no live devices, or the token lacks the data-api-meters-read scope",
            )
        if len(devices) > MAX_LIVE_EVENT_DEVICES:
            _LOGGER.warning(
                "Home %s has %d live devices, streaming the first %d",
                self._home_id,
                len(devices),
                MAX_LIVE_EVENT_DEVICES,
            )
        return [device.id for device in devices[:MAX_LIVE_EVENT_DEVICES]]

    def _rt_report(
        self,
        error: Exception,
        on_error: Callable[[Exception], None] | None,
        *,
        stopping: bool,
    ) -> None:
        """Pass a live stream error to `on_error`, or log it when there is none.

        Unexpected errors are always logged, with their traceback.
        """
        if not isinstance(error, _EXPECTED_LIVE_ERRORS):
            _LOGGER.error("Unexpected error in the live stream of home %s", self._home_id, exc_info=error)
        elif on_error is None:
            _LOGGER.log(
                logging.ERROR if stopping else logging.WARNING,
                "Live stream of home %s: %s: %s, %s",
                self._home_id,
                type(error).__name__,
                error,
                "stopping" if stopping else "retrying",
            )
        if on_error is not None:
            try:
                on_error(error)
            except Exception:
                _LOGGER.exception("Error in rt_subscribe on_error callback")

    def _enrich_measurement(self, measurement: LiveMeasurement) -> LiveMeasurement:
        """Return the measurement with the values pyTibber derives, and update the peak hour."""
        power, power_production = measurement.power, measurement.power_production
        if power is None and power_production is not None and power_production > 0:
            power = 0.0
        if power is not None and power > 0 and power_production is None:
            power_production = 0.0
        last_meter_production = measurement.last_meter_production
        if last_meter_production is not None:
            last_meter_production = max(0.0, last_meter_production)

        estimated_hour_consumption = measurement.estimated_hour_consumption
        if (timestamp := measurement.timestamp) is not None:
            window = self._rt_power.setdefault(measurement.device_id, deque())
            while window and window[0][0] < timestamp - LIVE_POWER_WINDOW:
                window.popleft()
            if power is not None:
                window.append((timestamp, power / 1000))
            if (current_hour := measurement.accumulated_consumption_last_hour) is not None:
                local_time = timestamp.astimezone(self._tibber_control.time_zone)
                if window:
                    average_kw = sum(kw for _, kw in window) / len(window)
                    seconds_left = 3600 - (local_time.minute * 60 + local_time.second)
                    estimated_hour_consumption = round(current_hour + average_kw * seconds_left / 3600, 3)
                consumption = self._hourly_consumption_data
                if consumption.peak_hour and current_hour > consumption.peak_hour:
                    consumption.peak_hour = round(current_hour, 2)
                    consumption.peak_hour_time = local_time

        return replace(
            measurement,
            power=power,
            power_production=power_production,
            last_meter_production=last_meter_production,
            estimated_hour_consumption=estimated_hour_consumption,
        )

    async def get_historic_data(
        self,
        n_data: int,
        resolution: str = RESOLUTION_HOURLY,
        production: bool = False,
    ) -> list[dict[str, Any]]:
        """Get historic data.

        :param n_data: The number of nodes to get from history. e.g. 5 would give 5 nodes
            and resolution = hourly would give the 5 last hours of historic data
        :param resolution: The resolution of the data. Can be HOURLY,
            DAILY, WEEKLY, MONTHLY or ANNUAL
        :param production: True to get production data instead of consumption
        """
        cons_or_prod_str = "production" if production else "consumption"
        res: list[dict[str, Any]] = []
        if resolution == RESOLUTION_HOURLY:
            max_n_data = 24 * 31
        elif resolution == RESOLUTION_DAILY:
            max_n_data = 31
        elif resolution == RESOLUTION_WEEKLY:
            max_n_data = 52
        elif resolution == RESOLUTION_MONTHLY:
            max_n_data = 12
        else:
            max_n_data = 1
        cursor = ""
        for _ in range(n_data // max_n_data + 1):
            _n_data = min(max_n_data, n_data - len(res))
            query = HISTORIC_DATA.format(
                self.home_id,
                cons_or_prod_str,
                resolution,
                _n_data,
                "profit" if production else "totalCost cost",
                cursor,
            )
            if not (data := await self._tibber_control.execute(query)):
                _LOGGER.error("Could not get the data.")
                continue
            data = data["viewer"]["home"][cons_or_prod_str]
            if data is None:
                continue
            res.extend(data["nodes"])
            if len(res) >= n_data:
                break
            cursor = data["pageInfo"]["startCursor"]
            if cursor is None:
                break
        return res

    async def get_historic_data_date(
        self,
        date_from: dt.datetime,
        n_data: int,
        resolution: str = RESOLUTION_HOURLY,
        production: bool = False,
    ) -> list[dict[str, Any]]:
        """Get historic data.
        :param date_from: The start-date to get the data from
        :param n_data: The number of nodes to get from history. e.g. 5 would give 5 nodes
            and resolution = hourly would give the 5 last hours of historic data.
            If 0 the set month-days will be calculated to the end of the month.
        :param resolution: The resolution of the data. Can be HOURLY,
            DAILY, WEEKLY, MONTHLY or ANNUAL
        :param production: True to get production data instead of consumption
        """

        date_from_base64 = base64.b64encode(date_from.strftime("%Y-%m-%d").encode()).decode("utf-8")

        if n_data == 0:
            # Calculate the number of days to the end of the month from the given date
            n_data = (date_from.replace(day=1, month=date_from.month + 1) - date_from).days

        cons_or_prod_str = "production" if production else "consumption"
        query = HISTORIC_DATA_DATE.format(
            self.home_id,
            cons_or_prod_str,
            resolution,
            n_data,
            date_from_base64,
            "profit production productionUnit" if production else "cost consumption consumptionUnit",
        )

        if not (data := await self._tibber_control.execute(query, timeout=30)):
            _LOGGER.error("Could not get the data.")
            return []

        data = data["viewer"]["home"][cons_or_prod_str]

        if data is None:
            return []

        return data["nodes"]

    async def get_historic_price_data(
        self,
        start: dt.datetime,
        end: dt.datetime,
        resolution: str = RESOLUTION_HOURLY,
    ) -> list[dict[str, Any]]:
        """Get the mean consumption price per hour, day or month from the Data API.

        The Data API has 15-minute prices only, so the prices of each period are averaged here. Periods
        are in the home's time zone. A period that `start` or `end` cuts off only has the prices in the
        range, and a period without a consumption price is left out.

        :param start: Timezone aware start of the range.
        :param end: Timezone aware end of the range (exclusive).
        :param resolution: The period of each entry, HOURLY, DAILY or MONTHLY.
        :return: The entries, oldest first, as `{"time": start of the period, "total": mean price}`.
        :raises ValueError: The resolution is not supported.

        See `TibberDataAPI.get_prices` for the errors that fetching the prices can raise.
        """
        resolution = resolution.upper()
        if resolution not in {RESOLUTION_HOURLY, RESOLUTION_DAILY, RESOLUTION_MONTHLY}:
            raise ValueError(f"Unsupported resolution {resolution}, use HOURLY, DAILY or MONTHLY")
        periods: dict[dt.datetime, list[float]] = {}
        # The intervals are sorted, so the periods are as well. Their start in UTC tells apart the two
        # hours that a change from summer time repeats.
        for interval in await self.get_prices(start, end):
            if interval.consumption is None or interval.consumption.price is None:
                continue
            period_start = _price_period_start(self._local_start(interval), resolution)
            periods.setdefault(period_start.astimezone(dt.UTC), []).append(interval.consumption.price)
        return [
            {
                "time": period_start.astimezone(self._tibber_control.time_zone).isoformat(),
                "total": round(sum(prices) / len(prices), 4),
            }
            for period_start, prices in periods.items()
        ]

    def current_attributes(self) -> dict[str, float]:
        """Return today's max, average and min consumption price.

        Also returns the average price for off peak 1 (00-08), peak (08-20) and off peak 2 (20-24).
        Without a consumption price today it returns nothing, as zero would be a real price.
        """
        today = dt.datetime.now(self._tibber_control.time_zone).date()
        prices: list[float] = []
        periods: dict[str, list[float]] = {"off_peak_1": [], "peak": [], "off_peak_2": []}
        for interval in self.prices_for_day(today):
            if interval.consumption is None or interval.consumption.price is None:
                continue
            price = round(interval.consumption.price, 4)
            hour = self._local_start(interval).hour
            period = "off_peak_1" if hour < 8 else "peak" if hour < 20 else "off_peak_2"  # noqa: PLR2004
            periods[period].append(price)
            prices.append(price)
        if not prices:
            return {}

        def _average(values: list[float]) -> float:
            return round(sum(values) / len(values), 4) if values else 0.0

        return {
            "max_price": max(prices),
            "avg_price": _average(prices),
            "min_price": min(prices),
            **{period: _average(values) for period, values in periods.items()},
        }
