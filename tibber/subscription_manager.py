"""Subscription manager for Graph QL websocket."""

import asyncio
import json
import logging
from time import time

import websockets

_LOGGER = logging.getLogger(__name__)

STATE_STARTING = 'starting'
STATE_RUNNING = 'running'
STATE_STOPPED = 'stopped'


class SubscriptionManager:
    """Subscription manager."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, loop, init_payload, url):
        """Create resources for websocket communication."""
        self.loop = loop
        self.subscriptions = {}
        self._url = url
        self._state = None
        self.websocket = None
        self._retry_timer = None
        self._client_task = None
        self._wait_time_before_retry = 15
        self._session_id = 0
        self._init_payload = init_payload

    def start(self):
        """Start websocket."""
        if self._state == STATE_RUNNING:
            return
        self._state = STATE_STARTING
        self._cancel_client_task()
        self._client_task = self.loop.create_task(self.running())
        for subscription_id in range(len(self.subscriptions)):
            callback, sub_query = self.subscriptions.get(subscription_id, (None, None))
            if callback is None:
                continue
            self.loop.create_task(self.subscribe(sub_query, callback, subscription_id))

    @property
    def is_running(self):
        """Return if client is running or not."""
        return self._state == STATE_RUNNING

    async def running(self):
        """Start websocket connection."""
        await self._close_websocket()
        try:
            self.websocket = await websockets.connect(self._url,
                                                      subprotocols=["graphql-subscriptions"])
            self._state = STATE_RUNNING
            self.websocket.send(json.dumps({"type": "init",
                                            "payload": self._init_payload}))

            while self._state != STATE_STOPPED:
                try:
                    msg = await asyncio.wait_for(self.websocket.recv(), timeout=20)
                except asyncio.TimeoutError:
                    _LOGGER.error("No data in 20 seconds, checking the connection.")
                    try:
                        pong_waiter = await self.websocket.ping()
                        await asyncio.wait_for(pong_waiter, timeout=10)
                    except asyncio.TimeoutError:
                        _LOGGER.debug("No response to ping in 10 seconds, reconnecting.")
                        break
                await self._process_msg(msg)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.error('Unexpected error', exc_info=True)
        finally:
            await self._close_websocket()
            if self._state != STATE_STOPPED:
                self._state = STATE_STOPPED
                _LOGGER.warning("Reconnecting")
                self.retry()
            else:
                self._state = STATE_STOPPED

    async def stop(self, timeout=10):
        """Close websocket connection."""
        _LOGGER.debug("Stopping client.")
        start_time = time()
        self._cancel_retry_timer()

        for subscription_id in range(len(self.subscriptions)):
            _LOGGER.debug("Sending unsubscribe: %s", subscription_id)
            await self.unsubscribe(subscription_id)

        while (timeout > 0 and
               self.websocket is not None
               and not self.subscriptions
               and (time() - start_time) < timeout/2):
            await asyncio.sleep(0.1, loop=self.loop)

        self._state = STATE_STOPPED
        while (timeout > 0 and
               self.websocket is not None and
               not self.websocket.closed and
               (time() - start_time) < timeout):
            await asyncio.sleep(0.1, loop=self.loop)

        await self._close_websocket()
        self._cancel_client_task()
        _LOGGER.debug("Server connection is stopped")

    def retry(self):
        """Retry to connect to websocket."""
        if self._state in [STATE_STARTING, STATE_RUNNING]:
            return
        self._cancel_retry_timer()
        self._cancel_client_task()
        self._state = STATE_STARTING
        self._retry_timer = self.loop.call_later(self._wait_time_before_retry, self.start)
        _LOGGER.debug('Reconnecting to server in %i seconds.', self._wait_time_before_retry)

    async def subscribe(self, sub_query, callback, current_session_id=None):
        """Add a new subscription."""
        if current_session_id is None:
            current_session_id = self._session_id
            self._session_id += 1
        if self.websocket is None or not self.websocket.open:
            await asyncio.sleep(1, loop=self.loop)
            return await self.subscribe(sub_query, callback, current_session_id)

        subscription = {"query": sub_query,
                        "type": "subscription_start", "id": current_session_id}
        json_subscription = json.dumps(subscription)
        await self.websocket.send(json_subscription)
        self.subscriptions[current_session_id] = (callback, sub_query)
        return current_session_id

    async def unsubscribe(self, subscription_id):
        """Unsubscribe."""
        if self.websocket is None or not self.websocket.open:
            _LOGGER.warning("Websocket is closed.")
            return
        await self.websocket.send(json.dumps({"id": subscription_id,
                                              "type": "subscription_end"}))

    async def _close_websocket(self):
        if self.websocket is None:
            return
        try:
            await self.websocket.close()
        finally:
            self.websocket = None

    async def _process_msg(self, msg):
        """Process received msg."""
        result = json.loads(msg)

        subscription_id = result.get('id')
        if subscription_id is None:
            return

        callback, _ = self.subscriptions.get(subscription_id, (None, None))
        if callback is None:
            return

        if result.get('type', '') == 'complete':
            _LOGGER.debug('Unsubscribe %s successfully.', subscription_id)
            del self.subscriptions[subscription_id]
            return

        data = result.get('payload', {})
        if data is None:
            return

        await callback(data)

    def _cancel_retry_timer(self):
        if self._retry_timer:
            self._retry_timer.cancel()

    def _cancel_client_task(self):
        if self._client_task:
            try:
                self._client_task.cancel()
            finally:
                self._client_task = None
