"""Tests for TokenManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from tibber.token_manager import TokenManager


async def test_access_token_property_returns_stored_token() -> None:
    """access_token must return the token set at construction."""
    manager = TokenManager("initial_token")
    assert manager.access_token == "initial_token"


async def test_set_access_token_updates_stored_token() -> None:
    """set_access_token must synchronously update the stored token."""
    manager = TokenManager("old")
    manager.set_access_token("new")
    assert manager.access_token == "new"


async def test_async_get_access_token_without_callback_returns_stored_token() -> None:
    """Without a refresh callback, async_get_access_token returns the stored token immediately."""
    manager = TokenManager("static_token")
    token = await manager.async_get_access_token()
    assert token == "static_token"


async def test_async_get_access_token_invokes_callback_and_stores_result() -> None:
    """When a callback is provided, the result is stored and returned."""
    callback = AsyncMock(return_value="fresh_token")
    manager = TokenManager("old_token", refresh_access_token=callback)

    token = await manager.async_get_access_token()

    assert token == "fresh_token"
    assert manager.access_token == "fresh_token"
    callback.assert_awaited_once()


async def test_async_get_access_token_callback_returning_none_keeps_old_token() -> None:
    """A callback that returns None must leave the stored token unchanged."""
    callback = AsyncMock(return_value=None)
    manager = TokenManager("old_token", refresh_access_token=callback)

    token = await manager.async_get_access_token()

    assert token == "old_token"
    assert manager.access_token == "old_token"


async def test_concurrent_calls_coalesce_into_single_callback_invocation() -> None:
    """Concurrent async_get_access_token() calls must invoke the callback at most once."""
    call_count = 0
    release = asyncio.Event()

    async def slow_callback() -> str:
        nonlocal call_count
        call_count += 1
        await release.wait()  # block until all callers have stacked up
        return "refreshed"

    manager = TokenManager("old", refresh_access_token=slow_callback)

    # Launch several concurrent callers before releasing the callback.
    tasks = [asyncio.create_task(manager.async_get_access_token()) for _ in range(5)]
    await asyncio.sleep(0)  # let all tasks reach the shield/await point
    release.set()

    results = await asyncio.gather(*tasks)

    assert call_count == 1, f"Expected 1 callback invocation, got {call_count}"
    assert all(r == "refreshed" for r in results)


async def test_callback_exception_is_caught_and_last_token_returned() -> None:
    """An exception raised by the refresh callback must be swallowed and the last token returned.

    The callback is defined outside this library, so we cannot control what it does.
    Propagating its exceptions into callers would break requests unnecessarily.
    """
    callback = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    manager = TokenManager("last_known", refresh_access_token=callback)

    token = await manager.async_get_access_token()

    assert token == "last_known"
    assert manager.access_token == "last_known"


async def test_cancelled_caller_does_not_cancel_shared_refresh() -> None:
    """Cancelling one waiter must not abort the in-flight callback for other waiters."""
    release = asyncio.Event()

    async def slow_callback() -> str:
        await release.wait()
        return "done"

    manager = TokenManager("old", refresh_access_token=slow_callback)

    task_a = asyncio.create_task(manager.async_get_access_token())
    task_b = asyncio.create_task(manager.async_get_access_token())

    await asyncio.sleep(0)  # let both tasks start

    task_a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_a

    release.set()
    result = await task_b

    assert result == "done"
    assert manager.access_token == "done"
