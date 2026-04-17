"""Tests for TibberHome."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch

import aiohttp
import pytest

import tibber
from tibber.exceptions import WebsocketReconnectedError, WebsocketTransportError
from tibber.gql_queries import INFO, REAL_TIME_CONSUMPTION_ENABLED
from tibber.realtime import TibberRT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

HOME_ID = "test-home-id"


@pytest.fixture
def tibber_connection(mock_websession: MagicMock) -> tibber.Tibber:
    tibber_client = tibber.Tibber(
        access_token="test-token",
        websession=mock_websession,
        user_agent="test",
    )
    tibber_client._user_agent = "test"  # noqa: SLF001
    return tibber_client


@pytest.fixture
def mock_websession() -> MagicMock:
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post = AsyncMock()
    return session


@pytest.fixture
def mock_realtime(tibber_connection: tibber.Tibber) -> MagicMock:
    rt = create_autospec(TibberRT, instance=True, subscription_running=False)
    rt.connect = AsyncMock(side_effect=lambda: setattr(rt, "subscription_running", True))
    tibber_connection.realtime = rt
    return rt


@pytest.fixture
def home(tibber_connection: tibber.Tibber) -> tibber.TibberHome:
    home = tibber.TibberHome(HOME_ID, tibber_connection)
    home._has_real_time_consumption = True  # noqa: SLF001
    return home


def _make_blocking_subscribe(
    yielded: list[Any],
) -> tuple[asyncio.Event, Any]:
    """Return (release_event, subscribe_fn) that yields *yielded* then blocks."""
    release = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        for item in yielded:
            yield item
        await release.wait()

    return release, subscribe


async def test_rt_subscribe_connects_and_calls_callback(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """Test that rt_subscribe connects via realtime and delivers subscription data to the callback."""
    sample_data = {"key": "value"}
    _, subscribe_fn = _make_blocking_subscribe([sample_data])
    mock_realtime.subscribe = subscribe_fn

    received: list[dict] = []
    callback_called = asyncio.Event()

    def callback(data: dict) -> None:
        received.append(data)
        callback_called.set()

    await home.rt_subscribe(callback)
    await asyncio.wait_for(callback_called.wait(), timeout=1.0)

    mock_realtime.connect.assert_awaited_once()
    assert received == [{"data": sample_data}]
    assert home.rt_subscription_running

    home.rt_unsubscribe()
    assert not home.rt_subscription_running


async def test_rt_unsubscribe_noop_when_not_subscribed(home: tibber.TibberHome) -> None:
    """Calling rt_unsubscribe on a fresh home must not raise."""
    assert not home.rt_subscription_running
    home.rt_unsubscribe()  # should be a no-op
    assert not home.rt_subscription_running


async def test_rt_subscribe_multiple_items_all_delivered(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """All items yielded by subscribe must be delivered to the callback in order."""
    items = [{"n": 1}, {"n": 2}, {"n": 3}]
    _, subscribe_fn = _make_blocking_subscribe(items)
    mock_realtime.subscribe = subscribe_fn

    received: list[dict] = []
    all_received = asyncio.Event()

    def callback(data: dict) -> None:
        received.append(data)
        if len(received) == len(items):
            all_received.set()

    await home.rt_subscribe(callback)
    await asyncio.wait_for(all_received.wait(), timeout=1.0)

    assert received == [{"data": item} for item in items]

    home.rt_unsubscribe()


@pytest.mark.parametrize(
    ("real_time_consumption", "http_calls"),
    [
        (
            False,
            [
                # Initial subscription (first call returns True)
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": REAL_TIME_CONSUMPTION_ENABLED % HOME_ID, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": INFO, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
                # Resubscription (returns False, so no INFO call)
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": REAL_TIME_CONSUMPTION_ENABLED % HOME_ID, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
            ],
        ),
        (
            True,
            [
                # Initial subscription (first call returns True)
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": REAL_TIME_CONSUMPTION_ENABLED % HOME_ID, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": INFO, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
                # Resubscription (returns True)
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": REAL_TIME_CONSUMPTION_ENABLED % HOME_ID, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
                call(
                    "https://api.tibber.com/v1-beta/gql",
                    headers={
                        "Authorization": "Bearer test-token",
                        "User-Agent": "test",
                    },
                    data={"query": INFO, "variables": {}},
                    timeout=aiohttp.ClientTimeout(total=10),
                ),
            ],
        ),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        WebsocketReconnectedError("reconnected"),
        WebsocketTransportError("transport error"),
        RuntimeError("unexpected"),
    ],
)
@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_rt_subscribe_on_error_called_on_exception(
    mock_websession: MagicMock,
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    error: Exception,
    real_time_consumption: bool,
    http_calls: list,
) -> None:
    """on_error must be called when subscribe raises an exception."""
    # Initialize info structure so update_real_time_consumption_enabled can update it
    home.info = {
        "viewer": {
            "home": {
                "features": {"realTimeConsumptionEnabled": real_time_consumption},
            },
        },
    }

    # Track which call number we're on to return different responses
    call_count = 0
    resubscribe_called = asyncio.Event()

    def make_response(rt_enabled: bool) -> MagicMock:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_type = "application/json"
        mock_response.json = AsyncMock(
            return_value={
                "data": {
                    "viewer": {
                        "home": {
                            "id": HOME_ID,
                            "features": {"realTimeConsumptionEnabled": rt_enabled},
                        },
                    },
                },
            },
        )
        return mock_response

    async def post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ARG001, ANN401
        nonlocal call_count
        call_count += 1
        # First two calls (initial subscription) always returns True to start subscription
        # Subsequent calls (resubscription) return the real_time_consumption value
        if call_count <= 2:
            return make_response(True)
        resubscribe_called.set()
        return make_response(real_time_consumption)

    mock_websession.post.side_effect = post_side_effect

    wait_for_events = asyncio.Event()
    wait_for_events.set()  # allow subscribe to raise immediately

    async def subscribe_raises(*args: Any, **kwargs: Any) -> AsyncGenerator:  # noqa: ANN401, ARG001
        await wait_for_events.wait()
        raise error
        yield

    mock_realtime.subscribe = subscribe_raises

    on_error_called = asyncio.Event()
    caught: list[Exception] = []

    def on_error(exc: Exception) -> None:
        caught.append(exc)
        on_error_called.set()
        wait_for_events.clear()  # allow test to control the flow after error is caught

    await home.rt_subscribe(MagicMock(), on_error=on_error)
    await asyncio.wait_for(on_error_called.wait(), timeout=1.0)

    assert caught == [error]
    # resubscription should have been triggered - wait for HTTP calls to complete
    await asyncio.wait_for(resubscribe_called.wait(), timeout=1.0)
    assert mock_websession.post.call_count == len(http_calls)
    assert mock_websession.post.call_args_list == http_calls
    assert home.rt_subscription_running is real_time_consumption

    home.rt_unsubscribe()

    assert not home.rt_subscription_running


async def test_rt_subscribe_no_crash_when_subscribe_raises_without_on_error(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """_start_listen must not propagate exceptions when no on_error is provided."""

    async def subscribe_raises(*args: Any, **kwargs: Any) -> AsyncGenerator:  # noqa: ANN401, ARG001
        raise WebsocketTransportError("transport error")
        yield

    mock_realtime.subscribe = subscribe_raises

    callback = MagicMock()
    await home.rt_subscribe(callback)

    # give the listener task a chance to run and finish without raising
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    callback.assert_not_called()
    home.rt_unsubscribe()


async def test_rt_resubscribe_raises_without_prior_subscribe(home: tibber.TibberHome) -> None:
    """rt_resubscribe must raise RuntimeError when rt_subscribe has not been called."""
    with pytest.raises(RuntimeError, match="rt_subscribe"):
        await home.rt_resubscribe()


async def test_rt_subscribe_raises_when_already_subscribed(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """rt_subscribe must raise RuntimeError when called while already subscribed."""
    _, subscribe_fn = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn

    callback = MagicMock()
    await home.rt_subscribe(callback)

    with pytest.raises(RuntimeError, match="rt_unsubscribe"):
        await home.rt_subscribe(callback)

    home.rt_unsubscribe()


async def test_rt_resubscribe_emits_deprecation_warning(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """rt_resubscribe must emit a DeprecationWarning."""
    _, subscribe_fn = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn

    callback = MagicMock()
    await home.rt_subscribe(callback)

    with pytest.warns(DeprecationWarning, match="deprecated"):
        await home.rt_resubscribe()

    home.rt_unsubscribe()
