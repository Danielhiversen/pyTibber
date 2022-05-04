"""Library to handle connection with Tibber API."""
import asyncio
import datetime as dt
import logging
from typing import Callable, Optional, Union

import aiohttp
import async_timeout
import pytz
from dateutil.parser import parse
from graphql_subscription_manager import SubscriptionManager

from .const import RESOLUTION_HOURLY, __version__

DEFAULT_TIMEOUT = 10
DEMO_TOKEN = "476c477d8a039529478ebd690d35ddd80e3308ffc49b59c65b142321aee963a4"
API_ENDPOINT = "https://api.tibber.com/v1-beta/gql"
SUB_ENDPOINT = "wss://api.tibber.com/v1-beta/gql/subscriptions"

_LOGGER = logging.getLogger(__name__)


class Tibber:
    """Class to communicate with the Tibber api."""

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        access_token: str = DEMO_TOKEN,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession = None,
        # Union can be replaced with | format in Python 3.10 and higher
        time_zone: Union[pytz.BaseTzInfo, str] = None,
    ):
        """Initialize the Tibber connection."""
        if websession is None:
            self.websession = aiohttp.ClientSession(
                headers={aiohttp.hdrs.USER_AGENT: f"pyTibber/{__version__}"}
            )
        else:
            self.websession = websession
        self._timeout = timeout
        self._access_token = access_token
        self.time_zone = time_zone or pytz.utc
        self._name = None
        self._user_id = None
        self._home_ids = []
        self._all_home_ids = []
        self._homes = {}
        self.sub_manager = None
        try:
            user_agent = self.websession._default_headers.get(  # pylint: disable=protected-access
                aiohttp.hdrs.USER_AGENT, ""
            )  # will be fixed by aiohttp 4.0
        except Exception:  # pylint: disable=broad-except
            user_agent = ""
        self.user_agent = f"{user_agent} pyTibber/{__version__}"

    async def close_connection(self) -> None:
        """Close the Tibber connection."""
        await self.websession.close()

    async def rt_connect(self) -> None:
        """Start subscription manager for real time data."""
        if self.sub_manager is not None:
            return
        self.sub_manager = SubscriptionManager(
            {"token": self._access_token},
            SUB_ENDPOINT,
            self.user_agent,
        )
        self.sub_manager.start()

    async def rt_disconnect(self) -> None:
        """Stop subscription manager."""
        if self.sub_manager is None:
            return
        await self.sub_manager.stop()

    async def execute(self, document: dict, variable_values=None) -> Union[dict, None]:
        """Execute gql."""
        if (res := await self._execute(document, variable_values)) is None:
            return None
        return res.get("data")

    async def _execute(
        self, document: dict, variable_values: dict = None, retry: int = 2
    ) -> Union[dict, None]:
        """Execute gql."""
        payload = {"query": document, "variables": variable_values or {}}

        post_args = {
            "headers": {
                "Authorization": "Bearer " + self._access_token,
                aiohttp.hdrs.USER_AGENT: self.user_agent,
            },
            "data": payload,
        }
        try:
            async with async_timeout.timeout(self._timeout):
                resp = await self.websession.post(API_ENDPOINT, **post_args)
            if resp.status != 200:
                _LOGGER.error("Error connecting to Tibber, resp code: %s", resp.status)
                return None
            result = await resp.json()
        except aiohttp.ClientError as err:
            if retry > 0:
                return await self._execute(document, variable_values, retry - 1)
            _LOGGER.error("Error connecting to Tibber: %s ", err, exc_info=True)
            raise
        except asyncio.TimeoutError:
            _LOGGER.error("Timed out when connecting to Tibber")
            raise
        if errors := result.get("errors"):
            _LOGGER.error("Received non-compatible response %s", errors)
        return result

    async def update_info(self, *_: Optional[None]) -> None:
        """Update home info async."""
        query = """
        {
          viewer {
            name
            userId
            homes {
              id
              subscriptions {
                status
              }
            }
          }
        }
        """

        if (res := await self._execute(query)) is None:
            return
        if errors := res.get("errors", []):
            msg = errors[0].get("message", "failed to login")
            _LOGGER.error(msg)
            raise InvalidLogin(msg)

        if not (data := res.get("data")):
            return

        if not (viewer := data.get("viewer")):
            return
        self._name = viewer.get("name")
        self._user_id = viewer.get("userId")

        homes = viewer.get("homes", [])
        self._home_ids = []
        for _home in homes:
            home_id = _home.get("id")
            self._all_home_ids += [home_id]
            subs = _home.get("subscriptions")
            if not subs:
                continue
            status = subs[0].get("status", "ended").lower()
            if not home_id or status != "running":
                continue
            self._home_ids += [home_id]

    @property
    def user_id(self) -> str:
        """Return user id of user."""
        return self._user_id

    @property
    def name(self) -> str:
        """Return name of user."""
        return self._name

    @property
    def home_ids(self) -> list[str]:
        """Return list of home ids."""
        return self.get_home_ids(only_active=True)

    def get_home_ids(self, only_active=True) -> list[str]:
        """Return list of home ids."""
        if only_active:
            return self._home_ids
        return self._all_home_ids

    def get_homes(self, only_active: bool = True) -> list[TibberHome]:
        """Return list of Tibber homes."""
        return [self.get_home(home_id) for home_id in self.get_home_ids(only_active)]

    def get_home(self, home_id) -> TibberHome:
        """Return an instance of TibberHome for given home id."""
        if home_id not in self._all_home_ids:
            _LOGGER.error("Could not find any Tibber home with id: %s", home_id)
            return None
        if home_id not in self._homes:
            self._homes[home_id] = TibberHome(home_id, self)
        return self._homes[home_id]

    async def send_notification(self, title, message) -> bool:
        """Send notification."""
        # pylint: disable=consider-using-f-string)
        query = """
        mutation{{
          sendPushNotification(input: {{
            title: "{}",
            message: "{}",
          }}){{
            successful
            pushedToNumberOfDevices
          }}
        }}
        """.format(
            title,
            message,
        )

        if not (res := await self.execute(query)):
            return False
        noti = res.get("sendPushNotification", {})
        successful = noti.get("successful", False)
        pushed_to_number_of_devices = noti.get("pushedToNumberOfDevices", 0)
        _LOGGER.debug(
            "send_notification: status %s, send to %s devices",
            successful,
            pushed_to_number_of_devices,
        )
        return successful

    async def fetch_consumption_data_active_homes(self) -> None:
        """Fetch consumption data for active homes."""
        tasks = []
        for home in self.get_homes(only_active=True):
            tasks.append(home.fetch_consumption_data())
        await asyncio.gather(*tasks)


class TibberHome:
    """Instance of Tibber home."""

    # pylint: disable=too-many-instance-attributes, too-many-public-methods

    def __init__(self, home_id: str, tibber_control: Tibber):
        """Initialize the Tibber home class."""
        self._tibber_control = tibber_control
        self._home_id = home_id
        self._current_price_total = None
        self._current_price_info = {}
        self._price_info = {}
        self._level_info = {}
        self._rt_power = []
        self.info = {}
        self._subscription_id = None
        self._data = None
        self.last_data_timestamp = None

        self.month_cons = None
        self.month_cost = None
        self.peak_hour = None
        self.peak_hour_time = None
        self.last_cons_data_timestamp = None
        self.hourly_consumption_data = []

    async def fetch_consumption_data(self) -> None:
        """Update consumption info async."""
        # pylint: disable=too-many-branches

        now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
        local_now = now.astimezone(self._tibber_control.time_zone)
        n_hours = 30 * 24

        if self.has_real_time_consumption:
            if not self.hourly_consumption_data or parse(
                self.hourly_consumption_data[0]["from"]
            ) < now - dt.timedelta(hours=n_hours + 24):
                self.hourly_consumption_data = []
            else:
                n_hours = (now - self.last_cons_data_timestamp).total_seconds() / 3600
                if n_hours < 1:
                    return
                n_hours = max(2, int(n_hours))
        else:
            if self.last_cons_data_timestamp is not None and (
                now - self.last_cons_data_timestamp
            ) < dt.timedelta(hours=24):
                return
            self.hourly_consumption_data = []

        consumption = await self.get_historic_data(
            n_hours, resolution=RESOLUTION_HOURLY
        )

        if not consumption:
            _LOGGER.error("Could not find consumption data.")
            return consumption

        if not self.hourly_consumption_data:
            self.hourly_consumption_data = consumption
        else:
            self.hourly_consumption_data = [
                _cons
                for _cons in self.hourly_consumption_data
                if _cons not in consumption
            ]
            self.hourly_consumption_data.extend(consumption)

        _month_cons = 0
        _month_cost = 0
        _month_hour_max_month_hour_cons = 0
        _month_hour_max_month_hour = None

        for node in self.hourly_consumption_data:
            _time = parse(node["from"])
            if _time.month != local_now.month or _time.year != local_now.year:
                continue
            if node.get("consumption") is None:
                continue

            if (
                self.last_cons_data_timestamp is None
                or _time + dt.timedelta(hours=1) > self.last_cons_data_timestamp
            ):
                self.last_cons_data_timestamp = _time + dt.timedelta(hours=1)
            if node["consumption"] > _month_hour_max_month_hour_cons:
                _month_hour_max_month_hour_cons = node["consumption"]
                _month_hour_max_month_hour = _time
            _month_cons += node["consumption"]

            if node.get("cost") is not None:
                _month_cost += node["cost"]

        self.month_cons = round(_month_cons, 2)
        self.month_cost = round(_month_cost, 2)
        self.peak_hour = round(_month_hour_max_month_hour_cons, 2)
        self.peak_hour_time = _month_hour_max_month_hour

    async def update_info(self) -> None:
        """Update current price info async."""
        # pylint: disable=consider-using-f-string)
        query = (
            """
        {
          viewer {
            home(id: "%s") {
              appNickname
              features {
                  realTimeConsumptionEnabled
                }
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
             currentSubscription {
                    priceInfo {
                      current {
                        currency
                      }
                    }
                  }
                }
              }
            }
        """
            % self._home_id
        )

        self.info = await self._tibber_control.execute(query)

    async def update_info_and_price_info(self) -> None:
        """Update current price info async."""
        # pylint: disable=consider-using-f-string)
        query = (
            """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                    level
                  }
                  today {
                    total
                    startsAt
                    level
                  }
                  tomorrow {
                    total
                    startsAt
                    level
                  }
                }
              }
              appNickname
              features {
                realTimeConsumptionEnabled
              }
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
              currentSubscription {
                priceInfo {
                  current {
                    currency
                  }
                }
              }
            }
          }
        }

        """
            % self._home_id
        )

        self.info = await self._tibber_control.execute(query)
        self._process_price_info(self.info)

    async def update_current_price_info(self) -> None:
        """Update current price info async."""
        # pylint: disable=consider-using-f-string)
        query = (
            """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                  }
                }
              }
            }
          }
        }
        """
            % self.home_id
        )
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
        """Update price info async."""
        # pylint: disable=consider-using-f-string)
        query = (
            """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                    level
                  }
                  today {
                    total
                    startsAt
                    level
                  }
                  tomorrow {
                    total
                    startsAt
                    level
                  }
                }
              }
            }
          }
        }
        """
            % self.home_id
        )
        price_info = await self._tibber_control.execute(query)
        self._process_price_info(price_info)

    def _process_price_info(self, price_info: dict) -> None:
        if not price_info:
            _LOGGER.error("Could not find price info.")
            return
        self._price_info = {}
        self._level_info = {}
        for key in ["current", "today", "tomorrow"]:
            try:
                home = price_info["viewer"]["home"]
                current_subscription = home["currentSubscription"]
                price_info_k = current_subscription["priceInfo"][key]
            except (KeyError, TypeError):
                _LOGGER.error("Could not find price info for %s.", key)
                continue
            if key == "current":
                self._current_price_info = price_info_k
                continue
            for data in price_info_k:
                self._price_info[data.get("startsAt")] = data.get("total")
                self._level_info[data.get("startsAt")] = data.get("level")

    @property
    def current_price_total(self) -> float:
        """Get current price total."""
        if not self._current_price_info:
            return None
        return self._current_price_info.get("total")

    @property
    def current_price_info(self) -> dict:
        """Get current price info."""
        return self._current_price_info

    @property
    def price_total(self) -> dict[dt.datetime, float]:
        """Get dictionary with price total, key is date-time."""
        return self._price_info

    @property
    def price_level(self) -> dict[dt.datetime, str]:
        """Get dictionary with price level, key is date-time."""
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
            current_subscription = self.info["viewer"]["home"]["currentSubscription"]
            return current_subscription["priceInfo"]["current"]["currency"]
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
        name = None
        try:
            name = self.info["viewer"]["home"]["appNickname"]
        except (KeyError, TypeError):
            pass
        if name:
            return name
        return self.info["viewer"]["home"]["address"].get("address1", "")

    @property
    def price_unit(self) -> str:
        """Return the price unit."""
        if not self.currency or not self.consumption_unit:
            _LOGGER.error("Could not find price_unit.")
            return " "
        return self.currency + "/" + self.consumption_unit

    async def rt_subscribe(self, callback: Callable) -> None:
        """Connect to Tibber and subscribe to Tibber rt subscription."""
        if self._subscription_id is not None:
            _LOGGER.error("Already subscribed.")
            return
        await self._tibber_control.rt_connect()
        # pylint: disable=consider-using-f-string)
        document = (
            """
            subscription{
              liveMeasurement(homeId:"%s"){
                accumulatedConsumption
                accumulatedConsumptionLastHour
                accumulatedCost
                accumulatedProduction
                accumulatedProductionLastHour
                accumulatedReward
                averagePower
                currency
                currentL1
                currentL2
                currentL3
                lastMeterConsumption
                lastMeterProduction
                maxPower
                minPower
                power
                powerFactor
                powerProduction
                powerReactive
                signalStrength
                timestamp
                voltagePhase1
                voltagePhase2
                voltagePhase3
            }
           }
        """
            % self.home_id
        )

        def _add_extra_data(data: dict) -> dict:
            _time = parse(data["data"]["liveMeasurement"]["timestamp"]).astimezone(
                self._tibber_control.time_zone
            )
            self._rt_power.append(
                (_time, data["data"]["liveMeasurement"]["power"] / 1000)
            )
            while self._rt_power and self._rt_power[0][0] < _time - dt.timedelta(
                minutes=5
            ):
                self._rt_power.pop(0)
            current_hour = data["data"]["liveMeasurement"][
                "accumulatedConsumptionLastHour"
            ]
            if current_hour is not None:
                power = sum(p[1] for p in self._rt_power) / len(self._rt_power)
                data["data"]["liveMeasurement"]["estimatedHourConsumption"] = round(
                    current_hour
                    + power * (3600 - (_time.minute * 60 + _time.second)) / 3600,
                    3,
                )
                if self.peak_hour and current_hour > self.peak_hour:
                    self.peak_hour = round(current_hour, 2)
                    self.peak_hour_time = _time
            return data

        def callback_add_extra_data(data: dict) -> None:
            """Add estimated hourly consumption."""
            try:
                data = _add_extra_data(data)
            except KeyError:
                pass
            callback(data)

        self._subscription_id = await self._tibber_control.sub_manager.subscribe(
            document, callback_add_extra_data
        )

    async def rt_unsubscribe(self) -> None:
        """Unsubscribe to Tibber rt subscription."""
        if self._subscription_id is None:
            _LOGGER.error("Not subscribed.")
            return
        await self._tibber_control.sub_manager.unsubscribe(self._subscription_id)

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        return (
            self._tibber_control.sub_manager is not None
            and self._tibber_control.sub_manager.is_running
        )

    async def get_historic_data(
        self, n_data: int, resolution: str = RESOLUTION_HOURLY
    ) -> list[dict]:
        """Get historic data."""
        # pylint: disable=consider-using-f-string)
        query = """
                {{
                  viewer {{
                    home(id: "{}") {{
                      consumption(resolution: {}, last: {}) {{
                        nodes {{
                          from
                          totalCost
                          cost
                          consumption
                        }}
                      }}
                    }}
                  }}
                }}
          """.format(
            self.home_id,
            resolution,
            n_data,
        )

        if not (data := await self._tibber_control.execute(query)):
            _LOGGER.error("Could not find the data.")
            return
        data = data["viewer"]["home"]["consumption"]
        if data is None:
            self._data = []
            return
        self._data = data["nodes"]
        return self._data

    def current_price_data(self) -> tuple[float, float, dt.datetime]:
        """get current price."""
        now = dt.datetime.now(self._tibber_control.time_zone)
        res = None, None, None
        for key, price_total in self.price_total.items():
            price_time = parse(key).astimezone(self._tibber_control.time_zone)
            time_diff = (now - price_time).total_seconds() / 60
            if not self.last_data_timestamp or price_time > self.last_data_timestamp:
                self.last_data_timestamp = price_time
            if 0 <= time_diff < 60:
                res = round(price_total, 3), self.price_level[key], price_time
        return res

    def current_attributes(self) -> dict:
        """get current attributes."""
        # pylint: disable=too-many-locals
        max_price = 0
        min_price = 10000
        sum_price = 0
        off_peak_1 = 0
        peak = 0
        off_peak_2 = 0
        num1 = 0
        num0 = 0
        num2 = 0
        num = 0
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
        grid_company = self.info["viewer"]["home"]["meteringPointData"].get(
            "gridCompany", ""
        )
        if grid_company and "glitre" in grid_company.lower():
            now = now.astimezone(pytz.timezone("Europe/Oslo"))
            if now.month >= 10 or now.month <= 3:
                grid_price = 47.39 / 100
            elif now.month == 4:
                grid_price = 41.51 / 100
            else:
                grid_price = 47.39 / 100
            if now.hour >= 22 or now.hour < 6:
                grid_price -= 12 / 100
            attr["grid_price"] = round(grid_price, 3)

        return attr


class InvalidLogin(Exception):
    """Invalid login exception."""
