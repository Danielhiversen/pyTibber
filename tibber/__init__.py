"""Library to handle connection with Tibber API."""
import asyncio
import datetime as dt
import logging
import random
import zoneinfo

import async_timeout
from aiohttp import ClientError, ClientSession, hdrs
from gql import Client

from .const import API_ENDPOINT, DEMO_TOKEN, __version__
from .exceptions import FatalHttpException, InvalidLogin, RetryableHttpException
from .gql_queries import INFO, PUSH_NOTIFICATION
from .tibber_home import TibberHome
from .tibber_response_handler import extract_response_data
from .websocker_transport import TibberWebsocketsTransport

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
        websession: ClientSession | None = None,
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
            websession = ClientSession()
        elif user_agent is None:
            user_agent = websession.headers.get(hdrs.USER_AGENT)
        if user_agent is None:
            # pylint: disable=broad-exception-raised
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
        self.sub_manager: Client | None = None
        self.api_endpoint: str = api_endpoint
        self.sub_endpoint: str | None = None
        self._watchdog_runner: None | asyncio.Task = None
        self._watchdog_running: bool = False

    async def close_connection(self) -> None:
        """Close the Tibber connection.
        This method simply closes the websession used by the object."""
        await self.websession.close()

    async def rt_disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        if self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        if self.sub_manager is None or not hasattr(self.sub_manager, "session"):
            return
        try:
            await self.sub_manager.close_async()
        finally:
            self.sub_manager = None

    async def rt_connect(self) -> None:
        """Start subscription manager."""
        if self.sub_endpoint is None:
            # pylint: disable=broad-exception-raised
            raise Exception("Subscription endpoint not initialized")

        if self.sub_manager is None:
            self.sub_manager = Client(
                transport=TibberWebsocketsTransport(
                    self.sub_endpoint,
                    self._access_token,
                    self.user_agent,
                ),
            )

        async with LOCK_RT_CONNECT:
            if self.rt_subscription_running:
                return
            if self._watchdog_runner is None:
                _LOGGER.debug("Starting watchdog")
                self._watchdog_running = True
                self._watchdog_runner = asyncio.create_task(self._rt_watchdog())
            await self.sub_manager.connect_async()

    async def _rt_watchdog(self) -> None:
        """Watchdog to keep connection alive."""
        await asyncio.sleep(60)

        next_is_running_test = dt.datetime.now()
        _retry_count = 0
        while self._watchdog_running:
            if self.rt_subscription_running:
                is_running = True
                if next_is_running_test > dt.datetime.now():
                    for home in self.get_homes(False):
                        if not home.has_real_time_consumption:
                            continue
                        if not home.rt_subscription_running:
                            is_running = False
                            next_is_running_test = dt.datetime.now() + dt.timedelta(
                                seconds=60
                            )
                            break
                if is_running:
                    _LOGGER.debug("Watchdog: Connection is alive")
                    await asyncio.sleep(5)
                    continue

            _LOGGER.error("Watchdog: Connection is down")

            try:
                if self.sub_manager is not None and hasattr(
                    self.sub_manager, "session"
                ):
                    await self.sub_manager.close_async()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Error in watchdog close")
            self.sub_manager = None

            delay_seconds = min(
                random.SystemRandom().randint(1, 60) + _retry_count**2,
                60 * 60,
            )
            _retry_count += 1
            await asyncio.sleep(delay_seconds)

            try:
                await self.rt_connect()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Error in watchdog connect, will retry. Retry count: %s",
                    _retry_count,
                    exc_info=_retry_count > 1,
                )
            else:
                _LOGGER.info("Watchdog: Reconnected successfully")
                await asyncio.sleep(60)

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
                hdrs.USER_AGENT: self.user_agent,
            },
            "data": payload,
        }
        try:
            async with async_timeout.timeout(timeout):
                resp = await self.websession.post(self.api_endpoint, **post_args)

            return await extract_response_data(resp)

        except ClientError as err:
            if retry > 0:
                return await self._execute(
                    document,
                    variable_values,
                    retry - 1,
                    timeout,
                )
            _LOGGER.error("Error connecting to Tibber: %s ", err, exc_info=True)
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
        if (res := await self._execute(INFO)) is None:
            return

        if not (data := res.get("data")):
            return

        if not (viewer := data.get("viewer")):
            return

        if not (sub_endpoint := viewer.get("websocketSubscriptionUrl")):
            return

        _LOGGER.debug("Using websocket subscription url %s", sub_endpoint)
        self.sub_endpoint = sub_endpoint
        if self.sub_manager is not None and isinstance(
            self.sub_manager.transport, TibberWebsocketsTransport
        ):
            self.sub_manager.transport.url = sub_endpoint

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

    @property
    def rt_subscription_running(self) -> bool:
        """Is real time subscription running."""
        return (
            self.sub_manager is not None
            and isinstance(self.sub_manager.transport, TibberWebsocketsTransport)
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
