"""Library to handle connection with Tibber API."""
import asyncio
import datetime as dt
import logging

import aiohttp
import async_timeout
import pytz
from dateutil.parser import parse
from graphql_subscription_manager import SubscriptionManager

from .const import RESOLUTION_HOURLY

DEFAULT_TIMEOUT = 10
DEMO_TOKEN = "d1007ead2dc84a2b82f0de19451c5fb22112f7ae11d19bf2bedb224a003ff74a"
API_ENDPOINT = "https://api.tibber.com/v1-beta/gql"
SUB_ENDPOINT = "wss://api.tibber.com/v1-beta/gql/subscriptions"

_LOGGER = logging.getLogger(__name__)


class Tibber:
    """Class to communicate with the Tibber api."""

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        access_token=DEMO_TOKEN,
        timeout=DEFAULT_TIMEOUT,
        websession=None,
        time_zone=None,
    ):
        """Initialize the Tibber connection."""
        if websession is None:

            async def _create_session():
                return aiohttp.ClientSession()

            loop = asyncio.get_event_loop()
            self.websession = loop.run_until_complete(_create_session())
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

    async def close_connection(self):
        """Close the Tibber connection."""
        await self.websession.close()

    def sync_close_connection(self):
        """Close the Tibber connection."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.close_connection())
        loop.run_until_complete(task)

    async def rt_connect(self, loop):
        """Start subscription manager for real time data."""
        if self.sub_manager is not None:
            return
        self.sub_manager = SubscriptionManager(
            loop, "token={}".format(self._access_token), SUB_ENDPOINT
        )
        self.sub_manager.start()

    async def rt_disconnect(self):
        """Stop subscription manager."""
        if self.sub_manager is None:
            return
        await self.sub_manager.stop()

    async def execute(self, document, variable_values=None):
        """Execute gql."""
        res = await self._execute(document, variable_values)
        if res is None:
            return None
        return res.get("data")

    async def _execute(self, document, variable_values=None, retry=2):
        """Execute gql."""
        payload = {"query": document, "variables": variable_values or {}}

        post_args = {
            "headers": {"Authorization": "Bearer " + self._access_token},
            "data": payload,
        }

        try:
            with async_timeout.timeout(self._timeout):
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
            if retry > 0:
                return await self._execute(document, variable_values, retry - 1)
            _LOGGER.error("Timed out when connecting to Tibber")
            raise
        errors = result.get("errors")
        if errors:
            _LOGGER.error("Received non-compatible response %s", errors)
        return result

    def sync_update_info(self, *_):
        """Update home info."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_info())
        loop.run_until_complete(task)

    async def update_info(self, *_):
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

        res = await self._execute(query)
        if res is None:
            return
        errors = res.get("errors", [])
        if errors:
            msg = errors[0].get("message", "failed to login")
            _LOGGER.error(msg)
            raise InvalidLogin(msg)

        data = res.get("data")
        if not data:
            return
        viewer = data.get("viewer")
        if not viewer:
            return
        self._name = viewer.get("name")
        self._user_id = viewer.get("userId")

        homes = viewer.get("homes", [])
        self._home_ids = []
        for _home in homes:
            home_id = _home.get("id")
            self._all_home_ids += [home_id]
            subs = _home.get("subscriptions")
            if subs:
                status = subs[0].get("status", "ended").lower()
                if not home_id or status != "running":
                    continue
            self._home_ids += [home_id]

    @property
    def user_id(self):
        """Return user id of user."""
        return self._user_id

    @property
    def name(self):
        """Return name of user."""
        return self._name

    @property
    def home_ids(self):
        """Return list of home ids."""
        return self.get_home_ids(only_active=True)

    def get_home_ids(self, only_active=True):
        """Return list of home ids."""
        if only_active:
            return self._home_ids
        return self._all_home_ids

    def get_homes(self, only_active=True):
        """Return list of Tibber homes."""
        return [self.get_home(home_id) for home_id in self.get_home_ids(only_active)]

    def get_home(self, home_id):
        """Retun an instance of TibberHome for given home id."""
        if home_id not in self._all_home_ids:
            _LOGGER.error("Could not find any Tibber home with id: %s", home_id)
            return None
        if home_id not in self._homes.keys():
            self._homes[home_id] = TibberHome(home_id, self)
        return self._homes[home_id]

    async def send_notification(self, title, message):
        """Send notification."""
        query = """
        mutation{
          sendPushNotification(input: {
            title: "%s",
            message: "%s",
          }){
            successful
            pushedToNumberOfDevices
          }
        }
        """ % (
            title,
            message,
        )

        res = await self.execute(query)
        if not res:
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


class TibberHome:
    """Instance of Tibber home."""

    # pylint: disable=too-many-instance-attributes, too-many-public-methods

    def __init__(self, home_id, tibber_control):
        """Initialize the Tibber home class."""
        self._tibber_control = tibber_control
        self._home_id = home_id
        self._current_price_total = None
        self._current_price_info = {}
        self._price_info = {}
        self._level_info = {}
        self.sub_manager = None
        self.info = {}
        self._subscription_id = None
        self._data = None
        self.last_data_timestamp = None

    def sync_update_info(self):
        """Update current price info."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_info())
        loop.run_until_complete(task)

    async def update_info(self):
        """Update current price info async."""
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

    def sync_update_current_price_info(self):
        """Update current price info."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_current_price_info())
        loop.run_until_complete(task)

    async def update_current_price_info(self):
        """Update current price info async."""
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

    def sync_update_price_info(self):
        """Update current price info."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.update_price_info())
        loop.run_until_complete(task)

    async def update_price_info(self):
        """Update price info async."""
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
        price_info_temp = await self._tibber_control.execute(query)
        if not price_info_temp:
            _LOGGER.error("Could not find price info.")
            return
        self._price_info = {}
        self._level_info = {}
        for key in ["current", "today", "tomorrow"]:
            try:
                home = price_info_temp["viewer"]["home"]
                current_subscription = home["currentSubscription"]
                price_info = current_subscription["priceInfo"][key]
            except (KeyError, TypeError):
                _LOGGER.error("Could not find price info for %s.", key)
                continue
            if key == "current":
                self._current_price_info = price_info
                continue
            for data in price_info:
                self._price_info[data.get("startsAt")] = data.get("total")
                self._level_info[data.get("startsAt")] = data.get("level")

    @property
    def current_price_total(self):
        """Get current price total."""
        if not self._current_price_info:
            return None
        return self._current_price_info.get("total")

    @property
    def current_price_info(self):
        """Get current price info."""
        return self._current_price_info

    @property
    def price_total(self):
        """Get dictionary with price total, key is date-time."""
        return self._price_info

    @property
    def price_level(self):
        """Get dictionary with price level, key is date-time."""
        return self._level_info

    @property
    def home_id(self):
        """Return home id."""
        return self._home_id

    @property
    def has_active_subscription(self):
        """Return home id."""
        try:
            sub = self.info["viewer"]["home"]["currentSubscription"]["status"]
        except (KeyError, TypeError):
            return False
        return sub in ["running", "awaiting market", "awaiting time restriction"]

    @property
    def has_real_time_consumption(self):
        """Return home id."""
        try:
            return self.info["viewer"]["home"]["features"]["realTimeConsumptionEnabled"]
        except (KeyError, TypeError):
            return False

    @property
    def address1(self):
        """Return the home adress1."""
        try:
            return self.info["viewer"]["home"]["address"]["address1"]
        except (KeyError, TypeError):
            _LOGGER.error("Could not find address1.")
        return ""

    @property
    def consumption_unit(self):
        """Return the consumption."""
        return "kWh"

    @property
    def currency(self):
        """Return the currency."""
        try:
            current_subscription = self.info["viewer"]["home"]["currentSubscription"]
            return current_subscription["priceInfo"]["current"]["currency"]
        except (KeyError, TypeError, IndexError):
            _LOGGER.error("Could not find currency.")
        return ""

    @property
    def country(self):
        """Return the country."""
        try:
            return self.info["viewer"]["home"]["address"]["country"]
        except (KeyError, TypeError):
            _LOGGER.error("Could not find country.")
        return ""

    @property
    def price_unit(self):
        """Return the price unit."""
        currency = self.currency
        consumption_unit = self.consumption_unit
        if not currency or not consumption_unit:
            _LOGGER.error("Could not find price_unit.")
            return " "
        return currency + "/" + consumption_unit

    async def rt_subscribe(self, loop, async_callback):
        """Connect to Tibber and subscribe to Tibber rt subscription."""
        if self._subscription_id is not None:
            _LOGGER.error("Already subscribed.")
            return
        await self._tibber_control.rt_connect(loop)
        document = (
            """
            subscription{
              liveMeasurement(homeId:"%s"){
                timestamp
                power
                powerProduction
                accumulatedProduction
                accumulatedConsumption
                accumulatedCost
                currency
                minPower
                averagePower
                maxPower
                voltagePhase1
                voltagePhase2
                voltagePhase3
                currentL1
                currentL2
                currentL3
                lastMeterConsumption
                lastMeterProduction
                signalStrength
            }
           }
        """
            % self.home_id
        )

        self._subscription_id = await self._tibber_control.sub_manager.subscribe(
            document, async_callback
        )

    async def rt_unsubscribe(self):
        """Unsubscribe to Tibber rt subscription."""
        if self._subscription_id is None:
            _LOGGER.error("Not subscribed.")
            return
        await self._tibber_control.sub_manager.unsubscribe(self._subscription_id)

    @property
    def rt_subscription_running(self):
        """Is real time subscription running."""
        return (
            self._tibber_control.sub_manager is not None
            and self._tibber_control.sub_manager.is_running
        )

    async def get_historic_data(self, n_data, resolution=RESOLUTION_HOURLY):
        """Get historic data."""
        query = """
                {
                  viewer {
                    home(id: "%s") {
                      consumption(resolution: %s, last: %s) {
                        nodes {
                          from
                          totalCost
                          cost
                          consumption
                        }
                      }
                    }
                  }
                }
          """ % (
            self.home_id,
            resolution,
            n_data,
        )
        data = await self._tibber_control.execute(query)
        if not data:
            _LOGGER.error("Could not find current the data.")
            return
        data = data["viewer"]["home"]["consumption"]
        if data is None:
            self._data = []
            return
        self._data = data["nodes"]

    def sync_get_historic_data(self, n_data, resolution=RESOLUTION_HOURLY):
        """get historic data."""
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.get_historic_data(n_data, resolution))
        loop.run_until_complete(task)
        return self._data

    def current_price_data(self):
        """get current price."""
        now = dt.datetime.now(self._tibber_control.time_zone)
        for key, price_total in self.price_total.items():
            price_time = parse(key).astimezone(self._tibber_control.time_zone)
            price_total = round(price_total, 3)
            time_diff = (now - price_time).total_seconds() / 60
            if not self.last_data_timestamp or price_time > self.last_data_timestamp:
                self.last_data_timestamp = price_time
            if 0 <= time_diff < 60:
                return price_total, self.price_level[key], price_time
        return None, None, None

    def current_attributes(self):
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
                grid_price = 49.70 / 100
            else:
                grid_price = 47.45 / 100
            if (now.month >= 11 or now.month <= 3) and (now.hour >= 22 or now.hour < 6):
                grid_price -= 14 / 100
            attr["grid_price"] = round(grid_price, 3)

        return attr


class InvalidLogin(Exception):
    """Invalid login exception."""
