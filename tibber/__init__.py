"""Library to handle connection with Tibber API."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random
import zoneinfo

import aiohttp
import async_timeout
from gql import Client
from gql.transport.websockets import WebsocketsTransport

from .const import API_ENDPOINT, DEMO_TOKEN, __version__
from .gql_queries import INFO, PUSH_NOTIFICATION
from .tibber_home import TibberHome

DEFAULT_TIMEOUT = 10

_LOGGER = logging.getLogger(__name__)
LOCK_RT_CONNECT = asyncio.Lock()


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
        api_endpoint: str = API_ENDPOINT,
    ):
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param websession: The websession to use when communicating with the Tibber API.
        :param time_zone: The time zone to display times in and to use.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        :param api_endpoint: Allow overriding API endpoint for easy testing
        """

        if websession is None:
            websession = aiohttp.ClientSession()
        elif user_agent is None:
            user_agent = websession.headers.get(aiohttp.hdrs.USER_AGENT)
        if user_agent is None:
            raise Exception(
                "Please provide value for HTTP user agent. Example: MyHomeAutomationServer/1.2.3"
            )
        self.user_agent = f"{user_agent} pyTibber/{__version__}"
        self.websession = websession

        self._timeout: int = timeout
        self._access_token: str = access_token
        self.time_zone: dt.tzinfo = time_zone or zoneinfo.ZoneInfo("UTC")
        self._name: str = ""
        self._user_id: str | None = None
        self._active_home_ids: list[str] = []
        self._all_home_ids: list[str] = []
        self._homes: dict[str, TibberHome] = {}
        self.sub_manager = None
        self.api_endpoint = api_endpoint
        self.sub_endpoint = None

    async def close_connection(self) -> None:
        """Close the Tibber connection.
        This method simply closes the websession used by the object."""
        await self.websession.close()

    async def rt_disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        if not hasattr(self.sub_manager, "session"):
            return
        await self.sub_manager.close_async()

    async def rt_connect(self) -> None:
        """Start subscription manager."""
        if self.sub_manager is None:
            self.sub_manager: Client = Client(
                transport=TibberWebsocketsTransport(
                    self.sub_endpoint,
                    self._access_token,
                    self.user_agent,
                ),
            )

        async with LOCK_RT_CONNECT:
            if self.rt_subscription_running:
                return
            try:
                await self.sub_manager.connect_async()
            except Exception as e:  # pylint: disable=broad-except
                _LOGGER.error("Failed to connect to Tibber RT %s", str(e))
                await self.rt_disconnect()
                raise

    async def execute(
        self,
        document: str,
        variable_values: dict | None = None,
        timeout: int | None = None,
    ) -> dict | None:
        """Execute a GraphQL query and return the data.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        :param timeout: The timeout to use for the request.
        """
        if (
            res := await self._execute(document, variable_values, timeout=timeout)
        ) is None:
            return None
        return res.get("data")

    async def _execute(
        self,
        document: str,
        variable_values: dict | None = None,
        retry: int = 2,
        timeout: int | None = None,
    ) -> dict | None:
        """Execute a GraphQL query and return the result as a dict loaded from the json response.

        :param document: The GraphQL query to request.
        :param variable_values: The GraphQL variables to parse with the request.
        """
        timeout = timeout or self._timeout

        payload = {"query": document, "variables": variable_values or {}}

        post_args = {
            "headers": {
                "Authorization": "Bearer " + self._access_token,
                aiohttp.hdrs.USER_AGENT: self.user_agent,
            },
            "data": payload,
        }
        try:
            async with async_timeout.timeout(timeout):
                resp = await self.websession.post(self.api_endpoint, **post_args)
            if resp.status != 200:
                _LOGGER.error("Error connecting to Tibber, resp code: %s", resp.status)
                return None
            result = await resp.json()
        except aiohttp.ClientError:
            if retry > 0:
                return await self._execute(
                    document,
                    variable_values,
                    retry - 1,
                    timeout,
                )
            _LOGGER.error("Error connecting to Tibber", exc_info=True)
            raise
        except asyncio.TimeoutError:
            if retry > 0:
                return await self._execute(
                    document,
                    variable_values,
                    retry - 1,
                    timeout,
                )
            _LOGGER.error("Timed out when connecting to Tibber")
            raise
        if errors := result.get("errors"):
            _LOGGER.error("Received non-compatible response %s", errors)
        return result

    async def update_info(self) -> None:
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

        if not (sub_endpoint := viewer.get("websocketSubscriptionUrl")):
            return

        _LOGGER.debug("Using websocket subscription url %s", sub_endpoint)
        self.sub_endpoint = sub_endpoint

        self._name = viewer.get("name")
        self._user_id = viewer.get("userId")

        self._active_home_ids = []
        for _home in viewer.get("homes", []):
            if not (home_id := _home.get("id")):
                continue
            self._all_home_ids += [home_id]
            if not (subs := _home.get("subscriptions")):
                continue
            if subs[0].get("status", "ended") is not None and subs[0].get("status", "ended").lower() == "running":
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

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        return (
            isinstance(self.sub_manager.transport, TibberWebsocketsTransport)
            and self.sub_manager.transport.running
        )

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


class InvalidLogin(Exception):
    """Invalid login exception."""


class TibberWebsocketsTransport(WebsocketsTransport):
    """Tibber websockets transport."""

    def __init__(self, url: str, access_token: str, user_agent: str) -> None:
        """Initialize TibberWebsocketsTransport."""
        super().__init__(
            url=url,
            init_payload={"token": access_token},
            headers={"User-Agent": user_agent},
            ping_interval=10,
        )
        self._watchdog_runner: None | asyncio.Task = None
        self._watchdog_running: bool = False

    @property
    def running(self) -> bool:
        """Is real time subscription running."""
        return self.websocket is not None and self.websocket.open

    async def connect(self) -> None:
        """Connect to websockets."""
        if self._watchdog_runner is None:
            _LOGGER.debug("Starting watchdog")
            self._watchdog_running = True
            self._watchdog_runner = asyncio.create_task(self._watchdog())
        return await super().connect()

    async def close(self) -> None:
        """Close websockets."""
        if self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        return await super().close()

    async def _receive(self) -> str:
        """Wait the next message from the websocket connection."""
        timeout = 90
        try:
            return await asyncio.wait_for(super()._receive(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.error("No data received from Tibber for %s seconds", timeout)
            raise

    async def _watchdog(self) -> None:
        """Watchdog to keep connection alive."""
        await asyncio.sleep(60)

        _retry_count = 0
        while self._watchdog_running:
            if self.receive_data_task in asyncio.all_tasks() and self.running:
                _retry_count = 0
                _LOGGER.debug("Watchdog: Connection is alive")
                await asyncio.sleep(5)
                continue

            _LOGGER.error("Watchdog: Connection is down")

            try:
                await super().close()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Error in watchdog close")

            if not self._watchdog_running:
                return

            try:
                await super().connect()
            except Exception:  # pylint: disable=broad-except
                delay_seconds = min(
                    random.SystemRandom().randint(1, 60) + _retry_count**2,
                    20 * 60,
                )
                _retry_count += 1
                _LOGGER.error(
                    "Error in watchdog connect, retrying in %s seconds, %s",
                    delay_seconds,
                    _retry_count,
                    exc_info=_retry_count > 1,
                )
                await asyncio.sleep(delay_seconds)
            else:
                _LOGGER.debug("Watchdog: Reconnected successfully")
