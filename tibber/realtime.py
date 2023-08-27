"""Tibber RT connection."""
import asyncio
import datetime as dt
import logging
import random
from typing import Any

from gql import Client

from .exceptions import SubscriptionEndpointMissing
from .home import TibberHome
from .websocker_transport import TibberWebsocketsTransport

LOCK_CONNECT = asyncio.Lock()

_LOGGER = logging.getLogger(__name__)


class TibberRT:
    """Class to handle real time connection with the Tibber api."""

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        access_token: str,
        timeout: int,
        user_agent: str,
    ):
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent

        self._sub_endpoint: str | None = None
        self._homes: list[TibberHome] = []
        self._watchdog_runner: None | asyncio.Task[Any] = None
        self._watchdog_running: bool = False

        self.sub_manager: Client | None = None

    async def disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        _LOGGER.debug("Stopping subscription manager")
        if self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        for home in self._homes:
            home.rt_unsubscribe()
        if self.sub_manager is None:
            return
        try:
            if not hasattr(self.sub_manager, "session"):
                return
            await self.sub_manager.close_async()  # type: ignore
        finally:
            self.sub_manager = None

    async def connect(self) -> None:
        """Start subscription manager."""
        self._create_sub_manager()

        assert self.sub_manager is not None

        async with LOCK_CONNECT:
            if self.subscription_running:
                return
            if self._watchdog_runner is None:
                _LOGGER.debug("Starting watchdog")
                self._watchdog_running = True
                self._watchdog_runner = asyncio.create_task(self._watchdog())
            await self.sub_manager.connect_async()  # type: ignore

    def _create_sub_manager(self) -> None:
        if self.sub_endpoint is None:
            raise SubscriptionEndpointMissing("Subscription endpoint not initialized")
        if self.sub_manager is not None:
            return
        self.sub_manager = Client(
            transport=TibberWebsocketsTransport(
                self.sub_endpoint,
                self._access_token,
                self._user_agent,
            ),
        )

    async def _watchdog(self) -> None:
        """Watchdog to keep connection alive."""
        assert self.sub_manager is not None
        assert isinstance(self.sub_manager.transport, TibberWebsocketsTransport)

        await asyncio.sleep(60)

        _retry_count = 0
        next_test_all_homes_running = dt.datetime.now()
        while self._watchdog_running:
            await asyncio.sleep(5)
            if (
                self.sub_manager.transport.running
                and self.sub_manager.transport.reconnect_at > dt.datetime.now()
            ):
                if dt.datetime.now() > next_test_all_homes_running:
                    is_running = True
                    for home in self._homes:
                        _LOGGER.debug(
                            "Watchdog: Checking if home %s is alive, %s, %s",
                            home.home_id,
                            home.has_real_time_consumption,
                            home.rt_subscription_running,
                        )
                        if home.has_real_time_consumption is False:
                            continue
                        if not home.rt_subscription_running:
                            is_running = False
                            next_test_all_homes_running = (
                                dt.datetime.now() + dt.timedelta(seconds=60)
                            )
                            break
                        _LOGGER.debug(
                            "Watchdog: Home %s is alive",
                            home.home_id,
                        )
                    if is_running:
                        _retry_count = 0
                        _LOGGER.debug("Watchdog: Connection is alive")
                        continue

            self.sub_manager.transport.reconnect_at = dt.datetime.now() + dt.timedelta(
                seconds=self._timeout
            )
            _LOGGER.error(
                "Watchdog: Connection is down, %s",
                self.sub_manager.transport.reconnect_at,
            )

            try:
                if hasattr(self.sub_manager, "session"):
                    await self.sub_manager.close_async()  # type: ignore
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Error in watchdog close")

            if not self._watchdog_running:
                _LOGGER.debug("Watchdog: Stopping")
                return

            self._create_sub_manager()
            try:
                await self.sub_manager.connect_async()  # type: ignore
                await self._resubscribe_homes()
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
                await asyncio.sleep(60)

    async def _resubscribe_homes(self) -> None:
        """Resubscribe to all homes."""
        _LOGGER.debug("Resubscribing to homes")
        await asyncio.gather(
            *[
                home.rt_resubscribe()
                for home in self._homes
            ]
        )

    def add_home(self, home: TibberHome) -> bool:
        """Add home to real time subscription."""
        if home.has_real_time_consumption is False:
            return False
        if home in self._homes:
            return False
        self._homes.append(home)
        return True

    @property
    def subscription_running(self) -> bool:
        """Is real time subscription running."""
        return (
            self.sub_manager is not None
            and isinstance(self.sub_manager.transport, TibberWebsocketsTransport)
            and self.sub_manager.transport.running
        )

    @property
    def sub_endpoint(self) -> str | None:
        """Get subscription endpoint."""
        return self._sub_endpoint

    @sub_endpoint.setter
    def sub_endpoint(self, sub_endpoint: str) -> None:
        """Set subscription endpoint."""
        self._sub_endpoint = sub_endpoint
        if self.sub_manager is not None and isinstance(
            self.sub_manager.transport, TibberWebsocketsTransport
        ):
            self.sub_manager.transport.url = sub_endpoint
