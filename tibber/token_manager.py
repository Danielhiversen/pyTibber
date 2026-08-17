"""Token manager for Tibber API clients."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


class TokenManager:
    """Manages the access token and optional refresh callback shared across all Tibber API clients.

    All consumers (``Tibber.execute``, ``TibberDataAPI``, and ``TibberWebsocketsTransport``)
    share one instance so the token has a single source of truth.  When a refresh callback is
    provided the token is fetched fresh before every request; concurrent callers coalesce into
    a single in-flight callback invocation.
    """

    def __init__(
        self,
        access_token: str,
        *,
        refresh_access_token: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        """Initialize the token manager.

        :param access_token: The initial access token.
        :param refresh_access_token: Optional async callback that returns a refreshed access
            token.  Expected to be cheap when the token is still valid (e.g. an OAuth2 session
            that returns the cached token without a network call).
        """
        self._access_token = access_token
        self._refresh_access_token = refresh_access_token
        self._refresh_task: asyncio.Task[str] | None = None

    @property
    def access_token(self) -> str:
        """Return the last known access token without refreshing (sync, no I/O)."""
        return self._access_token

    def set_access_token(self, access_token: str) -> None:
        """Update the stored access token synchronously."""
        self._access_token = access_token

    async def async_get_access_token(self) -> str:
        """Return the access token, invoking the refresh callback when configured.

        Concurrent callers coalesce: if a refresh is already in flight, new callers join it
        rather than starting their own callback invocation.  A cancelled caller does not cancel
        the shared in-flight refresh (``asyncio.shield``).

        Sequential callers always start a fresh callback invocation — the prior task is
        ``.done()`` by the time the next awaited call arrives.
        """
        if self._refresh_access_token is None:
            return self._access_token
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._do_refresh())
        return await asyncio.shield(self._refresh_task)

    async def _do_refresh(self) -> str:
        """Invoke the refresh callback and persist the returned token.

        Any exception raised by the callback is caught and logged; the last known token is
        returned so callers can still attempt a request (which may then fail with a 401 and
        trigger a retry).
        """
        if TYPE_CHECKING:
            assert self._refresh_access_token is not None
        try:
            token = await self._refresh_access_token()
        except Exception:
            _LOGGER.exception("Error in refresh_access_token callback, keeping last known token")
            return self._access_token
        if token is not None:
            _LOGGER.debug("Access token refreshed")
            self._access_token = token
        return self._access_token
