"""Tibber home"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
from typing import TYPE_CHECKING, Callable

from dateutil.parser import parse
from gql import gql

from .const import RESOLUTION_HOURLY
from .gql_queries import (
    HISTORIC_DATA,
    HISTORIC_PRICE,
    LIVE_SUBSCRIBE,
    PRICE_INFO,
    UPDATE_CURRENT_PRICE,
    UPDATE_INFO,
    UPDATE_INFO_PRICE,
)

if TYPE_CHECKING:
    from . import Tibber

_LOGGER = logging.getLogger(__name__)


class HourlyData:
    """Holds hourly data for consumption or production."""

    # pylint: disable=too-few-public-methods
    def __init__(self, production: bool = False):
        self.is_production: bool = production
        self.month_energy: float | None = None
        self.month_money: float | None = None
        self.peak_hour: float | None = None
        self.peak_hour_time: dt.datetime | None = None
        self.last_data_timestamp: dt.datetime | None = None
        self.data: list[dict] = []


class TibberHome:
    """Instance of Tibber home."""

    # pylint: disable=too-many-instance-attributes, too-many-public-methods

    def __init__(self, home_id: str, tibber_control: Tibber) -> None:
        """Initialize the Tibber home class.

        :param home_id: The ID of the home.
        :param tibber_control: The Tibber instance associated with
            this instance of TibberHome.
        """
        self._tibber_control = tibber_control
        self._home_id: str = home_id
        self._current_price_total: float | None = None
        self._current_price_info: dict[str, float] = {}
        self._price_info: dict[str, float] = {}
        self._level_info: dict[str, str] = {}
        self._rt_power: list[tuple[dt.datetime, float]] = []
        self.info: dict[str, dict] = {}
        self.last_data_timestamp: dt.datetime | None = None

        self._hourly_consumption_data: HourlyData = HourlyData()
        self._hourly_production_data: HourlyData = HourlyData(production=True)

    async def _fetch_data(self, hourly_data: HourlyData) -> None:
        """Update hourly consumption or production data asynchronously."""
        # pylint: disable=too-many-branches,too-many-statements,too-many-locals

        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        local_now = now.astimezone(self._tibber_control.time_zone)
        n_hours = 30 * 24

        if self.has_real_time_consumption:
            if (
                not hourly_data.data
                or hourly_data.last_data_timestamp is None
                or parse(hourly_data.data[0]["from"])
                < now - dt.timedelta(hours=n_hours + 24)
            ):
                hourly_data.data = []
            else:
                time_diff = now - hourly_data.last_data_timestamp
                seconds_diff = time_diff.total_seconds()
                n_hours = int(seconds_diff / 3600)
                if n_hours < 1:
                    return
                n_hours = max(2, int(n_hours))
        else:
            if hourly_data.last_data_timestamp is not None and (
                now - hourly_data.last_data_timestamp
            ) < dt.timedelta(hours=24):
                return
            hourly_data.data = []

        data = await self.get_historic_data(
            n_hours, resolution=RESOLUTION_HOURLY, production=hourly_data.is_production
        )

        if hourly_data.is_production:
            direction_name = "production"
            money_name = "profit"
        else:
            direction_name = "consumption"
            money_name = "cost"

        if not data:
            _LOGGER.error("Could not find %s data.", direction_name)
            return None

        if not hourly_data.data:
            hourly_data.data = data
        else:
            hourly_data.data = [
                entry for entry in hourly_data.data if entry not in data
            ]
            hourly_data.data.extend(data)

        _month_energy = 0
        _month_money = 0
        _month_hour_max_month_hour_energy = 0
        _month_hour_max_month_hour: dt.datetime | None = None

        for node in hourly_data.data:
            _time = parse(node["from"])
            if _time.month != local_now.month or _time.year != local_now.year:
                continue
            if (energy := node.get(direction_name)) is None:
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

            if node.get(money_name) is not None:
                _month_money += node[money_name]

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
    def hourly_consumption_data(self) -> list[dict]:
        """Get consumption data for the last 30 days."""
        return self._hourly_consumption_data.data

    @property
    def hourly_production_data(self) -> list[dict]:
        """Get production data for the last 30 days."""
        return self._hourly_production_data.data

    async def update_info(self) -> None:
        """Update home info and the current price info asynchronously."""
        if data := await self._tibber_control.execute(UPDATE_INFO % self._home_id):
            self.info = data

    async def update_info_and_price_info(self) -> None:
        """Update home info and all price info asynchronously."""
        if data := await self._tibber_control.execute(
            UPDATE_INFO_PRICE % self._home_id
        ):
            self.info = data
            self._process_price_info(self.info)

    async def update_current_price_info(self) -> None:
        """Update just the current price info asynchronously."""
        query = UPDATE_CURRENT_PRICE % self.home_id
        price_info_temp = await self._tibber_control.execute(query)
        if not price_info_temp:
            _LOGGER.error("Could not find current price info.")
            return
        try:
            home = price_info_temp["viewer"]["home"]
            current_subscription = home["currentSubscription"]
            price_info = current_subscription["priceInfo"]["current"]
        except (KeyError, TypeError):
            _LOGGER.error("Could not find current price info.")
            return
        if price_info:
            self._current_price_info = price_info

    async def update_price_info(self) -> None:
        """Update the current price info, todays price info
        and tomorrows price info asynchronously."""
        if price_info := await self._tibber_control.execute(PRICE_INFO % self.home_id):
            self._process_price_info(price_info)

    def _process_price_info(self, price_info: dict) -> None:
        """Processes price information retrieved from a GraphQL query.
        The information from the provided dictionary is extracted, then the
        properties of this TibberHome object is updated with this data.

        :param price_info: Price info to retrieve data from.
        """
        if not price_info:
            _LOGGER.error("Could not find price info.")
            return
        self._price_info = {}
        self._level_info = {}
        for key in ["current", "today", "tomorrow"]:
            try:
                price_info_k = price_info["viewer"]["home"]["currentSubscription"][
                    "priceInfo"
                ][key]
            except (KeyError, TypeError):
                _LOGGER.error("Could not find price info for %s.", key)
                continue
            if key == "current":
                self._current_price_info = price_info_k
                continue
            for data in price_info_k:
                self._price_info[data.get("startsAt")] = data.get("total")
                self._level_info[data.get("startsAt")] = data.get("level")
                if (
                    not self.last_data_timestamp
                    or parse(data.get("startsAt")) > self.last_data_timestamp
                ):
                    self.last_data_timestamp = parse(data.get("startsAt"))

    @property
    def current_price_total(self) -> float | None:
        """Get current price total."""
        if not self._current_price_info:
            return None
        return self._current_price_info.get("total")

    @property
    def current_price_info(self) -> dict:
        """Get current price info."""
        return self._current_price_info

    @property
    def price_total(self) -> dict[str, float]:
        """Get dictionary with price total, key is date-time as a string."""
        return self._price_info

    @property
    def price_level(self) -> dict[str, str]:
        """Get dictionary with price level, key is date-time as a string."""
        return self._level_info

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
        return sub in ["running", "awaiting market", "awaiting time restriction"]

    @property
    def has_real_time_consumption(self) -> bool:
        """Return home id."""
        try:
            return self.info["viewer"]["home"]["features"]["realTimeConsumptionEnabled"]
        except (KeyError, TypeError):
            return False

    @property
    def has_production(self) -> bool:
        """Return true if the home has a production metering point."""
        try:
            return bool(
                self.info["viewer"]["home"]["meteringPointData"]["productionEan"]
            )
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
            return self.info["viewer"]["home"]["currentSubscription"]["priceInfo"][
                "current"
            ]["currency"]
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

    def current_price_data(self) -> tuple[float | None, str | None, dt.datetime | None]:
        """Get current price."""
        now = dt.datetime.now(self._tibber_control.time_zone)
        for key, price_total in self.price_total.items():
            price_time = parse(key).astimezone(self._tibber_control.time_zone)
            time_diff = (now - price_time).total_seconds() / 60
            if 0 <= time_diff < 60:
                return round(price_total, 3), self.price_level[key], price_time
        return None, None, None

    async def rt_subscribe(self, callback: Callable) -> None:
        """Connect to Tibber and subscribe to Tibber real time subscription.

        :param callback: The function to call when data is received.
        """

        def _add_extra_data(data: dict) -> dict:
            live_data = data["data"]["liveMeasurement"]
            _time = parse(live_data["timestamp"]).astimezone(
                self._tibber_control.time_zone
            )
            while self._rt_power and self._rt_power[0][0] < _time - dt.timedelta(
                minutes=5
            ):
                self._rt_power.pop(0)

            self._rt_power.append((_time, live_data["power"] / 1000))
            current_hour = live_data["accumulatedConsumptionLastHour"]
            if current_hour is not None:
                power = sum(p[1] for p in self._rt_power) / len(self._rt_power)
                live_data["estimatedHourConsumption"] = round(
                    current_hour
                    + power * (3600 - (_time.minute * 60 + _time.second)) / 3600,
                    3,
                )
                if (
                    self._hourly_consumption_data.peak_hour
                    and current_hour > self._hourly_consumption_data.peak_hour
                ):
                    self._hourly_consumption_data.peak_hour = round(current_hour, 2)
                    self._hourly_consumption_data.peak_hour_time = _time
            return data

        async def _disconnect():
            await asyncio.sleep(30)
            _LOGGER.debug("No data received for 30 seconds, reconnecting")
            await self._tibber_control.rt_disconnect()

        async def _start():
            """Subscribe to Tibber."""
            _retry_count = 0
            restarter = asyncio.create_task(_disconnect())
            while True:
                try:
                    await self._tibber_control.rt_connect()
                    async for data in self._tibber_control.sub_manager.session.subscribe(
                        gql(LIVE_SUBSCRIBE % self.home_id)
                    ):
                        restarter.cancel()
                        restarter = asyncio.create_task(_disconnect())
                        data = {"data": data}
                        try:
                            data = _add_extra_data(data)
                        except KeyError:
                            pass
                        callback(data)
                        _retry_count = 0
                except Exception:  # pylint: disable=broad-except
                    delay_seconds = min(
                        random.SystemRandom().randint(1, 60) + _retry_count**2,
                        60 * 60,
                    )
                    _LOGGER.error(
                        "Tibber connection closed, will reconnect in %s seconds",
                        delay_seconds,
                    )
                    _retry_count += 1
                    await asyncio.sleep(delay_seconds)

        asyncio.create_task(_start())

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        return self._tibber_control.rt_subscription_running

    async def get_historic_data(
        self, n_data: int, resolution: str = RESOLUTION_HOURLY, production: bool = False
    ) -> list[dict]:
        """Get historic data.

        :param n_data: The number of nodes to get from history. e.g. 5 would give 5 nodes
            and resolution = hourly would give the 5 last hours of historic data
        :param resolution: The resolution of the data. Can be HOURLY,
            DAILY, WEEKLY, MONTHLY or ANNUAL
        :param production: True to get production data instead of consumption
        """
        cursor = ""
        res = []
        max_n_data = 7000
        while n_data > 0:
            _n_data = min(max_n_data, n_data)
            cons_or_prod_str = "production" if production else "consumption"
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
            n_data -= len(data["nodes"])
            if (
                not data["pageInfo"]["hasPreviousPage"]
                or not data["pageInfo"]["startCursor"]
            ):
                if n_data > 0:
                    max_n_data = max_n_data // 10
                    if max_n_data < 1:
                        _LOGGER.warning("Found less data than requested")
                        break
                    continue
                break
            cursor = data["pageInfo"]["startCursor"]
        return res

    async def get_historic_price_data(
        self,
        resolution: str = RESOLUTION_HOURLY,
    ) -> list[dict] | None:
        """Get historic price data.
        :param resolution: The resolution of the data. Can be HOURLY,
            DAILY, WEEKLY, MONTHLY or ANNUAL
        """
        query = HISTORIC_PRICE.format(
            self.home_id,
            resolution.lower(),
        )
        if not (data := await self._tibber_control.execute(query)):
            _LOGGER.error("Could not get the price data.")
            return None
        return data["viewer"]["home"]["currentSubscription"]["priceRating"][
            resolution.lower()
        ]["entries"]

    def current_attributes(self) -> dict:
        """Get current attributes."""
        # pylint: disable=too-many-locals
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
        for key, price_total in self.price_total.items():
            price_time = parse(key).astimezone(self._tibber_control.time_zone)
            price_total = round(price_total, 3)
            if now.date() == price_time.date():
                max_price = max(max_price, price_total)
                min_price = min(min_price, price_total)
                if price_time.hour < 8:
                    off_peak_1 += price_total
                    num1 += 1
                elif price_time.hour < 20:
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
        if (
            "glitre"
            in (
                self.info["viewer"]["home"]["meteringPointData"].get("gridCompany")
                or ""
            ).lower()
        ):
            if now.month < 7:
                grid_price = 47.39 / 100
            else:
                grid_price = 47.25 / 100
            if now.hour >= 22 or now.hour < 6:
                grid_price -= 12 / 100
            attr["grid_price"] = round(grid_price, 3)
            _LOGGER.warning(
                "Grid price attribute is deprecated and will be removed. "
                "Use https://github.com/Danielhiversen/home_assistant_glitre"
            )
        return attr
