"""Websocket transport for Tibber."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import random

from gql.transport.websockets import WebsocketsTransport

_LOGGER = logging.getLogger(__name__)


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
        self._timeout: int = 900
        self._reconnect_at: dt.datetime = dt.datetime.now() + dt.timedelta(
            seconds=self._timeout
        )

    @property
    def running(self) -> bool:
        """Is real time subscription running."""
        return (
            self.websocket is not None
            and self.websocket.open
            and self._reconnect_at > dt.datetime.now()
            and self.receive_data_task in asyncio.all_tasks()
        )

    async def connect(self) -> None:
        """Connect to websockets."""
        if self._watchdog_runner is None:
            _LOGGER.debug("Starting watchdog")
            self._watchdog_running = True
            self._watchdog_runner = asyncio.create_task(self._watchdog())
        await super().connect()

    async def close(self) -> None:
        """Close websockets."""
        if self._watchdog_runner is not None:
            _LOGGER.debug("Stopping watchdog")
            self._watchdog_running = False
            self._watchdog_runner.cancel()
            self._watchdog_runner = None
        await super().close()

    async def _receive(self) -> str:
        """Wait the next message from the websocket connection."""
        try:
            msg = await asyncio.wait_for(super()._receive(), timeout=self._timeout)
        except asyncio.TimeoutError:
            _LOGGER.error("No data received from Tibber for %s seconds", self._timeout)
            raise
        self._reconnect_at = dt.datetime.now() + dt.timedelta(seconds=self._timeout)
        return msg

    async def _watchdog(self) -> None:
        """Watchdog to keep connection alive."""
        await asyncio.sleep(60)

        _retry_count = 0
        while self._watchdog_running:
            if self.running:
                _retry_count = 0
                _LOGGER.debug("Watchdog: Connection is alive")
                await asyncio.sleep(5)
                continue

            _LOGGER.error(
                "Watchdog: Connection is down, %s, %s",
                self._reconnect_at,
                self.receive_data_task in asyncio.all_tasks(),
            )
            self._reconnect_at = dt.datetime.now() + dt.timedelta(seconds=self._timeout)

            try:
                await super().close()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Error in watchdog close")

            delay_seconds = min(
                random.SystemRandom().randint(1, 60) + _retry_count**2,
                60 * 60,
            )
            _retry_count += 1

            await asyncio.sleep(delay_seconds)

            try:
                await super().connect()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.error(
                    "Error in watchdog connect, will retry. Retry count: %s",
                    _retry_count,
                    exc_info=_retry_count > 1,
                )
            else:
                _LOGGER.info("Watchdog: Reconnected successfully")
