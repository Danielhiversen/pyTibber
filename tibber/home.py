"""Tibber home"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import logging
import random
import time
import warnings
from typing import TYPE_CHECKING, Any

import aiohttp
from gql import gql

from .const import CURRENCY_BY_COUNTRY, RESOLUTION_DAILY, RESOLUTION_HOURLY, RESOLUTION_MONTHLY, RESOLUTION_WEEKLY
from .data_api import PRICE_INTERVAL
from .exceptions import (
    HttpExceptionError,
    PricesNotAvailableError,
    RealTimeConsumptionDisabledError,
    SubscriptionFailedError,
    WebsocketReconnectedError,
    WebsocketTransportError,
)
from .gql_queries import (
    HISTORIC_DATA,
    HISTORIC_DATA_DATE,
    LIVE_SUBSCRIBE,
    REAL_TIME_CONSUMPTION_ENABLED,
    UPDATE_INFO,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from . import Tibber
    from .data_api import PriceInterval

_LOGGER = logging.getLogger(__name__)

RT_SUBSCRIPTION_TIMEOUT = 60
RESUBSCRIBE_WAIT_TIME = 60
REAL_TIME_CONSUMPTION_DISABLED_GRACE = dt.timedelta(hours=1)

MIN_REQUESTED_CONSUMPTION_HOURS: int = 3

RESUBSCRIBE_MAX_ATTEMPTS: int = 5


def _price_period_start(local_start: dt.datetime, resolution: str) -> dt.datetime:
    """Return the start of the hour, day or month that a time in the home's time zone is in."""
    if resolution == RESOLUTION_HOURLY:
        return local_start.replace(minute=0, second=0, microsecond=0)
    day = dt.datetime.combine(local_start.date(), dt.time(), tzinfo=local_start.tzinfo)
    return day if resolution == RESOLUTION_DAILY else day.replace(day=1)


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
        self._rt_power: list[tuple[dt.datetime, float]] = []
        self.info: dict[str, dict[Any, Any]] = {}
        self.last_data_timestamp: dt.datetime | None = None

        self._hourly_consumption_data = HourlyData()
        self._hourly_production_data = HourlyData(production=True)
        self._last_rt_data_received: float | None = None
        self._rt_listener: asyncio.Task[None] | None = None
        self._rt_callback: Callable[..., Any] | None = None
        self._rt_on_error: Callable[[Exception], None] | None = None
        self._rt_subscription_timeout_task: asyncio.Task[None] | None = None
        self._has_real_time_consumption: None | bool = None
        self._real_time_consumption_suggested_disabled: dt.datetime | None = None
        self._resubscribe_task: asyncio.Task[None] | None = None

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

    async def rt_subscribe(
        self,
        callback: Callable[..., Any],
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Connect to Tibber and subscribe to Tibber real time subscription.

        :param callback: The function to call when data is received.
        :param on_error: The function to call when an error occurs. Most errors are transient and
            resubscription continues automatically, but a `RealTimeConsumptionDisabledError` is
            terminal: the resubscribe loop has stopped and will not retry on its own. To resume,
            call `rt_subscribe` again, from a separate task rather than directly from this
            callback, and rate limit the retries.
        """
        if self._rt_listener is not None:
            raise RuntimeError("Already subscribed to real time data, call rt_unsubscribe first")
        _LOGGER.debug("Subscribe, %s", self.home_id)
        self._rt_callback = callback
        self._rt_on_error = on_error
        await self._rt_resubscribe()

    async def _rt_subscribe(self) -> None:
        """Subscribe to Tibber real time subscription."""
        await self._tibber_control.realtime.connect()
        self._rt_listener = asyncio.create_task(self._start_listen())
        self._rt_subscription_timeout_task = asyncio.create_task(
            self._rt_subscription_timeout(),
        )

    async def _start_listen(self) -> None:
        """Subscribe to Tibber."""
        on_error = self._rt_on_error
        try:
            async for _data in self._tibber_control.realtime.subscribe(
                gql(
                    LIVE_SUBSCRIBE % self.home_id,
                ),
                on_error=on_error,
            ):
                self._handle_subscription_data(_data)
        except WebsocketReconnectedError as err:
            _LOGGER.debug("Websocket reconnected for home %s, restarting subscription", self.home_id)
            if on_error:
                on_error(err)
        except Exception as err:
            if not isinstance(err, WebsocketTransportError):
                _LOGGER.exception("Error in subscription")
            else:
                level = logging.DEBUG if on_error is not None else logging.ERROR
                _LOGGER.log(
                    level,
                    "Error in subscription for home %s: %s: %s",
                    self.home_id,
                    err.__class__.__name__,
                    err,
                )
            if on_error is not None:
                on_error(err)

        await asyncio.sleep(random.random() * RESUBSCRIBE_WAIT_TIME)  # noqa: S311
        self._schedule_resubscribe()

    def _handle_subscription_data(self, _data: dict[str, Any]) -> None:
        """Handle incoming real time subscription data.

        Record that data was received to keep the subscription timeout watchdog
        from treating a healthy, active subscription as unresponsive, and treat the live
        measurement as a successful `True` status reading: it proves real time consumption is
        working right now, and it is the only such proof available while a subscription runs
        healthily. This both clears any suspected-disable grace timer and restores the believed
        status to `True`, so live data can recover a status that was already flipped to `False`.
        """
        self._last_rt_data_received = time.time()
        if self._has_real_time_consumption is not True or self._real_time_consumption_suggested_disabled is not None:
            _LOGGER.debug(
                "Real time data received for home %s, treating live measurement as proof "
                "real time consumption is enabled",
                self.home_id,
            )
            self._update_has_real_time_consumption(True)
        data = {"data": _data}
        try:
            data = self._add_extra_data(data)
        except KeyError as err:
            _LOGGER.debug("Missing expected key in rt_subscribe data, skipping enrichment: %s", err)
        except Exception:
            _LOGGER.exception("Error processing rt_subscribe data")
        if self._rt_callback is None:
            raise RuntimeError("No subscription callback set, call rt_subscribe first")
        try:
            self._rt_callback(data)
        except Exception:
            _LOGGER.exception("Error in rt_subscribe callback")
            return
        _LOGGER.debug(
            "Data received for %s: %s",
            self.home_id,
            data,
        )

    def _add_extra_data(self, data: dict[str, Any]) -> dict[str, Any]:
        live_data = data["data"]["liveMeasurement"]
        _timestamp = dt.datetime.fromisoformat(live_data["timestamp"]).astimezone(self._tibber_control.time_zone)
        while self._rt_power and self._rt_power[0][0] < _timestamp - dt.timedelta(minutes=5):
            self._rt_power.pop(0)

        self._rt_power.append((_timestamp, live_data["power"] / 1000))
        if "lastMeterProduction" in live_data:
            live_data["lastMeterProduction"] = max(0, live_data["lastMeterProduction"] or 0)

        if (
            (power_production := live_data.get("powerProduction"))
            and power_production > 0
            and live_data.get("power") is None
        ):
            live_data["power"] = 0

        if live_data.get("power", 0) > 0 and live_data.get("powerProduction") is None:
            live_data["powerProduction"] = 0

        current_hour = live_data["accumulatedConsumptionLastHour"]
        if current_hour is not None:
            power = sum(p[1] for p in self._rt_power) / len(self._rt_power)
            live_data["estimatedHourConsumption"] = round(
                current_hour + power * (3600 - (_timestamp.minute * 60 + _timestamp.second)) / 3600,
                3,
            )
            if self._hourly_consumption_data.peak_hour and current_hour > self._hourly_consumption_data.peak_hour:
                self._hourly_consumption_data.peak_hour = round(current_hour, 2)
                self._hourly_consumption_data.peak_hour_time = _timestamp
        return data

    def _schedule_resubscribe(self) -> None:
        if self._resubscribe_task is not None:
            # Cancelling the resubscribe task is a defensive no-op,
            # since the the only callers of _schedule_resubscribe,
            # _rt_listener and _rt_subscription_timeout_task, get cancelled on resubscribe.
            self._resubscribe_task.cancel()
        self._resubscribe_task = asyncio.create_task(self._rt_resubscribe())

    async def rt_resubscribe(self) -> None:
        """Resubscribe to Tibber data.

        Deprecated. Resubscription will happen automatically.
        """
        warnings.warn(
            "TibberHome.rt_resubscribe is deprecated, resubscription will happen automatically",
            DeprecationWarning,
            stacklevel=2,
        )
        if self._rt_callback is None:
            raise RuntimeError("No callback set for rt_resubscribe, call rt_subscribe first")
        await self._rt_resubscribe()

    async def _rt_resubscribe(self) -> None:
        """Resubscribe to Tibber data."""
        _LOGGER.info("Resubscribe, %s", self.home_id)
        self.rt_unsubscribe()

        await self._resubscribe_step(
            self.update_real_time_consumption_enabled(),
            "Failed to refresh real time consumption status, keeping last known status",
        )
        if self.has_real_time_consumption is False:
            _LOGGER.info("Home %s does not have real time consumption enabled", self.home_id)
            if (on_error := self._rt_on_error) is not None:
                on_error(
                    RealTimeConsumptionDisabledError(
                        f"Home {self.home_id} does not have real time consumption enabled",
                    ),
                )
            return

        # Update info to set websocket subscription url
        await self._resubscribe_step(
            self._tibber_control.update_info(),
            "Failed to update info, keeping last known info",
        )

        await self._rt_subscribe()

    async def _resubscribe_step(self, coro: Awaitable[Any], failure_message: str) -> None:
        """Await a resubscribe step, logging a warning on failure instead of aborting."""
        try:
            await coro
        except (TimeoutError, aiohttp.ClientError):
            # Transport errors are already logged at debug level inside execute.
            _LOGGER.warning("Home %s: %s", self.home_id, failure_message)
        except HttpExceptionError as err:
            # Known API errors (e.g. an expired token) are expected here and don't warrant a traceback.
            _LOGGER.warning("Home %s: %s: %s", self.home_id, failure_message, err)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Home %s: %s", self.home_id, failure_message, exc_info=True)

    def rt_unsubscribe(self) -> None:
        """Unsubscribe to Tibber data."""
        _LOGGER.debug("Unsubscribe, %s", self.home_id)
        if self._rt_listener is not None:
            self._rt_listener.cancel()
            self._rt_listener = None
        if self._rt_subscription_timeout_task is not None:
            self._rt_subscription_timeout_task.cancel()
            self._rt_subscription_timeout_task = None
        self._last_rt_data_received = None

    async def _rt_subscription_timeout(self) -> None:
        """Resubscribe if realtime subscription is unresponsive."""
        on_error = self._rt_on_error
        while True:
            # Add some random time to avoid all homes resubscribing at the same time
            # if there is an issue with the subscription
            await asyncio.sleep(RT_SUBSCRIPTION_TIMEOUT + random.random() * RT_SUBSCRIPTION_TIMEOUT)  # noqa: S311
            if (
                self._last_rt_data_received is not None
                and time.time() - self._last_rt_data_received <= RT_SUBSCRIPTION_TIMEOUT
            ):
                continue
            if on_error:
                on_error(
                    SubscriptionFailedError(
                        f"No real time data received for home {self.home_id} "
                        f"in the last {RT_SUBSCRIPTION_TIMEOUT} seconds",
                    ),
                )
            else:
                _LOGGER.error(
                    "No real time data received for home %s in the last %d seconds, reconnecting and resubscribing",
                    self.home_id,
                    RT_SUBSCRIPTION_TIMEOUT,
                )
            if self._rt_listener is not None:
                self._rt_listener.cancel()
                self._rt_listener = None
            await self._tibber_control.realtime.reconnect()
            self._schedule_resubscribe()

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        return self._tibber_control.realtime.subscription_running and self._rt_listener is not None

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
