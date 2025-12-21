"""Tibber RT connection."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from ssl import SSLContext
from typing import TYPE_CHECKING, Any

from gql import Client, GraphQLRequest
from gql.transport.exceptions import TransportClosed, TransportConnectionFailed, TransportError
from gql.transport.websockets import WebsocketsTransport
from tenacity import before_sleep_log, retry, wait_exponential_jitter

from .exceptions import SubscriptionEndpointMissingError, WebsocketReconnectedError, WebsocketTransportError

if TYPE_CHECKING:
    from gql.client import AsyncClientSession

KEEP_ALIVE_TIMEOUT = 90
LOCK_CONNECT = asyncio.Lock()
MIN_RECONNECT_INTERVAL = 1
MAX_RECONNECT_INTERVAL = 60
PING_INTERVAL = 30
PONG_TIMEOUT = 20

_LOGGER = logging.getLogger(__name__)


class TibberRT:
    """Class to handle real time connection with the Tibber api."""

    def __init__(self, access_token: str, timeout: int, user_agent: str, ssl: SSLContext | bool) -> None:
        """Initialize the Tibber connection.

        :param access_token: The access token to access the Tibber API with.
        :param timeout: The timeout in seconds to use when communicating with the Tibber API.
        :param user_agent: User agent identifier for the platform running this. Required if websession is None.
        """
        self._access_token: str = access_token
        self._timeout: int = timeout
        self._user_agent: str = user_agent
        self._ssl_context = ssl
        self._sub_endpoint: str | None = None
        self._tibber_connected = asyncio.Event()
        self._client: Client | None = None
        self.subscription_running = False
        self._session: AsyncClientSession | None = None

    def _create_client(self) -> Client:
        """Create a new gql Client with the current transport settings."""
        self._tibber_connected.clear()
        return Client(
            transport=TibberWebsocketsTransport(
                self._sub_endpoint,
                self._access_token,
                self._user_agent,
                ssl=self._ssl_context,
                tibber_connected=self._tibber_connected,
            ),
        )

    async def disconnect(self) -> None:
        """Disconnect the websocket client."""
        _LOGGER.debug("Stopping subscription manager")
        async with LOCK_CONNECT:
            await self._disconnect()

    async def _disconnect(self) -> None:
        """Disconnect the websocket client."""
        if self._client is not None and self._session is not None:
            await self._client.close_async()
            self._session = None
        self.subscription_running = False

    async def connect(self) -> None:
        """Connect the websocket client."""
        async with LOCK_CONNECT:
            await self._connect()

    async def _connect(self) -> None:
        """Connect the websocket client."""
        if self._sub_endpoint is None:
            raise SubscriptionEndpointMissingError("Subscription endpoint not initialized")

        if self.subscription_running or self._session:
            return

        self._client = self._create_client()
        try:
            self._session = await asyncio.wait_for(
                self._client.connect_async(
                    reconnecting=True,
                    retry_connect=retry(
                        wait=wait_exponential_jitter(
                            initial=MIN_RECONNECT_INTERVAL,
                            max=MAX_RECONNECT_INTERVAL,
                            jitter=MAX_RECONNECT_INTERVAL,
                        ),
                        before_sleep=before_sleep_log(_LOGGER, logging.INFO),
                    ),
                ),
                timeout=self._timeout,
            )
        except TimeoutError as err:
            _LOGGER.debug("Timeout connecting to websocket: %s", err)
            # The connection will be retried by the reconnecting task
        else:
            self.subscription_running = True

    async def reconnect(self) -> None:
        """Reconnect the websocket client."""
        async with LOCK_CONNECT:
            if self._session is None:
                return
            _LOGGER.debug("Reconnecting websocket client")
            await self._disconnect()
            await self._connect()

    async def set_access_token(self, access_token: str) -> None:
        """Set access token."""
        self._access_token = access_token
        await self.reconnect()

    async def subscribe(
        self,
        request: GraphQLRequest,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Subscribe to a GraphQL query."""
        if self._session is None:
            raise RuntimeError("Connect must be called before subscribe")

        try:
            async for result in self._session.subscribe(request):
                yield result
        except TransportError as err:
            _LOGGER.debug("%s: %s", err.__class__.__name__, err)
            self.subscription_running = False
            self._tibber_connected.clear()
            if isinstance(err, TransportConnectionFailed):
                if on_error:
                    on_error(err)
                _LOGGER.debug("Waiting for reconnect")
                await self._tibber_connected.wait()
                self.subscription_running = True
                _LOGGER.info("Reconnected")
                raise WebsocketReconnectedError("Websocket reconnected") from err
            raise WebsocketTransportError(err) from err

    async def set_subscription_endpoint(self, url: str) -> None:
        """Set subscription endpoint."""
        old_url = self._sub_endpoint
        if url == old_url:
            return
        _LOGGER.debug("Updating subscription endpoint to %s", url)
        self._sub_endpoint = url
        await self.reconnect()


class TibberWebsocketsTransport(WebsocketsTransport):
    """Tibber websockets transport."""

    def __init__(
        self,
        url: str,
        access_token: str,
        user_agent: str,
        *,
        ssl: SSLContext | bool = True,
        tibber_connected: asyncio.Event,
    ) -> None:
        """Initialize TibberWebsocketsTransport."""
        super().__init__(
            url=url,
            init_payload={"token": access_token},
            headers={"User-Agent": user_agent},
            ssl=ssl,
            keep_alive_timeout=KEEP_ALIVE_TIMEOUT,
            ping_interval=PING_INTERVAL,
            pong_timeout=PONG_TIMEOUT,
        )
        self._tibber_connected = tibber_connected
        self._user_agent = user_agent

    async def _after_connect(self) -> None:
        """Hook to add custom code for subclasses.

        Called after the connection has been established.
        """
        await super()._after_connect()
        self._tibber_connected.set()

    async def close(self) -> None:
        """Close the websocket connection.

        This method is only called by the client.
        """
        await self._fail(TransportClosed(f"Tibber websocket closed by {self._user_agent}"))
        await self.wait_closed()

    async def _close_hook(self) -> None:
        """Hook called by WebsocketsTransportBase on connection close.

        This method is called when the connection is closed
        for any reason (not only by the client).
        """
        self._tibber_connected.clear()
        await super()._close_hook()
