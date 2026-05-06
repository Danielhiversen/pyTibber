"""Tests for TibberRT class."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gql.client import AsyncClientSession, Client
from gql.transport.common.adapters.websockets import WebSocketsAdapter
from websockets.asyncio.connection import State

import tibber.realtime as realtime_module
from tibber.realtime import TibberRT
from tibber.websocket_transport import TibberWebsocketsTransport

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture(name="tibber_rt")
def tibber_rt_fixture() -> TibberRT:
    """Create a TibberRT instance for testing."""
    tibber_rt = TibberRT(
        access_token="test_token",
        timeout=30,
        user_agent="test_agent",
        ssl=True,
    )
    tibber_rt.sub_endpoint = "wss://test.endpoint"
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

    # First connect - transport not running, so connect_async should be called
    await tibber_rt.connect()

    mock_client.connect_async.assert_awaited_once()

    # Second connect should not call connect_async again since subscription_running is True
    await tibber_rt.connect()

    # connect_async should still only have been called once
    mock_client.connect_async.assert_awaited_once()

    await tibber_rt.disconnect()

    mock_client.close_async.assert_awaited_once()


async def test_subscription_running(
    mock_client: MagicMock,
    tibber_rt: TibberRT,
) -> None:
    """Test subscription_running."""
    assert tibber_rt.subscription_running is False

    await tibber_rt.connect()

    assert tibber_rt.subscription_running is True

    mock_client.transport.adapter.websocket.state = State.CLOSED

    assert tibber_rt.subscription_running is False

    await tibber_rt.disconnect()

    assert tibber_rt.subscription_running is False

    await tibber_rt.connect()

    assert tibber_rt.subscription_running is True

    mock_client.transport.adapter.websocket = None

    assert tibber_rt.subscription_running is False


async def test_update_endpoint(mock_client: MagicMock, tibber_rt: TibberRT) -> None:
    """Delay endpoint replacement until the current connection is reset."""
    await tibber_rt.connect()

    assert mock_client.transport.url == "wss://test.endpoint"

    # Set new endpoint
    tibber_rt.sub_endpoint = "wss://new.endpoint"

    assert tibber_rt.sub_endpoint == "wss://new.endpoint"
    assert mock_client.transport.url == "wss://test.endpoint"

    await tibber_rt.disconnect()
    await tibber_rt.connect()

    assert mock_client.transport.url == "wss://new.endpoint"


async def test_close_sub_manager_skips_clients_without_session(
    tibber_rt: TibberRT,
) -> None:
    """Avoid calling gql close_async when the client never got a session."""

    class FakeClient:
        def __init__(self) -> None:
            self.transport = TibberWebsocketsTransport(
                url="wss://test.endpoint",
                access_token="test_token",
                user_agent="test_agent",
            )
            self.close_async = AsyncMock()

    mock_client = FakeClient()

    tibber_rt.sub_manager = cast("Client", mock_client)

    await tibber_rt.disconnect()

    mock_client.close_async.assert_not_awaited()


async def test_set_access_token_resets_connection_after_releasing_lock(
    tibber_rt: TibberRT,
) -> None:
    """Reset through the public wrapper after updating the token under the lock."""
    reset_connection = AsyncMock()

    async def assert_unlocked_reset(unsubscribe_homes: bool = False, stop_watchdog: bool = True) -> None:
        assert tibber_rt._access_token == "new_token"  # noqa: SLF001
        assert not realtime_module.LOCK_CONNECT.locked()
        await reset_connection(unsubscribe_homes=unsubscribe_homes, stop_watchdog=stop_watchdog)

    tibber_rt._watchdog_runner = asyncio.create_task(asyncio.sleep(60))  # noqa: SLF001
    tibber_rt._reset_connection = assert_unlocked_reset  # type: ignore[method-assign]  # noqa: SLF001
    tibber_rt.reconnect = AsyncMock()  # type: ignore[method-assign]

    try:
        await tibber_rt.set_access_token("new_token")
    finally:
        if tibber_rt._watchdog_runner is not None and not tibber_rt._watchdog_runner.done():  # noqa: SLF001
            tibber_rt._watchdog_runner.cancel()  # noqa: SLF001

    reset_connection.assert_awaited_once_with(unsubscribe_homes=True, stop_watchdog=True)


async def test_watchdog_resets_connection_after_releasing_lock(
    monkeypatch: pytest.MonkeyPatch,
    tibber_rt: TibberRT,
) -> None:
    """Reset through the public wrapper when the watchdog detects a down connection."""
    reset_connection = AsyncMock()

    async def fake_sleep(_delay: float) -> None:
        return None

    async def assert_unlocked_reset(unsubscribe_homes: bool = False, stop_watchdog: bool = True) -> None:
        assert not realtime_module.LOCK_CONNECT.locked()
        await reset_connection(unsubscribe_homes=unsubscribe_homes, stop_watchdog=stop_watchdog)

    async def stop_watchdog_reconnect() -> None:
        tibber_rt._watchdog_running = False  # noqa: SLF001

    monkeypatch.setattr(realtime_module.asyncio, "sleep", fake_sleep)
    tibber_rt._reset_connection = assert_unlocked_reset  # type: ignore[method-assign]  # noqa: SLF001
    tibber_rt.reconnect = AsyncMock(side_effect=stop_watchdog_reconnect)  # type: ignore[method-assign]
    tibber_rt._watchdog_running = True  # noqa: SLF001

    await tibber_rt._watchdog()  # noqa: SLF001

    reset_connection.assert_awaited_once_with(unsubscribe_homes=False, stop_watchdog=False)


async def test_websocket_transport() -> None:
    """Test websocket transport."""
    transport = TibberWebsocketsTransport(
        url="wss://test.endpoint",
        access_token="test_token",
        user_agent="test_agent",
    )
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

    await connect_task
    await client.close_async()

    assert mock_adapter.connect.await_count == 1
    assert mock_adapter.send.await_count == 1
    mock_adapter.receive.assert_awaited()
    assert mock_adapter.close.await_count == 1
