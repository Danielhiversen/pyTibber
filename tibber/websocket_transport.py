"""Websocket transport for Tibber."""

import asyncio
import datetime as dt
import logging
from ssl import SSLContext

from gql.transport.exceptions import TransportClosed
from gql.transport.websockets import WebsocketsTransport
from websockets.asyncio.connection import State

_LOGGER = logging.getLogger(__name__)


class TibberWebsocketsTransport(WebsocketsTransport):
    """Tibber websockets transport."""

    def __init__(self, url: str, access_token: str, user_agent: str, ssl: SSLContext | bool = True) -> None:
        """Initialize TibberWebsocketsTransport."""
        super().__init__(
            url=url,
            init_payload={"token": access_token},
            headers={"User-Agent": user_agent},
            ping_interval=30,
            ssl=ssl,
        )
        self._user_agent: str = user_agent
        self._timeout: int = 90
        self.reconnect_at: dt.datetime = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self._timeout)

    @property
    def running(self) -> bool:
        """Is real time subscription running."""
        return hasattr(self, "websocket") and self.websocket is not None and self.websocket.state is State.OPEN

    async def _receive(self) -> str:
        """Wait the next message from the websocket connection."""
        try:
            msg = await asyncio.wait_for(super()._receive(), timeout=self._timeout)
        except TimeoutError:
            _LOGGER.error("No data received from Tibber for %s seconds", self._timeout)
            raise
        self.reconnect_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=self._timeout)
        return msg

    async def close(self) -> None:
        """Close the websocket connection."""
        await self._fail(TransportClosed(f"Tibber websocket closed by {self._user_agent}"))
        await self.wait_closed()
