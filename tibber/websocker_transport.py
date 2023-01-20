"""Websocket transport for Tibber."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from gql.transport.exceptions import TransportClosed
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
        self._timeout: int = 90
        self.reconnect_at: dt.datetime = dt.datetime.now() + dt.timedelta(
            seconds=self._timeout
        )

    @property
    def running(self) -> bool:
        """Is real time subscription running."""
        return (
            self.websocket is not None
            and self.websocket.open
            and self.reconnect_at > dt.datetime.now()
            and self.receive_data_task in asyncio.all_tasks()
        )

    async def _receive(self) -> str:
        """Wait the next message from the websocket connection."""
        try:
            msg = await asyncio.wait_for(super()._receive(), timeout=self._timeout)
        except asyncio.TimeoutError:
            _LOGGER.error("No data received from Tibber for %s seconds", self._timeout)
            raise
        self.reconnect_at = dt.datetime.now() + dt.timedelta(seconds=self._timeout)
        return msg

    async def close(self) -> None:
        await self._fail(TransportClosed("Tibber websocket closed by pyTibber"))
        await self.wait_closed()
