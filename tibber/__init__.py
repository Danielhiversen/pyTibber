"""Library to handle connection with Tibber API."""
import asyncio
import datetime as dt
import logging
import zoneinfo
from typing import Optional

import aiohttp
import async_timeout
from graphql_subscription_manager import SubscriptionManager

from .const import __version__
from .gql_queries import INFO, PUSH_NOTIFICATION
from .tibber_home import TibberHome

DEFAULT_TIMEOUT = 10
DEMO_TOKEN = "5K4MVS-OjfWhK_4yrjOlFe1F6kJXPVf7eQYggo8ebAE"
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
        websession: Optional[aiohttp.ClientSession] = None,
        time_zone: Optional[dt.tzinfo] = None,
        user_agent: str = None,
        api_endpoint: str = API_ENDPOINT, # Allow overriding API endpoint for easy testing
    ):
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param websession: The websession to use when communicating with the Tibber API.
        :param time_zone: The time zone to display times in and to use.
        :param user_agent: User agent identifier for the platform running this
        """
        if user_agent is None:
            raise Exception("Please provide value for HTTP user agent for allowing better tracking of Tibber API consumers. Example: MyHomeAutomationServer/1.2.3")

        user_agent = f"{user_agent} pyTibber/{__version__}"
        if websession is None:
            self.websession = aiohttp.ClientSession(
                headers={aiohttp.hdrs.USER_AGENT: user_agent}
            )
        else:
            self.websession = websession
        self._timeout: int = timeout
        self._access_token: str = access_token
        self.time_zone: dt.tzinfo = time_zone or zoneinfo.ZoneInfo("UTC")
        self._name: str = ""
        self._user_id: Optional[str] = None
        self._active_home_ids: list[str] = []
        self._all_home_ids: list[str] = []
        self._homes: dict[str, TibberHome] = {}
        self.sub_manager: Optional[SubscriptionManager] = None
        self.api_endpoint = api_endpoint
        try:
            user_agent = self.websession._default_headers.get(
                aiohttp.hdrs.USER_AGENT, ""
            )  # will be fixed by aiohttp 4.0
        except Exception:  # pylint: disable=broad-except
            user_agent = ""
        self.user_agent = f"{user_agent} pyTibber/{__version__}"

    async def close_connection(self) -> None:
        """Close the Tibber connection.
        This method simply closes the websession used by the object."""
        await self.websession.close()

    async def rt_connect(self) -> None:
        """Start the GraphQL subscription manager for real time data.
        This method instantiates the graphql_subscription_manager.SubscriptionManager
        class which authenticates the user with the provided access token, and then
        starts the SubscriptionManager.
        """
        if self.sub_manager is not None:
            return
        self.sub_manager = SubscriptionManager(
            {"token": self._access_token},
            SUB_ENDPOINT,
            self.user_agent,
        )
        self.sub_manager.start()

    async def rt_disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        if self.sub_manager is None:
            return
        await self.sub_manager.stop()

    async def execute(
        self, document: str, variable_values: Optional[dict] = None
    ) -> Optional[dict]:
        """Execute a GraphQL query and return the data.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        """
        if (res := await self._execute(document, variable_values)) is None:
            return None
        return res.get("data")

    async def _execute(
        self, document: str, variable_values: dict = None, retry: int = 2
    ) -> Optional[dict]:
        """Execute a GraphQL query and return the result as a dict loaded from the json response.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        """
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
                resp = await self.websession.post(self.api_endpoint, **post_args)
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

    async def update_info(self, *_) -> None:
        """Updates home info asynchronously."""
        if (res := await self._execute(INFO)) is None:
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

        self._active_home_ids = []
        for _home in viewer.get("homes", []):
            if not (home_id := _home.get("id")):
                continue
            self._all_home_ids += [home_id]
            if not (subs := _home.get("subscriptions")):
                continue
            if subs[0].get("status", "ended").lower() == "running":
                self._active_home_ids += [home_id]

    def get_home_ids(self, only_active=True) -> list[str]:
        """Return list of home ids."""
        if only_active:
            return self._active_home_ids
        return self._all_home_ids

    def get_homes(self, only_active: bool = True) -> list["TibberHome"]:
        """Return list of Tibber homes."""
        return [
            home
            for home_id in self.get_home_ids(only_active)
            if (home := self.get_home(home_id))
        ]

    def get_home(self, home_id: str) -> Optional["TibberHome"]:
        """Return an instance of TibberHome for given home id."""
        if home_id not in self._all_home_ids:
            _LOGGER.error("Could not find any Tibber home with id: %s", home_id)
            return None
        if home_id not in self._homes:
            self._homes[home_id] = TibberHome(home_id, self)
        return self._homes[home_id]

    async def send_notification(self, title: str, message: str) -> bool:
        """Sends a push notification to the Tibber app on registered devices.

        :param title: The title of the push notification.
        :param message: The message of the push notification.
        """
        if not (
            res := await self.execute(
                PUSH_NOTIFICATION.format(
                    title,
                    message,
                )
            )
        ):
            return False
        notification = res.get("sendPushNotification", {})
        successful = notification.get("successful", False)
        pushed_to_number_of_devices = notification.get("pushedToNumberOfDevices", 0)
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

    async def fetch_production_data_active_homes(self) -> None:
        """Fetch production data for active homes."""
        tasks = []
        for home in self.get_homes(only_active=True):
            if home.has_production:
                tasks.append(home.fetch_production_data())
        await asyncio.gather(*tasks)

    @property
    def user_id(self) -> Optional[str]:
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


class InvalidLogin(Exception):
    """Invalid login exception."""
