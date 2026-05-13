"""Tibber RT connection."""

import asyncio
import datetime as dt
import logging
import random
from collections.abc import Awaitable, Callable
from ssl import SSLContext
from typing import Any

from gql import Client

from .exceptions import SubscriptionEndpointMissingError
from .home import TibberHome
from .websocket_transport import TibberWebsocketsTransport

LOCK_CONNECT = asyncio.Lock()

_LOGGER = logging.getLogger(__name__)


class TibberRT:
    """Class to handle real time connection with the Tibber api."""

    def __init__(
        self,
        access_token: str,
        timeout: int,
        user_agent: str,
        ssl: SSLContext | bool,
        refresh_access_token: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        :param refresh_access_token: Async callback to refresh the access token before reconnecting.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent
        self._ssl_context = ssl
        self._refresh_access_token = refresh_access_token
        self._reconnect_lock = asyncio.Lock()

        self._sub_endpoint: str | None = None
        self._homes: list[TibberHome] = []
        self._watchdog_runner: None | asyncio.Task[Any] = None
        self._watchdog_running: bool = False

        self.sub_manager: Client | None = None
        self.session: Any | None = None

    async def disconnect(self) -> None:
        """Stop subscription manager.
        This method simply calls the stop method of the SubscriptionManager if it is defined.
        """
        _LOGGER.debug("Stopping subscription manager")
        await self._reset_connection(unsubscribe_homes=True)

    async def _reset_connection(self, unsubscribe_homes: bool = False, stop_watchdog: bool = True) -> None:
        """Reset websocket connection state."""
        async with LOCK_CONNECT:
            await self._reset_connection_locked(
                unsubscribe_homes=unsubscribe_homes,
                stop_watchdog=stop_watchdog,
            )

    async def _reset_connection_locked(self, unsubscribe_homes: bool = False, stop_watchdog: bool = True) -> None:
        """Reset websocket connection state while LOCK_CONNECT is held."""
        if stop_watchdog and self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        if unsubscribe_homes:
            for home in self._homes:
                home.rt_unsubscribe()
        try:
            if self.sub_manager is None:
                return

            if not self._sub_manager_has_session():
                _LOGGER.debug(
                    "Skipping subscription manager close because the gql client has no session",
                )
                return

            await self.sub_manager.close_async()
        finally:
            self.session = None
            self.sub_manager = None

    async def connect(self) -> None:
        """Start subscription manager."""
        async with LOCK_CONNECT:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        """Start subscription manager while LOCK_CONNECT is held."""
        if self.subscription_running:
            return

        if self.sub_manager is None:
            self.sub_manager = self._build_sub_manager()

        if self._watchdog_runner is None:
            _LOGGER.debug("Starting watchdog")
            self._watchdog_running = True
            self._watchdog_runner = asyncio.create_task(self._watchdog())
        self.session = await self.sub_manager.connect_async()

    async def reconnect(self) -> None:
        """Reconnect and resubscribe all homes."""
        async with self._reconnect_lock:
            access_token = await self._refresh_access_token() if self._refresh_access_token is not None else None
            if access_token and access_token != self._access_token:
                self._access_token = access_token
                await self._reset_connection(
                    unsubscribe_homes=self.subscription_running,
                    stop_watchdog=False,
                )
            elif access_token:
                self._access_token = access_token
            await self.connect()
            await self._resubscribe_homes()

    async def set_access_token(self, access_token: str) -> None:
        """Set access token and reconnect active realtime subscriptions."""
        async with LOCK_CONNECT:
            restore_connection = self.should_restore_connection
            self._access_token = access_token
            await self._reset_connection_locked(
                unsubscribe_homes=restore_connection,
                stop_watchdog=True,
            )

            if restore_connection:
                await self._connect_locked()

        if restore_connection:
            await self._resubscribe_homes()

    def _build_sub_manager(self) -> Client:
        """Create a subscription manager for the current websocket endpoint."""
        if self.sub_endpoint is None:
            raise SubscriptionEndpointMissingError("Subscription endpoint not initialized")

        return Client(
            transport=TibberWebsocketsTransport(
                self.sub_endpoint,
                self._access_token,
                self._user_agent,
                ssl=self._ssl_context,
            ),
        )

    def _sub_manager_has_session(self) -> bool:
        """Return True if the current gql client owns a session."""
        return self.sub_manager is not None and hasattr(self.sub_manager, "session")

    def _current_transport(self) -> TibberWebsocketsTransport | None:
        if self.sub_manager is None:
            return None
        if not isinstance(self.sub_manager.transport, TibberWebsocketsTransport):
            return None
        return self.sub_manager.transport

    async def _watchdog(self) -> None:
        """Watchdog to keep connection alive."""
        if self._current_transport() is None:
            _LOGGER.debug("Watchdog: Starting without a current transport")

        await asyncio.sleep(60)

        _retry_count = 0
        next_test_all_homes_running = dt.datetime.now(tz=dt.UTC)
        while self._watchdog_running:
            await asyncio.sleep(5)
            transport = self._current_transport()
            if (
                transport is not None
                and transport.running
                and transport.reconnect_at
                > dt.datetime.now(
                    tz=dt.UTC,
                )
                and dt.datetime.now(tz=dt.UTC) > next_test_all_homes_running
            ):
                is_running = True
                for home in self._homes:
                    _LOGGER.debug(
                        "Watchdog: Checking if home %s is alive, %s, %s",
                        home.home_id,
                        home.has_real_time_consumption,
                        home.rt_subscription_running,
                    )
                    if not home.rt_subscription_running:
                        is_running = False
                        next_test_all_homes_running = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=60)
                        break
                    _LOGGER.debug(
                        "Watchdog: Home %s is alive",
                        home.home_id,
                    )
                if is_running:
                    _retry_count = 0
                    _LOGGER.debug("Watchdog: Connection is alive")
                    continue

            reconnect_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self._timeout)
            if transport is not None:
                transport.reconnect_at = reconnect_at
                reconnect_at = transport.reconnect_at
            _LOGGER.error(
                "Watchdog: Connection is down, %s",
                reconnect_at,
            )

            try:
                await self._reset_connection(stop_watchdog=False)
            except Exception:
                _LOGGER.exception("Error in watchdog close")

            if not self._watchdog_running:
                _LOGGER.debug("Watchdog: Stopping")
                return

            try:
                await self.reconnect()
            except Exception as err:
                delay_seconds = min(
                    random.SystemRandom().randint(1, 30) + _retry_count**2,
                    5 * 60,
                )
                _retry_count += 1
                _LOGGER.exception(
                    "Error in watchdog connect, retrying in %s seconds, %s: %s",
                    delay_seconds,
                    _retry_count,
                    err,
                )
                await asyncio.sleep(delay_seconds)
            else:
                _LOGGER.debug("Watchdog: Reconnected successfully")
                await asyncio.sleep(60)

    async def _resubscribe_homes(self) -> None:
        """Resubscribe to all homes."""
        _LOGGER.debug("Resubscribing to homes")
        await asyncio.gather(*[home.rt_resubscribe() for home in self._homes])

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
            and self.session is not None
        )

    @property
    def should_restore_connection(self) -> bool:
        """Whether realtime subscriptions should be restored after a reset."""
        return self.subscription_running or self._watchdog_runner is not None

    @property
    def sub_endpoint(self) -> str | None:
        """Get subscription endpoint."""
        return self._sub_endpoint

    @sub_endpoint.setter
    def sub_endpoint(self, sub_endpoint: str) -> None:
        """Set subscription endpoint."""
        self._sub_endpoint = sub_endpoint
        if self.sub_manager is not None and isinstance(self.sub_manager.transport, TibberWebsocketsTransport):
            if self.session is not None or self._sub_manager_has_session():
                _LOGGER.debug(
                    "Delaying websocket subscription url update until the next reconnect",
                )
                return

            self.sub_manager = self._build_sub_manager()
