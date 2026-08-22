"""Tests for TibberHome."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch

import aiohttp
import pytest

import tibber
from tibber.exceptions import (
    InvalidLoginError,
    SubscriptionFailedError,
    WebsocketReconnectedError,
    WebsocketTransportError,
)
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


@pytest.mark.parametrize("enabled", [True, False])
async def test_update_real_time_consumption_enabled_without_prior_info(
    mock_websession: MagicMock,
    home: tibber.TibberHome,
    enabled: bool,
) -> None:
    """Updating the flag must not require info to be populated first."""
    home._has_real_time_consumption = None  # noqa: SLF001
    assert home.info == {}

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(
        return_value={
            "data": {
                "viewer": {
                    "home": {
                        "id": HOME_ID,
                        "features": {"realTimeConsumptionEnabled": enabled},
                    },
                },
            },
        },
    )
    mock_websession.post = AsyncMock(return_value=mock_response)

    await home.update_real_time_consumption_enabled()

    assert home.has_real_time_consumption is enabled
    # The method must not mutate info as a side effect.
    assert home.info == {}


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


@pytest.mark.parametrize(
    "error",
    [
        WebsocketTransportError("transport error"),
        WebsocketReconnectedError("reconnected"),
    ],
)
@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_rt_subscribe_no_crash_when_subscribe_raises_without_on_error(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    error: Exception,
) -> None:
    """_start_listen must not propagate exceptions when no on_error is provided."""

    async def subscribe_raises(*args: Any, **kwargs: Any) -> AsyncGenerator:  # noqa: ANN401, ARG001
        raise error
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


def _make_reconnect_watchdog_stopper() -> tuple[asyncio.Event, AsyncMock]:
    """Return (reconnected_event, reconnect_mock) that bounds the watchdog loop.

    The reconnect mock records the call and then raises CancelledError, which
    terminates the watchdog coroutine right after the first reconnect. This keeps
    the assertion on a single reconnect deterministic and stops the watchdog from
    scheduling a follow-up resubscribe that would leak into teardown.
    """
    reconnected = asyncio.Event()

    async def reconnect() -> None:
        reconnected.set()
        raise asyncio.CancelledError

    return reconnected, AsyncMock(side_effect=reconnect)


@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 0.2)
async def test_rt_subscription_stays_connected_while_data_flows(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """The watchdog must not reconnect while fresh data keeps arriving.

    Data streamed faster than the timeout keeps refreshing the freshness
    timestamp the watchdog relies on, so it must leave the subscription alone.
    """
    stop = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        while not stop.is_set():
            yield {"liveMeasurement": {}}
            await asyncio.sleep(0.02)

    mock_realtime.subscribe = subscribe

    received: list[dict] = []
    got_enough = asyncio.Event()

    def callback(data: dict) -> None:
        received.append(data)
        if len(received) >= 5:
            got_enough.set()

    await home.rt_subscribe(callback)
    # Enough data has flowed through the real handler to refresh the timestamp.
    await asyncio.wait_for(got_enough.wait(), timeout=2.0)
    # Let at least one full watchdog cycle run while data keeps arriving.
    await asyncio.sleep(0.5)

    mock_realtime.reconnect.assert_not_awaited()
    assert home.rt_subscription_running

    stop.set()
    home.rt_unsubscribe()
    assert not home.rt_subscription_running


@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 0.05)
async def test_rt_subscription_reconnects_when_data_goes_stale(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """The watchdog must reconnect after a subscription stops delivering data."""
    reconnected, reconnect = _make_reconnect_watchdog_stopper()
    mock_realtime.reconnect = reconnect

    # One datum, then the subscription blocks forever and the data goes stale.
    _, subscribe_fn = _make_blocking_subscribe([{"liveMeasurement": {}}])
    mock_realtime.subscribe = subscribe_fn

    received = asyncio.Event()

    def callback(data: dict) -> None:  # noqa: ARG001
        received.set()

    await home.rt_subscribe(callback)
    # Confirm the subscription started and delivered fresh data first.
    await asyncio.wait_for(received.wait(), timeout=2.0)
    # Data has now stopped, so the watchdog must reconnect once it goes stale.
    await asyncio.wait_for(reconnected.wait(), timeout=2.0)

    mock_realtime.reconnect.assert_awaited_once()
    assert not home.rt_subscription_running

    home.rt_unsubscribe()


@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 0)
async def test_rt_subscription_reconnects_when_no_data_received(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """A subscription that never delivered data must be treated as stale."""
    reconnected, reconnect = _make_reconnect_watchdog_stopper()
    mock_realtime.reconnect = reconnect

    # Never yields any data.
    _, subscribe_fn = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn

    await home.rt_subscribe(MagicMock())
    await asyncio.wait_for(reconnected.wait(), timeout=2.0)

    mock_realtime.reconnect.assert_awaited_once()
    assert not home.rt_subscription_running

    home.rt_unsubscribe()


@pytest.mark.parametrize(
    "post_error",
    [
        aiohttp.ClientError("boom"),
        TimeoutError(),
        ValueError("boom"),
        InvalidLoginError(400, '"exp" claim timestamp check failed', "UNAUTHENTICATED"),
    ],
)
@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_rt_subscribe_recovers_when_resubscribe_step_fails(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    caplog: pytest.LogCaptureFixture,
    post_error: Exception,
) -> None:
    """A failing resubscribe step is logged and must not abort the subscription.

    The consumption-status refresh and the info update both run on the initial
    subscribe. When they fail (transport errors take the TimeoutError/ClientError
    branch, other errors the generic branch) the last known state is kept and the
    subscription still comes up.
    """
    mock_websession.post.side_effect = post_error

    sample_data = {"key": "value"}
    _, subscribe_fn = _make_blocking_subscribe([sample_data])
    mock_realtime.subscribe = subscribe_fn

    received: list[dict] = []
    callback_called = asyncio.Event()

    def callback(data: dict) -> None:
        received.append(data)
        callback_called.set()

    with caplog.at_level(logging.WARNING):
        await home.rt_subscribe(callback)
        await asyncio.wait_for(callback_called.wait(), timeout=1.0)

    mock_realtime.connect.assert_awaited_once()
    assert received == [{"data": sample_data}]
    assert home.rt_subscription_running
    assert "keeping last known status" in caplog.text
    assert "keeping last known info" in caplog.text

    home.rt_unsubscribe()


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
@pytest.mark.asyncio
async def test_resubscribe_step_known_error_logs_without_traceback(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Known API errors (HttpExceptionError) must log without a traceback.

    Unknown errors (e.g. ValueError) must still emit exc_info so the traceback
    is preserved for debugging.
    """
    # --- known error: no traceback ---
    mock_websession.post.side_effect = InvalidLoginError(
        400,
        '"exp" claim timestamp check failed',
        "UNAUTHENTICATED",
    )
    _, subscribe_fn = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn

    with caplog.at_level(logging.WARNING):
        await home.rt_subscribe(MagicMock())

    # The warning message contains the failure_message text; exc_info must be None.
    known_records = [r for r in caplog.records if "keeping last known" in r.message]
    assert known_records, "Expected warning records for the resubscribe step"
    for rec in known_records:
        assert rec.exc_info is None, "Known API error must not include traceback"

    home.rt_unsubscribe()
    caplog.clear()

    # --- unknown error: traceback preserved ---
    mock_websession.post.side_effect = ValueError("something unexpected")
    _, subscribe_fn2 = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn2

    with caplog.at_level(logging.WARNING):
        await home.rt_subscribe(MagicMock())

    # exc_info must be set so the traceback is visible in logs for unexpected errors.
    unknown_records = [r for r in caplog.records if "keeping last known" in r.message]
    assert unknown_records, "Expected warning records for the resubscribe step"
    for rec in unknown_records:
        assert rec.exc_info is not None, "Unknown error must include traceback"

    home.rt_unsubscribe()


@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 0)
async def test_rt_subscription_timeout_calls_on_error(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
) -> None:
    """The watchdog must deliver a SubscriptionFailedError to on_error on timeout."""
    reconnected, reconnect = _make_reconnect_watchdog_stopper()
    mock_realtime.reconnect = reconnect

    # Never yields any data, so the watchdog treats the subscription as stale.
    _, subscribe_fn = _make_blocking_subscribe([])
    mock_realtime.subscribe = subscribe_fn

    caught: list[Exception] = []
    on_error_called = asyncio.Event()

    def on_error(exc: Exception) -> None:
        caught.append(exc)
        on_error_called.set()

    await home.rt_subscribe(MagicMock(), on_error=on_error)
    await asyncio.wait_for(on_error_called.wait(), timeout=2.0)
    await asyncio.wait_for(reconnected.wait(), timeout=2.0)

    assert len(caught) == 1
    assert isinstance(caught[0], SubscriptionFailedError)
    assert HOME_ID in str(caught[0])
    mock_realtime.reconnect.assert_awaited_once()

    home.rt_unsubscribe()


async def test_rt_subscribe_survives_callback_exception(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception raised by the callback must not stop later items from arriving."""
    items = [{"first": 1}, {"second": 2}]
    _, subscribe_fn = _make_blocking_subscribe(items)
    mock_realtime.subscribe = subscribe_fn

    calls: list[dict] = []
    second_received = asyncio.Event()

    def callback(data: dict) -> None:
        calls.append(data)
        if len(calls) == 1:
            raise ValueError("callback boom")
        second_received.set()

    with caplog.at_level(logging.ERROR):
        await home.rt_subscribe(callback)
        await asyncio.wait_for(second_received.wait(), timeout=1.0)

    assert calls == [{"data": items[0]}, {"data": items[1]}]
    assert home.rt_subscription_running
    assert "Error in rt_subscribe callback" in caplog.text

    home.rt_unsubscribe()


async def test_update_real_time_consumption_enabled_ignores_empty_response(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty response must leave has_real_time_consumption unchanged."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value={})
    mock_websession.post = AsyncMock(return_value=mock_response)

    with caplog.at_level(logging.ERROR):
        await home.update_real_time_consumption_enabled()

    assert home.has_real_time_consumption is True
    assert "Could not get real time consumption enabled status." in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        {"viewer": {"home": {}}},  # missing "features" -> KeyError
        {"viewer": None},  # not subscriptable -> TypeError
    ],
)
async def test_update_real_time_consumption_enabled_handles_malformed_payload(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
    payload: dict,
) -> None:
    """A malformed payload must resolve the flag to None."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value={"data": payload})
    mock_websession.post = AsyncMock(return_value=mock_response)

    await home.update_real_time_consumption_enabled()

    assert home.has_real_time_consumption is None


def _make_enabled_response() -> AsyncMock:
    """Return a post mock whose response reports real time consumption enabled."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(
        return_value={
            "data": {
                "viewer": {
                    "home": {
                        "id": HOME_ID,
                        "features": {"realTimeConsumptionEnabled": True},
                    },
                },
            },
        },
    )
    return AsyncMock(return_value=mock_response)


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 0.05)
async def test_rt_subscription_resubscribes_after_watchdog_reconnect(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
) -> None:
    """After a watchdog reconnect the subscription must resubscribe and resume data.

    The first subscription never yields, so its data goes stale and the watchdog
    reconnects. When reconnect succeeds (instead of raising) the watchdog schedules
    a resubscribe, which brings up a fresh subscription that delivers data again.
    """
    mock_websession.post = _make_enabled_response()

    call_count = 0
    stop = asyncio.Event()
    got_data = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First subscription never yields: the data goes stale.
            await asyncio.Event().wait()
        else:
            # After the reconnect the fresh subscription keeps data flowing.
            while not stop.is_set():
                yield {"liveMeasurement": {}}
                await asyncio.sleep(0.01)

    mock_realtime.subscribe = subscribe

    def callback(data: dict) -> None:  # noqa: ARG001
        got_data.set()

    await home.rt_subscribe(callback)
    await asyncio.wait_for(got_data.wait(), timeout=2.0)

    assert mock_realtime.reconnect.await_count >= 1
    assert home.rt_subscription_running

    stop.set()
    home.rt_unsubscribe()


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
@patch("tibber.home.RT_SUBSCRIPTION_TIMEOUT", 3600)
async def test_rt_subscribe_recovers_from_repeated_errors(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
) -> None:
    """Repeated subscription errors keep triggering resubscription until it holds.

    The second resubscribe schedule cancels the previous (already completed)
    resubscribe task, and the subscription ends up running once subscribe stops
    failing. The watchdog is kept idle with a long timeout.
    """
    mock_websession.post = _make_enabled_response()

    call_count = 0
    settled = asyncio.Event()
    block = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise WebsocketTransportError("transport error")
            yield
        # The third subscription stays open, so resubscription settles.
        settled.set()
        await block.wait()

    mock_realtime.subscribe = subscribe

    await home.rt_subscribe(MagicMock())
    await asyncio.wait_for(settled.wait(), timeout=1.0)

    assert home.rt_subscription_running
    assert mock_realtime.connect.await_count == 3

    home.rt_unsubscribe()
