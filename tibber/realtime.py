"""Tibber RT connection."""

from __future__ import annotations
from typing import TYPE_CHECKING, Any
if TYPE_CHECKING:
    from ssl import SSLContext
    from collections.abc import Awaitable, Callable

import asyncio
import datetime as dt
import logging
import random

from gql import Client

from .exceptions import SubscriptionEndpointMissingError
from .home import TibberHome
from .websocket_transport import TibberWebsocketsTransport



LOCK_CONNECT = asyncio.Lock()

_LOGGER = logging.getLogger(__name__)


class TibberRT:
    """Class to handle real time connection with the Tibber api."""

    def __init__(self,
                 access_token: str,
                 timeout: int,
                 user_agent: str,
                 ssl: SSLContext | bool,
                 on_reconnect: Callable[[], Awaitable[None]] | None = None,
                 ) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent
        self._ssl_context = ssl
        self._on_reconnect = on_reconnect

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

    async def _reset_connection(self, unsubscribe_homes: bool = False) -> None:
        """Reset websocket connection state."""
        if self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        if unsubscribe_homes:
            for home in self._homes:
                home.rt_unsubscribe()
        try:
            if self.session is not None and self.sub_manager is not None:
                await self.sub_manager.close_async()
        finally:
            self.session = None
            self.sub_manager = None

    async def connect(self) -> None:
        """Start subscription manager."""
        self._create_sub_manager()

        # _create_sub_manager() already raises SubscriptionEndpointMissingError
        # if sub_endpoint is None, so sub_manager is guaranteed to be set here.
        # This guard catches future regressions if _create_sub_manager() changes.
        if self.sub_manager is None:
            raise RuntimeError("sub_manager not initialized before connect()")

        async with LOCK_CONNECT:
            if self.subscription_running:
                return
            if self._watchdog_runner is None:
                _LOGGER.debug("Starting watchdog")
                self._watchdog_running = True
                self._watchdog_runner = asyncio.create_task(self._watchdog())
                # Make sure that we see Watchdog raises in the log.
                self._watchdog_runner.add_done_callback(
                    lambda t: _LOGGER.error("Watchdog task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() else None
                )
            self.session = await self.sub_manager.connect_async()

    async def reconnect(self) -> None:
        """Reconnect and resubscribe all homes."""
        await self.connect()
        await self._resubscribe_homes()

    async def set_access_token(self, access_token: str) -> None:
        """Set access token."""
        reconnect_running = self.subscription_running or self._watchdog_runner is not None
        self._access_token = access_token
        await self._reset_connection(unsubscribe_homes=reconnect_running)

    def _create_sub_manager(self) -> None:
        if self.sub_endpoint is None:
            raise SubscriptionEndpointMissingError("Subscription endpoint not initialized")
        if self.sub_manager is not None:
            return
        self.sub_manager = Client(
            transport=TibberWebsocketsTransport(
                self.sub_endpoint,
                self._access_token,
                self._user_agent,
                ssl=self._ssl_context,
            ),
        )

    async def _watchdog(self) -> None:
        """Watchdog to keep connection alive."""

        # Watchdog is started from connect() which calls _create_sub_manager() first,
        # so sub_manager is guaranteed to exist and have the correct transport type.
        # This guard catches future regressions and or rouge watchdog calls.
        if self.sub_manager is None:
            raise RuntimeError("Watchdog started without sub_manager")
        if not isinstance(self.sub_manager.transport, TibberWebsocketsTransport):
            raise RuntimeError(
                f"Watchdog started with unexpected transport type: "
                f"{type(self.sub_manager.transport)}"
            )

        await asyncio.sleep(60)

        _retry_count = 0
        next_test_all_homes_running = dt.datetime.now(tz=dt.UTC)
        while self._watchdog_running:
            await asyncio.sleep(5)

            # Reconnect Backoff
            if self.sub_manager is None:
                continue

            if (
                self.sub_manager.transport.running
                and self.sub_manager.transport.reconnect_at
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

            self.sub_manager.transport.reconnect_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self._timeout)
            _LOGGER.error(
                "Watchdog: Connection is down, %s",
                self.sub_manager.transport.reconnect_at,
            )

            try:
                if self.session is not None:
                    await self.sub_manager.close_async()
            except Exception:
                _LOGGER.exception("Error in watchdog close")
            finally:
                # Reset connection state so _create_sub_manager() builds a fresh
                # transport with current credentials instead of reusing the stale one.
                self.session = None
                self.sub_manager = None

            if not self._watchdog_running:
                _LOGGER.debug("Watchdog: Stopping")
                return

            delay_seconds = min(
                random.SystemRandom().randint(1, 30) + _retry_count ** 2,
                5 * 60,
            )
            if self._on_reconnect is not None:
                try:
                    await self._on_reconnect()  # fetch fresh websocketSubscriptionUrl before reconnecting
                except Exception as err:
                    # Tibber API unreachable or token expired. No point connecting
                    # with stale credentials, wait and retry.
                    _retry_count += 1
                    _LOGGER.error(
                        "Failed to refresh connection info before reconnect, aborting: %s", err
                    )
                    await asyncio.sleep(delay_seconds)
                    continue

            self._create_sub_manager()

            try:
                self.session = await self.sub_manager.connect_async()
                await self._resubscribe_homes()
            except Exception as err:  # noqa: BLE001
                _retry_count += 1
                _LOGGER.error(
                    "Error in watchdog connect, retrying in %s seconds, %s: %s",
                    delay_seconds,
                    _retry_count,
                    err,
                    exc_info=_retry_count > 1,
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
        if self._sub_endpoint == sub_endpoint:
            return  # URL unchanged, don't replace a running sub_manager
        self._sub_endpoint = sub_endpoint
        if self.sub_manager is not None and isinstance(self.sub_manager.transport, TibberWebsocketsTransport):
            self.sub_manager = Client(
                transport=TibberWebsocketsTransport(
                    sub_endpoint,
                    self._access_token,
                    self._user_agent,
                    ssl=self._ssl_context,
                ),
            )
