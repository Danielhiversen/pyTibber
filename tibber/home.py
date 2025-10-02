"""Tibber home"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from gql import gql

from .const import RESOLUTION_HOURLY
from .gql_queries import (
    HISTORIC_DATA,
    HISTORIC_DATA_DATE,
    HISTORIC_PRICE,
    LIVE_SUBSCRIBE,
    UPDATE_INFO_PRICE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from . import Tibber

_LOGGER = logging.getLogger(__name__)

MIN_IN_HOUR: int = 60
MIN_IN_QUARTER: int = 15


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
        self.price_total: dict[str, float] = {}
        self._rt_power: list[tuple[dt.datetime, float]] = []
        self.info: dict[str, dict[Any, Any]] = {}
        self.last_data_timestamp: dt.datetime | None = None

        self._hourly_consumption_data: HourlyData = HourlyData()
        self._hourly_production_data: HourlyData = HourlyData(production=True)
        self._last_rt_data_received: dt.datetime = dt.datetime.now(tz=dt.UTC)
        self._rt_listener: None | asyncio.Task[Any] = None
        self._rt_callback: Callable[..., Any] | None = None
        self._rt_stopped: bool = True
        self._has_real_time_consumption: None | bool = None
        self._real_time_consumption_suggested_disabled: dt.datetime | None = None

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
            n_hours = max(2, int(n_hours))

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
            hourly_data.data = [entry for entry in hourly_data.data if entry not in data]
            hourly_data.data.extend(data)

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
        """Update home info and the current price info asynchronously."""
        await self.update_info_and_price_info()

    async def update_info_and_price_info(self) -> None:
        """Update home info and all price info asynchronously."""
        if not (data := await self._tibber_control.execute(UPDATE_INFO_PRICE % self._home_id)):
            _LOGGER.error("Could not get the data.")
            return
        self.info = data

        # Handle inactive homes where currentSubscription might be None
        # Access currentSubscription, handle missing keys
        try:
            viewer = self.info["viewer"]
        except KeyError as err:
            _LOGGER.error("Missing 'viewer' key in info for home %s: %s", self._home_id, err)
            self.price_total = {}
            return
        try:
            home = viewer["home"]
        except KeyError as err:
            _LOGGER.error("Missing 'home' key in viewer for home %s: %s", self._home_id, err)
            self.price_total = {}
            return
        
        self._update_has_real_time_consumption()

        current_subscription = home.get("currentSubscription")
        if current_subscription is None:
            _LOGGER.debug("No active subscription for home %s", self._home_id)
            self.price_total = {}
            return

        price_info = current_subscription.get("priceInfo")
        if price_info is None:
            _LOGGER.debug("No price info available for home %s", self._home_id)
            self.price_total = {}
            return

        today = price_info.get("today", [])
        tomorrow = price_info.get("tomorrow", [])
        try:
            self.price_total = {item["startsAt"]: item["total"] for item in today + tomorrow}
        except (KeyError, TypeError) as err:
            _LOGGER.error("Malformed price info data for home %s: %s", self._home_id, err)
            self.price_total = {}

    def _update_has_real_time_consumption(self) -> None:
        try:
            _has_real_time_consumption = self.info["viewer"]["home"]["features"]["realTimeConsumptionEnabled"]
        except (KeyError, TypeError):
            self._has_real_time_consumption = None
            return
        if self._has_real_time_consumption is None:
            self._has_real_time_consumption = _has_real_time_consumption
            return

        if self._has_real_time_consumption is True and _has_real_time_consumption is False:
            now = dt.datetime.now(tz=dt.UTC)
            if self._real_time_consumption_suggested_disabled is None:
                self._real_time_consumption_suggested_disabled = now
                self._has_real_time_consumption = None
            elif now - self._real_time_consumption_suggested_disabled > dt.timedelta(hours=1):
                self._real_time_consumption_suggested_disabled = None
                self._has_real_time_consumption = False
            else:
                self._has_real_time_consumption = None
            return

        if _has_real_time_consumption is True:
            self._real_time_consumption_suggested_disabled = None
        self._has_real_time_consumption = _has_real_time_consumption

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
        """Return the currency."""
        try:
            return self.info["viewer"]["home"]["currentSubscription"]["priceInfo"]["current"]["currency"]
        except (KeyError, TypeError, IndexError):
            _LOGGER.error("Could not find currency.")
        return ""

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

    def current_price_rank(self, price_total: dict[str, float], price_time: dt.datetime | None) -> float | None:
        """Gets the rank (0-1) of how expensive the current price is compared to the other prices today."""
        # No price -> no rank
        if price_time is None:
            return None
        # Map price_total to a list of tuples (datetime, float)
        price_items_typed: list[tuple[dt.datetime, float]] = [
            (
                dt.datetime.fromisoformat(time).astimezone(self._tibber_control.time_zone),
                price,
            )
            for time, price in price_total.items()
        ]

        # Filter out prices not from today, sort by price
        prices_today_sorted = sorted(
            [item for item in price_items_typed if item[0].date() == price_time.date()],
            key=lambda x: x[1],
        )
        # Find the rank of the current price
        for idx, item in enumerate(prices_today_sorted, start=1):
            if item[0] == price_time:
                return idx / len(prices_today_sorted)
        return None

    def current_price_data(self) -> tuple[float | None, dt.datetime | None, float | None]:
        """Get current price."""
        now = dt.datetime.now(self._tibber_control.time_zone)
        for key, price_total in self.price_total.items():
            price_time = dt.datetime.fromisoformat(key).astimezone(self._tibber_control.time_zone)
            time_diff = (now - price_time).total_seconds() / MIN_IN_HOUR
            if 0 <= time_diff < MIN_IN_QUARTER:
                price_rank = self.current_price_rank(self.price_total, price_time)
                return round(price_total, 3), price_time, price_rank
        return None, None, None

    async def rt_subscribe(self, callback: Callable[..., Any]) -> None:
        """Connect to Tibber and subscribe to Tibber real time subscription.

        :param callback: The function to call when data is received.
        """

        def _add_extra_data(data: dict[str, Any]) -> dict[str, Any]:
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

        async def _start() -> None:
            """Subscribe to Tibber."""
            for _ in range(30):
                if self._rt_stopped:
                    _LOGGER.debug("Stopping rt_subscribe")
                    return
                if self._tibber_control.realtime.subscription_running:
                    break

                _LOGGER.debug("Waiting for rt_connect")
                await asyncio.sleep(1)
            else:
                _LOGGER.error("rt not running")
                return

            try:
                session = self._tibber_control.realtime.sub_manager.session
                if not hasattr(session, "subscribe"):
                    _LOGGER.error("Session does not support subscribe method")
                    return
                async for _data in session.subscribe(
                    gql(LIVE_SUBSCRIBE % self.home_id),
                ):
                    data = {"data": _data}
                    with contextlib.suppress(KeyError):
                        data = _add_extra_data(data)
                    callback(data)
                    self._last_rt_data_received = dt.datetime.now(tz=dt.UTC)
                    _LOGGER.debug(
                        "Data received for %s: %s",
                        self.home_id,
                        data,
                    )
                    if self._rt_stopped or not self._tibber_control.realtime.subscription_running:
                        _LOGGER.debug("Stopping rt_subscribe loop")
                        return
            except Exception:
                _LOGGER.exception("Error in rt_subscribe")

        self._rt_callback = callback
        self._tibber_control.realtime.add_home(self)
        await self._tibber_control.realtime.connect()
        self._rt_listener = asyncio.create_task(_start())
        self._rt_stopped = False

    async def rt_resubscribe(self) -> None:
        """Resubscribe to Tibber data."""
        self.rt_unsubscribe()
        _LOGGER.debug("Resubscribe, %s", self.home_id)
        await asyncio.gather(
            *[
                self.update_info(),
                self._tibber_control.update_info(),
            ],
            return_exceptions=False,
        )
        if self._rt_callback is None:
            _LOGGER.warning("No callback set for rt_resubscribe")
            return
        await self.rt_subscribe(self._rt_callback)

    def rt_unsubscribe(self) -> None:
        """Unsubscribe to Tibber data."""
        _LOGGER.debug("Unsubscribe, %s", self.home_id)
        self._rt_stopped = True
        if self._rt_listener is None:
            return
        self._rt_listener.cancel()
        self._rt_listener = None

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        if not self._tibber_control.realtime.subscription_running:
            return False
        return not self._last_rt_data_received < dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=60)

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
        query = HISTORIC_DATA.format(
            self.home_id,
            cons_or_prod_str,
            resolution,
            n_data,
            "profit" if production else "totalCost cost",
            "",
        )
        if not (data := await self._tibber_control.execute(query, timeout=30)):
            _LOGGER.error("Could not get the data.")
            return []
        data = data["viewer"]["home"][cons_or_prod_str]
        if data is None:
            return []
        return data["nodes"]

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
        resolution: str = RESOLUTION_HOURLY,
    ) -> list[dict[Any, Any]] | None:
        """Get historic price data.
        :param resolution: The resolution of the data. Can be HOURLY,
            DAILY, WEEKLY, MONTHLY or ANNUAL
        """
        resolution = resolution.lower()
        query = HISTORIC_PRICE.format(
            self.home_id,
            resolution,
        )
        if not (data := await self._tibber_control.execute(query)):
            _LOGGER.error("Could not get the price data.")
            return None
        return data["viewer"]["home"]["currentSubscription"]["priceRating"][resolution]["entries"]

    def current_attributes(self) -> dict[str, float]:
        """Get current attributes."""
        max_price = 0.0
        min_price = 10000.0
        sum_price = 0.0
        off_peak_1 = 0.0
        peak = 0.0
        off_peak_2 = 0.0
        num1 = 0.0
        num0 = 0.0
        num2 = 0.0
        num = 0.0
        now = dt.datetime.now(self._tibber_control.time_zone)
        for key, _price_total in self.price_total.items():
            price_time = dt.datetime.fromisoformat(key).astimezone(self._tibber_control.time_zone)
            price_total = round(_price_total, 3)
            if now.date() == price_time.date():
                max_price = max(max_price, price_total)
                min_price = min(min_price, price_total)
                if price_time.hour < 8:  # noqa: PLR2004
                    off_peak_1 += price_total
                    num1 += 1
                elif price_time.hour < 20:  # noqa: PLR2004
                    peak += price_total
                    num0 += 1
                else:
                    off_peak_2 += price_total
                    num2 += 1
                num += 1
                sum_price += price_total

        attr = {}
        attr["max_price"] = max_price
        attr["avg_price"] = round(sum_price / num, 3) if num > 0 else 0
        attr["min_price"] = min_price
        attr["off_peak_1"] = round(off_peak_1 / num1, 3) if num1 > 0 else 0
        attr["peak"] = round(peak / num0, 3) if num0 > 0 else 0
        attr["off_peak_2"] = round(off_peak_2 / num2, 3) if num2 > 0 else 0
        return attr
