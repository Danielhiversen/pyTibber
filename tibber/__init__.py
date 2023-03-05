"""Library to handle connection with Tibber API."""
import asyncio
import datetime as dt
import logging
import zoneinfo

import aiohttp
import async_timeout

from .const import API_ENDPOINT, DEFAULT_TIMEOUT, DEMO_TOKEN, __version__
from .exceptions import (
    FatalHttpException,
    InvalidLogin,
    RetryableHttpException,
    UserAgentMissing,
)
from .gql_queries import INFO, PUSH_NOTIFICATION
from .tibber_home import TibberHome
from .tibber_response_handler import extract_response_data
from .tibber_rt import TibberRT

_LOGGER = logging.getLogger(__name__)


class Tibber:
    """Class to communicate with the Tibber api."""

    # pylint: disable=too-many-instance-attributes, too-many-arguments

    def __init__(
        self,
        access_token: str = DEMO_TOKEN,
        timeout: int = DEFAULT_TIMEOUT,
        websession: aiohttp.ClientSession | None = None,
        time_zone: dt.tzinfo | None = None,
        user_agent: str | None = None,
    ):
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param websession: The websession to use when communicating with the Tibber API.
        :param time_zone: The time zone to display times in and to use.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """

        if websession is None:
            websession = aiohttp.ClientSession()
        elif user_agent is None:
            user_agent = websession.headers.get(aiohttp.hdrs.USER_AGENT)
        if user_agent is None:
            raise UserAgentMissing("Please provide value for HTTP user agent")
        self._user_agent: str = f"{user_agent} pyTibber/{__version__}"
        self.websession = websession
        self._timeout: int = timeout
        self._access_token: str = access_token

        self.realtime: TibberRT = TibberRT(
            self._access_token,
            self._timeout,
            self._user_agent,
        )

        self.time_zone: dt.tzinfo = time_zone or zoneinfo.ZoneInfo("UTC")
        self._name: str = ""
        self._user_id: str | None = None
        self._active_home_ids: list[str] = []
        self._all_home_ids: list[str] = []
        self._homes: dict[str, TibberHome] = {}

    async def close_connection(self) -> None:
        """Close the Tibber connection.
        This method simply closes the websession used by the object."""
        await self.websession.close()

    async def execute(
        self,
        document: str,
        variable_values: dict | None = None,
        timeout: int | None = None,
        retry: int = 3,
    ) -> dict | None:
        """Execute a GraphQL query and return the data.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        :param timeout: The timeout to use for the request.
        :param retry: The number of times to retry the request.
        """
        timeout = timeout or self._timeout

        payload = {"query": document, "variables": variable_values or {}}

        post_args = {
            "headers": {
                "Authorization": "Bearer " + self._access_token,
                aiohttp.hdrs.USER_AGENT: self._user_agent,
            },
            "data": payload,
        }
        try:
            async with async_timeout.timeout(timeout):
                resp = await self.websession.post(API_ENDPOINT, **post_args)
            return (await extract_response_data(resp)).get("data")

        except aiohttp.ClientError as err:
            if retry > 0:
                return await self.execute(
                    document,
                    variable_values,
                    retry - 1,
                    timeout,
                )
            _LOGGER.error("Error connecting to Tibber: %s ", err, exc_info=True)
            raise
        except asyncio.TimeoutError:
            if retry > 0:
                return await self.execute(
                    document,
                    variable_values,
                    retry - 1,
                    timeout,
                )
            _LOGGER.error("Timed out when connecting to Tibber")
            raise
        except (InvalidLogin, FatalHttpException) as err:
            _LOGGER.error(
                "Fatal error interacting with Tibber API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise
        except RetryableHttpException as err:
            _LOGGER.warning(
                "Temporary failure interacting with Tibber API, HTTP status: %s. API error: %s / %s",
                err.status,
                err.extension_code,
                err.message,
            )
            raise

    async def update_info(self) -> None:
        """Updates home info asynchronously."""
        if (data := await self.execute(INFO)) is None:
            return

        if not (viewer := data.get("viewer")):
            return

        if sub_endpoint := viewer.get("websocketSubscriptionUrl"):
            _LOGGER.debug("Using websocket subscription url %s", sub_endpoint)
            self.realtime.sub_endpoint = sub_endpoint

        self._name = viewer.get("name")
        self._user_id = viewer.get("userId")

        self._active_home_ids = []
        for _home in viewer.get("homes", []):
            if not (home_id := _home.get("id")):
                continue
            self._all_home_ids += [home_id]
            if not (subs := _home.get("subscriptions")):
                continue
            if (
                subs[0].get("status") is not None
                and subs[0]["status"].lower() == "running"
            ):
                self._active_home_ids += [home_id]

    def get_home_ids(self, only_active: bool = True) -> list[str]:
        """Return list of home ids."""
        if only_active:
            return self._active_home_ids
        return self._all_home_ids

    def get_homes(self, only_active: bool = True) -> list[TibberHome]:
        """Return list of Tibber homes."""
        return [
            home
            for home_id in self.get_home_ids(only_active)
            if (home := self.get_home(home_id))
        ]

    def get_home(self, home_id: str) -> TibberHome | None:
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

    async def rt_disconnect(self) -> None:  # Todo: remove?
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        return await self.realtime.disconnect()

    @property
    def user_id(self) -> str | None:
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
