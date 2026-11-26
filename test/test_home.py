"""Tests for TibberHome."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call, create_autospec, patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import pytest

import tibber
from tibber.data_api import PriceDetails, PriceInterval, Prices
from tibber.exceptions import (
    InvalidLoginError,
    PricesNotAvailableError,
    RateLimitExceededError,
    RealTimeConsumptionDisabledError,
    SubscriptionFailedError,
    WebsocketReconnectedError,
    WebsocketTransportError,
)
from tibber.gql_queries import INFO, REAL_TIME_CONSUMPTION_ENABLED
from tibber.home import REAL_TIME_CONSUMPTION_DISABLED_GRACE
from tibber.realtime import TibberRT

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .conftest import FixedDateTime

HOME_ID = "test-home-id"


def _prices_payload() -> dict[str, Any]:
    """Return a Data API prices payload."""
    return {
        "currency": "NOK",
        "items": [{"time": "2026-05-06T00:00:00+02:00", "consumption": {"price": 1.0}}],
    }


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
    session.request = AsyncMock(return_value=_json_response(_prices_payload()))
    return session


@pytest.fixture
def mock_realtime(tibber_connection: tibber.Tibber) -> MagicMock:
    rt = create_autospec(TibberRT, instance=True, subscription_running=False)
    rt.connect = AsyncMock(side_effect=lambda: setattr(rt, "subscription_running", True))
    tibber_connection.realtime = rt
    return rt


@pytest.fixture
async def home(tibber_connection: tibber.Tibber, mock_websession: MagicMock) -> tibber.TibberHome:
    """Return a home that already believes real time consumption is enabled.

    The status is seeded through the public API rather than by assigning to the private attribute,
    so the fixture keeps working across refactors. The post mock is replaced afterwards, so that
    tests start from a clean call count.
    """
    home = tibber.TibberHome(HOME_ID, tibber_connection)
    mock_websession.post = _make_status_response(rt_enabled=True)
    await home.update_real_time_consumption_enabled()
    mock_websession.post = AsyncMock()
    return home


def _status_payload(rt_enabled: bool | None, *, with_features: bool = True) -> dict[str, Any]:
    """Return a REAL_TIME_CONSUMPTION_ENABLED payload reporting the given status."""
    home_payload: dict[str, Any] = {"id": HOME_ID}
    if with_features:
        home_payload["features"] = {"realTimeConsumptionEnabled": rt_enabled}
    return {"data": {"viewer": {"home": home_payload}}}


def _json_response(payload: dict[str, Any]) -> MagicMock:
    """Return a mocked 200 JSON response carrying the given payload."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value=payload)
    return mock_response


def _make_status_response(rt_enabled: bool) -> AsyncMock:
    """Return a post mock whose response reports the given real time consumption status."""
    return AsyncMock(return_value=_json_response(_status_payload(rt_enabled)))


def _gql_call(query: str) -> Any:  # noqa: ANN401
    """Return the expected websession.post call for a GraphQL query."""
    return call(
        "https://api.tibber.com/v1-beta/gql",
        headers={
            "Authorization": "Bearer test-token",
            "User-Agent": "test",
        },
        data={"query": query, "variables": {}},
        timeout=aiohttp.ClientTimeout(total=10),
    )


# One resubscribe cycle: refresh the real time consumption status, then refresh info.
RESUBSCRIBE_HTTP_CALLS = [_gql_call(REAL_TIME_CONSUMPTION_ENABLED % HOME_ID), _gql_call(INFO)]


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
    tibber_connection: tibber.Tibber,
    enabled: bool,
) -> None:
    """Updating the flag must not require info to be populated first."""
    # A freshly constructed home has a never-read status and no info.
    home = tibber.TibberHome(HOME_ID, tibber_connection)
    assert home.info == {}

    mock_websession.post = _make_status_response(rt_enabled=enabled)

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


@pytest.mark.parametrize("real_time_consumption", [False, True])
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
    # The initial subscribe and the resubscribe after the error each run one full cycle. A single
    # False reading only arms the grace-period timer, so the home keeps reporting its last known
    # True and the call sequence is identical whether the resubscribe status query reports True
    # or False.
    expected_calls = RESUBSCRIBE_HTTP_CALLS * 2
    assert mock_websession.post.call_count == len(expected_calls)
    assert mock_websession.post.call_args_list == expected_calls
    assert home.rt_subscription_running is True

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
    """A malformed payload carries no usable information and must not change the last known status."""
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value={"data": payload})
    mock_websession.post = AsyncMock(return_value=mock_response)

    await home.update_real_time_consumption_enabled()

    # The `home` fixture starts out with a known True status, which a malformed payload
    # must not overwrite.
    assert home.has_real_time_consumption is True


@pytest.mark.parametrize(
    "payload",
    [
        {"viewer": {"home": {}}},  # missing "features" -> KeyError
        {"viewer": None},  # not subscriptable -> TypeError
    ],
)
async def test_update_real_time_consumption_enabled_malformed_payload_stays_unknown(
    tibber_connection: tibber.Tibber,
    mock_websession: MagicMock,
    payload: dict,
) -> None:
    """A malformed payload must leave a never-read status as unknown (None)."""
    fresh_home = tibber.TibberHome(HOME_ID, tibber_connection)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.json = AsyncMock(return_value={"data": payload})
    mock_websession.post = AsyncMock(return_value=mock_response)

    await fresh_home.update_real_time_consumption_enabled()

    assert fresh_home.has_real_time_consumption is None


async def test_status_false_without_prior_true_sets_false_immediately(
    tibber_connection: tibber.Tibber,
    mock_websession: MagicMock,
) -> None:
    """A False status with no known True resolves to False directly, with no grace period."""
    fresh_home = tibber.TibberHome(HOME_ID, tibber_connection)
    mock_websession.post = _make_status_response(rt_enabled=False)

    await fresh_home.update_real_time_consumption_enabled()

    assert fresh_home.has_real_time_consumption is False


async def test_status_single_false_keeps_true_within_grace(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """A single False status following a known True must keep reporting the last known True."""
    mock_websession.post = _make_status_response(rt_enabled=False)

    await home.update_real_time_consumption_enabled()

    assert home.has_real_time_consumption is True


async def test_status_repeated_false_within_grace_stays_true(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """Repeated False statuses inside the grace period must keep reporting the last known True."""
    mock_websession.post = _make_status_response(rt_enabled=False)

    for _ in range(5):
        await home.update_real_time_consumption_enabled()

    assert home.has_real_time_consumption is True


async def test_status_flips_false_only_strictly_after_grace(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """The status must flip to False only once the grace period has been strictly exceeded."""
    mock_websession.post = _make_status_response(rt_enabled=False)
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is True

        # Exactly at the boundary the grace period has not been exceeded yet.
        frozen_clock.current = armed + REAL_TIME_CONSUMPTION_DISABLED_GRACE
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is True

        frozen_clock.current = armed + REAL_TIME_CONSUMPTION_DISABLED_GRACE + dt.timedelta(seconds=1)
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is False


async def test_status_true_reading_restarts_grace_period(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A True status must clear the grace-period timer so a later, unrelated False starts fresh.

    Regression test: a False -> True -> False sequence must not jump straight to a confirmed
    disable just because a timer from an earlier, already-recovered-from False was never cleared.
    """
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    past_grace = REAL_TIME_CONSUMPTION_DISABLED_GRACE + dt.timedelta(seconds=1)

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        mock_websession.post = _make_status_response(rt_enabled=False)
        await home.update_real_time_consumption_enabled()

        mock_websession.post = _make_status_response(rt_enabled=True)
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is True

        # Long after the original timer would have expired, a brand new False must get a full
        # fresh grace period rather than flipping on the stale timer.
        mock_websession.post = _make_status_response(rt_enabled=False)
        frozen_clock.current = armed + past_grace
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is True

        frozen_clock.current = armed + past_grace + past_grace
        await home.update_real_time_consumption_enabled()
        assert home.has_real_time_consumption is False


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
    mock_websession.post = _make_status_response(rt_enabled=True)

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
    mock_websession.post = _make_status_response(rt_enabled=True)

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


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_rt_resubscribe_confirmed_disable_notifies_on_error(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A confirmed disable (False sustained past the grace period) must notify on_error and stop.

    Only a status that has been False for the whole grace period may end the resubscribe loop,
    and it must do so visibly. Repeated subscribe failures drive one status query per resubscribe
    cycle: the first False arms the grace-period timer, and the clock is advanced past the grace
    window before the second False so it confirms the disable.
    """
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    status_calls = 0

    async def post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ANN401, ARG001
        nonlocal status_calls
        if kwargs["data"]["query"] != REAL_TIME_CONSUMPTION_ENABLED % HOME_ID:
            # Info refresh: a benign payload that carries no status, so it stays a no-op reading.
            return _json_response(
                {"data": {"viewer": {"name": "n", "userId": "u", "homes": [], "websocketSubscriptionUrl": None}}},
            )
        status_calls += 1
        if status_calls == 1:
            # Initial subscription reports real time consumption enabled.
            return _json_response(_status_payload(rt_enabled=True))
        if status_calls == 2:
            # First False: arms the grace-period timer at the armed time, status stays True.
            return _json_response(_status_payload(rt_enabled=False))
        # Second False, now past the grace window: confirms the disable.
        frozen_clock.current = armed + REAL_TIME_CONSUMPTION_DISABLED_GRACE + dt.timedelta(seconds=1)
        return _json_response(_status_payload(rt_enabled=False))

    mock_websession.post = AsyncMock(side_effect=post_side_effect)

    async def subscribe_raises(*args: Any, **kwargs: Any) -> AsyncGenerator:  # noqa: ANN401, ARG001
        raise WebsocketTransportError("transport error")
        yield

    mock_realtime.subscribe = subscribe_raises

    disabled_error_seen = asyncio.Event()
    caught: list[Exception] = []

    def on_error(exc: Exception) -> None:
        caught.append(exc)
        if isinstance(exc, RealTimeConsumptionDisabledError):
            disabled_error_seen.set()

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        await home.rt_subscribe(MagicMock(), on_error=on_error)
        await asyncio.wait_for(disabled_error_seen.wait(), timeout=1.0)

    assert home.has_real_time_consumption is False
    assert isinstance(caught[-1], RealTimeConsumptionDisabledError)
    assert not home.rt_subscription_running

    home.rt_unsubscribe()


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_rt_resubscribe_recovers_from_degraded_status_payload(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
) -> None:
    """A degraded status-query payload during resubscribe must not disable a running home.

    If a status query comes back with a well-formed 200 response whose payload carries
    no usable `realTimeConsumptionEnabled` value, that must
    be a no-op, so the resubscribe still refreshes info and brings the subscription back up.
    """
    call_count = 0

    async def post_side_effect(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ARG001, ANN401
        nonlocal call_count
        call_count += 1
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.content_type = "application/json"
        if call_count == 1:
            # Initial status query: real time consumption enabled.
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
        elif call_count == 3:
            # Resubscription status query: degraded response, the home cannot be resolved.
            mock_response.json = AsyncMock(return_value={"data": {"viewer": {"home": None}}})
        else:
            mock_response.json = AsyncMock(
                return_value={
                    "data": {
                        "viewer": {
                            "name": "n",
                            "userId": "u",
                            "homes": [],
                            "websocketSubscriptionUrl": None,
                        },
                    },
                },
            )
        return mock_response

    mock_websession.post = AsyncMock(side_effect=post_side_effect)

    subscribe_call_count = 0
    subscribed_again = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        nonlocal subscribe_call_count
        subscribe_call_count += 1
        if subscribe_call_count == 1:
            raise WebsocketTransportError("transport error")
            yield
        subscribed_again.set()
        yield {"liveMeasurement": {}}
        await asyncio.Event().wait()

    mock_realtime.subscribe = subscribe

    await home.rt_subscribe(MagicMock(), on_error=lambda exc: None)  # noqa: ARG005
    await asyncio.wait_for(subscribed_again.wait(), timeout=1.0)

    assert home.has_real_time_consumption is True
    assert mock_websession.post.call_count == 4
    assert home.rt_subscription_running

    home.rt_unsubscribe()


def _info_payload(rt_enabled: bool | None, *, with_features: bool = True) -> dict[str, Any]:
    """Return an UPDATE_INFO payload reporting the given real time consumption status."""
    home_payload: dict[str, Any] = {
        "id": HOME_ID,
        "currentSubscription": {"status": "running"},
    }
    if with_features:
        home_payload["features"] = {"realTimeConsumptionEnabled": rt_enabled}
    return {"data": {"viewer": {"home": home_payload}}}


def _make_status_query_router(rt_enabled: bool) -> AsyncMock:
    """Return a post mock reporting the given status, and a benign payload for every other query."""

    def respond(*args: Any, **kwargs: Any) -> MagicMock:  # noqa: ANN401, ARG001
        if kwargs["data"]["query"] == REAL_TIME_CONSUMPTION_ENABLED % HOME_ID:
            return _json_response(_status_payload(rt_enabled))
        return _json_response(
            {
                "data": {
                    "viewer": {
                        "name": "n",
                        "userId": "u",
                        "homes": [],
                        "websocketSubscriptionUrl": None,
                    },
                },
            },
        )

    return AsyncMock(side_effect=respond)


@pytest.mark.parametrize("callback_raises", [False, True])
@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_live_data_restarts_grace_period(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
    callback_raises: bool,
) -> None:
    """Live real time data must clear the suspected-disable timer.

    Regression test: a False status arms the grace-period timer, the subscription then runs
    healthily for hours, and a later brand new False status must get a full fresh grace period
    instead of flipping the status immediately because of the hours-old timer.

    Parametrized over a callback that raises, because the timer must be cleared before the
    consumer callback runs: a consumer whose callback always throws must not accumulate a stale
    suspicion while data is flowing.
    """
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    # Every real time consumption status query reports disabled.
    mock_websession.post = _make_status_query_router(rt_enabled=False)

    subscribe_calls = 0
    measurement_delivered = asyncio.Event()
    resubscribed = asyncio.Event()
    release = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        nonlocal subscribe_calls
        subscribe_calls += 1
        if subscribe_calls == 1:
            yield {"liveMeasurement": {}}
            # Stay up until the test simulates a transient transport error hours later.
            await release.wait()
            raise WebsocketTransportError("transport error")
        resubscribed.set()
        yield {"liveMeasurement": {}}
        await asyncio.Event().wait()

    mock_realtime.subscribe = subscribe

    disabled_seen = asyncio.Event()

    def on_error(exc: Exception) -> None:
        if isinstance(exc, RealTimeConsumptionDisabledError):
            disabled_seen.set()

    def callback(_data: dict) -> None:
        measurement_delivered.set()
        if callback_raises:
            raise ValueError("callback boom")

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        await home.rt_subscribe(callback, on_error=on_error)
        # The status query reported disabled, which only arms the timer, so the subscription
        # still comes up and the first measurement clears the suspicion again.
        await asyncio.wait_for(measurement_delivered.wait(), timeout=1.0)
        assert home.has_real_time_consumption is True

        # Two hours of healthy subscription pass, then a transient transport error resubscribes.
        frozen_clock.current = armed + dt.timedelta(hours=2)
        release.set()
        pending = [
            asyncio.create_task(resubscribed.wait()),
            asyncio.create_task(disabled_seen.wait()),
        ]
        _, still_pending = await asyncio.wait(
            pending,
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in still_pending:
            task.cancel()

    assert not disabled_seen.is_set(), "a brand new False must not confirm a disable on a stale timer"
    assert home.has_real_time_consumption is True
    assert resubscribed.is_set()
    assert home.rt_subscription_running

    home.rt_unsubscribe()


async def test_update_info_false_status_keeps_true_within_grace(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """A False status from the info query must only arm the timer."""
    mock_websession.post = AsyncMock(return_value=_json_response(_info_payload(rt_enabled=False)))

    await home.update_info()

    assert home.has_real_time_consumption is True


async def test_update_info_malformed_status_is_a_no_op(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """An info payload without a usable status must not change the last known status."""
    mock_websession.post = AsyncMock(
        return_value=_json_response(_info_payload(None, with_features=False)),
    )

    await home.update_info()

    assert home.has_real_time_consumption is True


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_live_data_restarts_grace_period_armed_by_info_poll(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A timer armed by a periodic info poll must also be cleared by live data.

    This is the shape a consumer normally hits in production: it polls home info on a
    timer while the real time subscription runs, so a transient False can arm the grace-period
    timer without any resubscribe happening at all.
    """
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    mock_websession.post = _make_status_query_router(rt_enabled=True)

    measurements: asyncio.Queue[Any] = asyncio.Queue()
    gate = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        yield {"liveMeasurement": {}}
        await gate.wait()
        yield {"liveMeasurement": {}}
        await asyncio.Event().wait()

    mock_realtime.subscribe = subscribe

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        await home.rt_subscribe(measurements.put_nowait)
        await asyncio.wait_for(measurements.get(), timeout=1.0)

        # An info poll reports real time consumption disabled, arming the grace-period timer.
        mock_websession.post = AsyncMock(return_value=_json_response(_info_payload(rt_enabled=False)))
        await home.update_info()
        assert home.has_real_time_consumption is True

        # The subscription is still healthy and delivers another measurement, clearing the timer.
        gate.set()
        await asyncio.wait_for(measurements.get(), timeout=1.0)

        # Hours later another transient False must start a fresh grace period, not flip.
        frozen_clock.current = armed + dt.timedelta(hours=2)
        await home.update_info()

    assert home.has_real_time_consumption is True

    home.rt_unsubscribe()


@patch("tibber.home.RESUBSCRIBE_WAIT_TIME", 0)
async def test_live_data_recovers_confirmed_false_status(
    home: tibber.TibberHome,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A live measurement must recover a status that has already flipped to False.

    A live measurement is proof that real time consumption works right now, so it counts as a
    successful True status reading, not merely a timer clear. Once a sustained False from periodic
    info polls has confirmed a disable while a listener is still attached, resumed live
    data must restore the believed status to True so the next resubscribe does not stop.
    """
    armed = dt.datetime(2026, 5, 6, 0, 0, 0, tzinfo=dt.UTC)
    mock_websession.post = _make_status_query_router(rt_enabled=True)

    measurements: asyncio.Queue[Any] = asyncio.Queue()
    gate = asyncio.Event()

    async def subscribe(*args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:  # noqa: ANN401, ARG001
        yield {"liveMeasurement": {}}
        await gate.wait()
        yield {"liveMeasurement": {}}
        await asyncio.Event().wait()

    mock_realtime.subscribe = subscribe

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = armed
        await home.rt_subscribe(measurements.put_nowait)
        await asyncio.wait_for(measurements.get(), timeout=1.0)
        assert home.has_real_time_consumption is True

        # An info poll reports disabled, arming the grace-period timer without flipping the status.
        mock_websession.post = AsyncMock(return_value=_json_response(_info_payload(rt_enabled=False)))
        await home.update_info()
        assert home.has_real_time_consumption is True

        # A second False poll past the grace window, with no live data in between, confirms the disable.
        frozen_clock.current = armed + REAL_TIME_CONSUMPTION_DISABLED_GRACE + dt.timedelta(seconds=1)
        await home.update_info()
        assert home.has_real_time_consumption is False

        # Live data resumes and proves real time consumption is working, restoring the status.
        gate.set()
        await asyncio.wait_for(measurements.get(), timeout=1.0)

    assert home.has_real_time_consumption is True

    home.rt_unsubscribe()


async def test_rt_resubscribe_confirmed_disable_without_on_error_logs_and_stops(
    tibber_connection: tibber.Tibber,
    mock_realtime: MagicMock,
    mock_websession: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A confirmed disable must stop the resubscribe loop without raising when no on_error is set."""
    # A never-read status resolves a False directly, with no grace period.
    fresh_home = tibber.TibberHome(HOME_ID, tibber_connection)
    mock_websession.post = _make_status_response(rt_enabled=False)

    with caplog.at_level(logging.INFO):
        await fresh_home.rt_subscribe(MagicMock())

    assert fresh_home.has_real_time_consumption is False
    mock_realtime.connect.assert_not_awaited()
    assert not fresh_home.rt_subscription_running
    assert f"Home {HOME_ID} does not have real time consumption enabled" in caplog.text


def _price_item(time: str, price: float | None, **details: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return a Data API price item."""
    return {"time": time, "consumption": {"price": price, **details}}


def _interval(time: str, price: float) -> PriceInterval:
    return PriceInterval(start=dt.datetime.fromisoformat(time), consumption=PriceDetails(price=price))


def _quarters(start: str, count: int) -> list[PriceInterval]:
    """Return `count` consecutive 15-minute intervals from `start`, priced by their position."""
    first = dt.datetime.fromisoformat(start)
    return [_interval((first + dt.timedelta(minutes=15 * index)).isoformat(), float(index)) for index in range(count)]


async def test_update_prices_fetches_today_and_tomorrow(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    mock_websession: MagicMock,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Prices are fetched from the start of today, in the home's time zone, and parsed."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    mock_websession.request = AsyncMock(
        return_value=_json_response(
            {
                "currency": "EUR",
                "items": [
                    {
                        "time": "2026-05-06T00:00:00+02:00",
                        "consumption": {"price": 0.45, "subsidizedPrice": 0.0, "vatPercent": 25},
                        "production": {"price": -0.1, "thirdPartyPrice": 0.02},
                    },
                    _price_item("2026-05-06T00:15:00+02:00", 0.5),
                    _price_item("2026-05-08T00:00:00+02:00", 9.0),
                ],
                "next": None,
            },
        ),
    )

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    assert mock_websession.request.await_args.kwargs["params"] == {"since": "2026-05-05T22:00:00Z"}
    assert home.prices == [
        PriceInterval(
            start=dt.datetime.fromisoformat("2026-05-06T00:00:00+02:00"),
            consumption=PriceDetails(price=0.45, subsidized_price=0.0, vat_percent=25.0),
            production=PriceDetails(price=-0.1, third_party_price=0.02),
        ),
        _interval("2026-05-06T00:15:00+02:00", 0.5),
    ]
    assert home.currency == "EUR"
    assert home.price_unit == "EUR/kWh"


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientError("Network error"),
        InvalidLoginError(401, "Invalid token"),
        RateLimitExceededError(429, "Rate limited", "RATE_LIMITED", 1.0),
    ],
)
async def test_update_prices_raises_and_keeps_prices(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A failed price fetch must reach the caller, so it can retry, and keep the known prices."""
    known_prices = [_interval("2026-11-24T00:00:00+01:00", 0.25)]
    home.prices = known_prices
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))

    with pytest.raises(type(error)):
        await home.update_prices()

    assert home.prices == known_prices


async def test_update_prices_only_fetches_the_unknown_prices(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Published prices never change, so a later update asks for what follows the known ones."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    today = _quarters("2026-05-06T00:00:00+02:00", 4)
    yesterday = [_interval("2026-05-05T23:45:00+02:00", 9.0)]
    home.prices = yesterday + today
    new = [_interval("2026-05-06T01:00:00+02:00", 5.0)]
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=new))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    since = dt.datetime(2026, 5, 5, 23, 0, tzinfo=dt.UTC)
    end = dt.datetime(2026, 5, 8, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    get_prices.assert_awaited_once_with(HOME_ID, since, end)
    assert home.prices == today + new


async def test_update_prices_fetches_nothing_when_the_prices_are_complete(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """With today's and tomorrow's prices known there is nothing to fetch, but yesterday's prices go."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    complete = _quarters("2026-05-06T00:00:00+02:00", 192)
    home.prices = [_interval("2026-05-05T23:45:00+02:00", 9.0), *complete]
    get_prices = AsyncMock()
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    get_prices.assert_not_awaited()
    assert home.prices == complete


async def test_update_prices_fetches_a_hole_in_the_known_prices_again(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """A price that was missed once is fetched again, together with what follows it."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    known = _quarters("2026-05-06T00:00:00+02:00", 3)
    after_hole = [_interval("2026-05-06T01:00:00+02:00", 7.0)]
    home.prices = known + after_hole
    new = [_interval("2026-05-06T00:45:00+02:00", 3.0), *after_hole]
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=new))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        await home.update_prices()

    assert get_prices.await_args.args[1] == dt.datetime(2026, 5, 5, 22, 45, tzinfo=dt.UTC)
    assert home.prices == known + new


async def test_update_prices_counts_the_intervals_of_a_summer_time_change(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    frozen_clock: type[FixedDateTime],
) -> None:
    """The day that summer time ends has 100 intervals, and the next one to fetch comes after them."""
    try:
        tibber_connection.time_zone = ZoneInfo("Europe/Oslo")
    except ZoneInfoNotFoundError:
        pytest.skip("No time zone database")
    day = _quarters("2026-10-24T22:00:00+00:00", 100)
    home.prices = day
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=[]))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)

    with patch("tibber.home.dt.datetime", frozen_clock):
        frozen_clock.current = dt.datetime(2026, 10, 25, 12, 0, tzinfo=dt.UTC)
        await home.update_prices()

    assert get_prices.await_args.args[1] == dt.datetime(2026, 10, 25, 23, 0, tzinfo=dt.UTC)
    assert home.prices == day


async def test_update_prices_clears_prices_when_not_available(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A home without prices gets no prices instead of an error."""
    home.prices = [_interval("2026-11-24T00:00:00+01:00", 0.25)]
    error = PricesNotAvailableError(403, "No prices", "prices-not-available")
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))

    await home.update_prices()

    assert home.prices == []


async def test_get_prices_does_not_change_prices(
    home: tibber.TibberHome,
    mock_websession: MagicMock,
) -> None:
    """Fetching a range of prices returns them without replacing the cached prices."""
    mock_websession.request = AsyncMock(
        return_value=_json_response({"items": [_price_item("2026-01-01T00:00:00+00:00", 1.0)], "next": None}),
    )
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    assert await home.get_prices(start, start + dt.timedelta(days=1)) == [
        _interval("2026-01-01T00:00:00+00:00", 1.0),
    ]
    assert home.prices == []


async def test_current_price_data(
    home: tibber.TibberHome,
    frozen_clock: type[FixedDateTime],
) -> None:
    """The current price and its rank among today's prices, where equal prices share a rank."""
    home.prices = [
        _interval(f"2026-05-06T{time}:00+00:00", price)
        for time, price in {
            "00:00": 0.0,
            "02:00": 1.0,
            "02:15": 3.0,
            "02:30": 2.0,
            "02:45": 2.0,
            "03:00": 4.0,
        }.items()
    ] + [_interval("2026-05-07T02:30:00+00:00", 0.1)]

    with patch("tibber.home.dt.datetime", frozen_clock):
        price, price_time, price_rank = home.current_price_data()
        current = home.current_price_interval

    assert current == home.prices[3]
    assert price == 2.0
    assert price_time == dt.datetime(2026, 5, 6, 2, 30, tzinfo=dt.UTC)
    assert price_rank == 3 / 6


async def test_current_price_data_without_current_interval(home: tibber.TibberHome) -> None:
    """No current interval gives no price."""
    home.prices = [_interval("2020-01-01T00:00:00+00:00", 1.0)]

    assert home.current_price_interval is None
    assert home.current_price_data() == (None, None, None)


async def test_current_attributes(
    home: tibber.TibberHome,
    frozen_clock: type[FixedDateTime],
) -> None:
    """Today's statistics, also for negative prices, and only for today."""
    home.prices = [
        _interval("2026-05-06T01:00:00+00:00", -2.0),
        _interval("2026-05-06T09:00:00+00:00", -1.0),
        _interval("2026-05-06T21:00:00+00:00", -0.5),
        _interval("2026-05-07T09:00:00+00:00", 5.0),
    ]

    with patch("tibber.home.dt.datetime", frozen_clock):
        attributes = home.current_attributes()

    assert attributes == {
        "max_price": -0.5,
        "avg_price": round(-3.5 / 3, 4),
        "min_price": -2.0,
        "off_peak_1": -2.0,
        "peak": -1.0,
        "off_peak_2": -0.5,
    }


async def test_current_attributes_without_prices(home: tibber.TibberHome) -> None:
    """No prices today gives nothing, as zeros would look like real prices."""
    assert home.current_attributes() == {}

    home.prices = [PriceInterval(start=dt.datetime(2026, 5, 6, tzinfo=dt.UTC), production=PriceDetails(price=1.0))]
    assert home.current_attributes() == {}


@pytest.mark.parametrize(
    ("resolution", "prices", "expected"),
    [
        (
            "HOURLY",
            [
                ("2026-05-05T22:00:00Z", 1.0),
                ("2026-05-05T22:15:00Z", 2.0),
                ("2026-05-05T22:30:00Z", 3.0),
                ("2026-05-05T22:45:00Z", 4.0),
                ("2026-05-05T23:00:00Z", 6.0),
            ],
            [("2026-05-06T00:00:00+02:00", 2.5), ("2026-05-06T01:00:00+02:00", 6.0)],
        ),
        (
            "daily",
            [("2026-05-05T21:45:00Z", 1.0), ("2026-05-05T22:00:00Z", 3.0), ("2026-05-05T22:15:00Z", 5.0)],
            [("2026-05-05T00:00:00+02:00", 1.0), ("2026-05-06T00:00:00+02:00", 4.0)],
        ),
        (
            "MONTHLY",
            [("2026-04-30T21:45:00Z", 1.0), ("2026-04-30T22:00:00Z", 3.0), ("2026-05-01T22:00:00Z", 5.0)],
            [("2026-04-01T00:00:00+02:00", 1.0), ("2026-05-01T00:00:00+02:00", 4.0)],
        ),
    ],
)
async def test_get_historic_price_data_averages_the_prices_of_each_period(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolution: str,
    prices: list[tuple[str, float]],
    expected: list[tuple[str, float]],
) -> None:
    """The 15-minute prices are averaged per period of the home's time zone, and a period needs a price."""
    tibber_connection.time_zone = dt.timezone(dt.timedelta(hours=2))
    intervals = [_interval(time, price) for time, price in prices]
    # A production price alone isn't a price to pay, so it doesn't make a period of its own.
    intervals.append(PriceInterval(start=dt.datetime(2026, 7, 1, tzinfo=dt.UTC), production=PriceDetails(price=9.0)))
    get_prices = AsyncMock(return_value=Prices(currency="NOK", intervals=intervals))
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    start = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)
    end = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)

    entries = await home.get_historic_price_data(start, end, resolution)

    get_prices.assert_awaited_once_with(HOME_ID, start, end)
    assert entries == [{"time": time, "total": total} for time, total in expected]
    assert home.prices == []


async def test_get_historic_price_data_tells_apart_the_hours_of_a_summer_time_change(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hour that summer time ends is there twice, and must not become one period."""
    try:
        tibber_connection.time_zone = ZoneInfo("Europe/Oslo")
    except ZoneInfoNotFoundError:
        pytest.skip("No time zone database")
    intervals = [_interval("2026-10-25T00:00:00+00:00", 1.0), _interval("2026-10-25T01:00:00+00:00", 3.0)]
    monkeypatch.setattr(
        tibber_connection.data_api,
        "get_prices",
        AsyncMock(return_value=Prices(currency="NOK", intervals=intervals)),
    )
    start = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)

    entries = await home.get_historic_price_data(start, start + dt.timedelta(days=1))

    assert entries == [
        {"time": "2026-10-25T02:00:00+02:00", "total": 1.0},
        {"time": "2026-10-25T02:00:00+01:00", "total": 3.0},
    ]


async def test_get_historic_price_data_rejects_unsupported_resolutions(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Data API has no weekly or annual prices, so they aren't made up from the 15-minute ones."""
    get_prices = AsyncMock()
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", get_prices)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    with pytest.raises(ValueError, match="Unsupported resolution WEEKLY"):
        await home.get_historic_price_data(start, start + dt.timedelta(days=30), "WEEKLY")

    get_prices.assert_not_awaited()


async def test_get_historic_price_data_raises_when_prices_are_not_available(
    home: tibber.TibberHome,
    tibber_connection: tibber.Tibber,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A range before the price history of the home is an error, not an empty list."""
    error = PricesNotAvailableError(403, "Before the history", "prices-not-available")
    monkeypatch.setattr(tibber_connection.data_api, "get_prices", AsyncMock(side_effect=error))
    start = dt.datetime(2000, 1, 1, tzinfo=dt.UTC)

    with pytest.raises(PricesNotAvailableError):
        await home.get_historic_price_data(start, start + dt.timedelta(days=1))


@pytest.mark.parametrize(("country", "currency"), [("SE", "SEK"), ("NO", "NOK"), ("DE", "EUR"), ("XX", "")])
async def test_currency_falls_back_to_country(home: tibber.TibberHome, country: str, currency: str) -> None:
    """The currency is known from the home's country before any prices are fetched."""
    home.info = {"viewer": {"home": {"address": {"country": country}}}}

    assert home.currency == currency
