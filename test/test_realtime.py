"""Tests for TibberRT class."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gql.client import AsyncClientSession, Client
from gql.transport.common.adapters.websockets import WebSocketsAdapter
from gql.transport.exceptions import TransportConnectionFailed, TransportError
from websockets.asyncio.connection import State

from tibber.exceptions import SubscriptionEndpointMissingError, WebsocketReconnectedError, WebsocketTransportError
from tibber.realtime import TibberRT, TibberWebsocketsTransport

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def timeout() -> int:
    return 30


@pytest.fixture(name="tibber_rt")
async def tibber_rt_fixture(mock_client: MagicMock, timeout: int) -> TibberRT:  # noqa: ARG001, ASYNC109
    """Create a TibberRT instance for testing."""
    tibber_rt = TibberRT(
        access_token="test_token",
        timeout=timeout,
        user_agent="test_agent",
        ssl=True,
    )
    await tibber_rt.set_subscription_endpoint("wss://test.endpoint")
    return tibber_rt


@pytest.fixture(name="mock_client")
def mock_client_fixture() -> Generator[MagicMock]:
    """Create a mock Client."""
    with patch("tibber.realtime.Client") as mock_client_class:
        mock_client = MagicMock(spec=Client)

        def create_client(
            *args: Any,  # noqa: ANN401, ARG001
            transport: TibberWebsocketsTransport,
            **kwargs: Any,  # noqa: ANN401, ARG001
        ) -> MagicMock:
            mock_client.transport = transport
            return mock_client

        mock_client_class.side_effect = create_client

        async def mock_connect_async(**kwargs: Any) -> MagicMock:  # noqa: ANN401, ARG001
            session = mock_client.session = MagicMock(spec=AsyncClientSession)
            mock_client.transport.adapter.websocket = MagicMock(state=State.OPEN)
            return session

        mock_client.connect_async = AsyncMock(wraps=mock_connect_async)

        yield mock_client


async def test_connect_disconnect(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """Test connect and disconnect."""
    # Should not raise
    await tibber_rt.disconnect()

    mock_client.close_async.assert_not_awaited()

    # First connect - transport not running, so connect_async should be called
    await tibber_rt.connect()

    mock_client.connect_async.assert_awaited_once()

    # Second connect should not call connect_async again since the client is already connected
    await tibber_rt.connect()

    # connect_async should still only have been called once
    mock_client.connect_async.assert_awaited_once()

    await tibber_rt.disconnect()

    mock_client.close_async.assert_awaited_once()


async def test_subscription_running(
    tibber_rt: TibberRT,
) -> None:
    """Test subscription running."""
    assert tibber_rt.subscription_running is False

    await tibber_rt.connect()

    assert tibber_rt.subscription_running is True

    await tibber_rt.disconnect()

    assert tibber_rt.subscription_running is False

    await tibber_rt.connect()

    assert tibber_rt.subscription_running is True


async def test_update_endpoint(mock_client: MagicMock) -> None:
    """Test update subscription endpoint."""
    tibber_rt = TibberRT(
        access_token="test_token",
        timeout=30,
        user_agent="test_agent",
        ssl=True,
    )

    with pytest.raises(SubscriptionEndpointMissingError, match="Subscription endpoint not initialized"):
        await tibber_rt.connect()

    mock_client.reset_mock()
    await tibber_rt.set_subscription_endpoint("wss://new.endpoint")
    await tibber_rt.connect()

    assert mock_client.transport.url == "wss://new.endpoint"
    assert mock_client.close_async.call_count == 0
    assert mock_client.connect_async.call_count == 1
    mock_client.reset_mock()

    await tibber_rt.set_subscription_endpoint("wss://new.endpoint")

    assert mock_client.transport.url == "wss://new.endpoint"
    assert mock_client.close_async.call_count == 0
    assert mock_client.connect_async.call_count == 0
    mock_client.reset_mock()

    await tibber_rt.disconnect()
    await tibber_rt.connect()

    assert mock_client.transport.url == "wss://new.endpoint"
    assert mock_client.close_async.call_count == 1
    assert mock_client.connect_async.call_count == 1
    mock_client.reset_mock()

    await tibber_rt.set_subscription_endpoint("wss://another_connected.endpoint")

    assert mock_client.transport.url == "wss://another_connected.endpoint"
    assert mock_client.close_async.call_count == 1
    assert mock_client.connect_async.call_count == 1
    mock_client.reset_mock()

    connect_event = asyncio.Event()
    original_connect_async = mock_client.connect_async

    async def mock_connect_async(**kwargs: Any) -> MagicMock:  # noqa: ANN401
        session = await original_connect_async(**kwargs)
        await connect_event.wait()
        return session

    mock_client.connect_async = AsyncMock(wraps=mock_connect_async)

    set_endpoint_task_1 = asyncio.create_task(tibber_rt.set_subscription_endpoint("wss://connected.endpoint.1"))
    set_endpoint_task_2 = asyncio.create_task(tibber_rt.set_subscription_endpoint("wss://connected.endpoint.2"))

    await asyncio.sleep(0.1)
    assert mock_client.transport.url == "wss://connected.endpoint.1"
    connect_event.set()
    await asyncio.gather(set_endpoint_task_1, set_endpoint_task_2)

    assert mock_client.transport.url == "wss://connected.endpoint.2"
    assert mock_client.close_async.call_count == 2
    assert mock_client.connect_async.call_count == 2


async def test_websocket_transport() -> None:
    """Test websocket transport."""
    tibber_connected = asyncio.Event()
    transport = TibberWebsocketsTransport(
        url="wss://test.endpoint",
        access_token="test_token",
        user_agent="test_agent",
        tibber_connected=tibber_connected,
    )
    transport.keep_alive_timeout = 0
    mock_adapter = MagicMock(spec=WebSocketsAdapter)
    sent_messages: asyncio.Queue[str] = asyncio.Queue()

    async def mock_receive() -> str:
        message = await sent_messages.get()
        answer: dict[str, Any]
        if json.loads(message)["type"] == "connection_init":
            answer = {"type": "connection_ack"}
        else:
            answer = {"type": "data", "payload": {"data": {"test": "value"}}}
        return json.dumps(answer)

    async def mock_send(message: str) -> None:
        await sent_messages.put(message)

    mock_adapter.connect = AsyncMock()
    mock_adapter.receive = AsyncMock(side_effect=mock_receive)
    mock_adapter.send = AsyncMock(side_effect=mock_send)
    transport.adapter = mock_adapter
    client = Client(transport=transport)

    connect_task = asyncio.create_task(client.connect_async())
    await tibber_connected.wait()

    assert tibber_connected.is_set()

    await connect_task
    await client.close_async()

    assert mock_adapter.connect.await_count == 1
    assert mock_adapter.send.await_count == 1
    mock_adapter.receive.assert_awaited()
    assert mock_adapter.close.await_count == 1
    assert not tibber_connected.is_set()


async def test_subscribe_raises_when_not_connected(tibber_rt: TibberRT) -> None:
    """subscribe must raise RuntimeError when called before connect."""
    with pytest.raises(RuntimeError, match="Connect must be called before subscribe"):
        await anext(tibber_rt.subscribe(MagicMock()))


async def test_subscribe_yields_results(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """subscribe must yield every item produced by the underlying session."""
    await tibber_rt.connect()

    sample = {"key": "value"}

    async def mock_subscribe(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        yield sample

    mock_client.session.subscribe = mock_subscribe

    results = [item async for item in tibber_rt.subscribe(MagicMock())]

    assert results == [sample]


async def test_subscribe_transport_connection_failed_calls_on_error_and_raises_reconnected(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """TransportConnectionFailed must call on_error, wait for reconnect, then raise WebsocketReconnectedError."""
    await tibber_rt.connect()

    err = TransportConnectionFailed("connection failed")
    caught: list[Exception] = []

    def on_error(exc: Exception) -> None:
        caught.append(exc)
        # Unblock _tibber_connected.wait() so the generator can finish
        tibber_rt._tibber_connected.set()  # noqa: SLF001

    async def failing_subscribe(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        raise err
        yield

    mock_client.session.subscribe = failing_subscribe

    with pytest.raises(WebsocketReconnectedError):
        await anext(tibber_rt.subscribe(MagicMock(), on_error=on_error))

    assert caught == [err]


async def test_subscribe_other_transport_error_raises_websocket_transport_error(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """A TransportError that is not TransportConnectionFailed must raise WebsocketTransportError."""
    await tibber_rt.connect()

    err = TransportError("generic transport error")

    async def failing_subscribe(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        raise err
        yield

    mock_client.session.subscribe = failing_subscribe

    with pytest.raises(WebsocketTransportError):
        await anext(tibber_rt.subscribe(MagicMock()))


async def test_reconnect_noop_when_not_connected(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """reconnect must be a no-op when the client is not connected."""
    await tibber_rt.reconnect()

    mock_client.connect_async.assert_not_awaited()
    mock_client.close_async.assert_not_awaited()


async def test_set_access_token_reconnects_with_new_token(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """set_access_token must update the token and reconnect so the new token is used."""
    await tibber_rt.connect()
    mock_client.connect_async.reset_mock()
    mock_client.close_async.reset_mock()

    await tibber_rt.set_access_token("new_token")

    mock_client.close_async.assert_awaited_once()
    mock_client.connect_async.assert_awaited_once()
    assert mock_client.transport.init_payload["token"] == "new_token"


@pytest.mark.parametrize("timeout", [0])
async def test_connect_timeout_leaves_no_session_and_subscription_not_running(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """When connect_async times out, subscription_running must remain False and no session is set."""

    async def slow_connect(**kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        await asyncio.sleep(9999)

    mock_client.connect_async = AsyncMock(side_effect=slow_connect)

    await tibber_rt.connect()

    assert tibber_rt.subscription_running is False

    await tibber_rt.disconnect()

    mock_client.close_async.assert_not_awaited()
    assert tibber_rt.subscription_running is False


async def test_subscribe_transport_connection_failed_without_on_error_raises_reconnected(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """TransportConnectionFailed with on_error=None must still raise WebsocketReconnectedError after reconnect."""
    await tibber_rt.connect()

    err = TransportConnectionFailed("connection failed")

    async def failing_subscribe(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401, ARG001
        raise err
        yield

    mock_client.session.subscribe = failing_subscribe

    async def set_connected_after_clear() -> None:
        # Wait until subscribe() clears the event, then unblock the wait()
        while tibber_rt._tibber_connected.is_set():  # noqa: ASYNC110, SLF001
            await asyncio.sleep(0)
        tibber_rt._tibber_connected.set()  # noqa: SLF001

    unblock_task = asyncio.create_task(set_connected_after_clear())

    with pytest.raises(WebsocketReconnectedError):
        await anext(tibber_rt.subscribe(MagicMock(), on_error=None))

    await unblock_task

    assert tibber_rt.subscription_running is True
