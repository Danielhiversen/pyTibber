"""Subscription manager."""

import asyncio
import json
import logging
import ssl

import websockets

_LOGGER = logging.getLogger(__name__)


class SubscriptionManager:
    """Subscription manager."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, loop, init_payload, url):
        """Create resources for websocket communication."""
        self.loop = loop
        self.async_callbacks = {}
        self._url = url
        self.is_running = False
        self.websocket = None
        self.retry_timer = 15
        self._session_id = 0
        self._init_payload = init_payload

    def start(self):
        """Start websocket."""
        if self.is_running:
            return
        self.is_running = True
        self._session_id = 0
        self.loop.create_task(self._connect())

    async def _connect(self):
        """Start websocket connection."""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            self.websocket = await websockets.connect(self._url,
                                                      ssl=ssl_context,
                                                      subprotocols=["graphql-subscriptions"])
        except Exception:  # pylint: disable=broad-except
            _LOGGER.error('Unexpected error', exc_info=True)
            if self.is_running:
                self.retry()

        try:
            self.is_running = True
            self.websocket.send(json.dumps({"type": "init",
                                            "payload": self._init_payload}))
            k = 0
            while self.is_running:
                k += 1
                result = json.loads(await self.websocket.recv())
                session_id = result.get('id')
                if session_id is None:
                    continue

                callback = self.async_callbacks.get(session_id)
                if callback is None:
                    continue

                data = result.get('payload', {})
                if data is None:
                    continue
                await callback(data)

        except Exception:  # pylint: disable=broad-except
            _LOGGER.error('Unexpected error', exc_info=True)
            if self.is_running:
                self.retry()
        finally:
            await self.websocket.close()

    async def stop(self, blocking=False):
        """Close websocket connection."""
        for subscription_id in range(len(self.async_callbacks)):
            await self.unsubscribe(subscription_id)
        self.is_running = False
        k = 0
        while blocking and not self.websocket.closed:
            k += 1
            await asyncio.sleep(0.1, loop=self.loop)
            if k % 5:
                await self.websocket.close()

    def retry(self):
        """Retry to connect to Tibber."""
        self.loop.call_later(self.retry_timer, self.start)
        _LOGGER.debug('Reconnecting to Tibber in %i.', self.retry_timer)

    async def subscribe(self, sub_query, callback):
        """Add a new subscription."""
        if not self.is_running:
            await self.start()
        if self.websocket is None:
            await asyncio.sleep(1, loop=self.loop)
            return await self.subscribe(sub_query, callback)

        current_session_id = self._session_id
        self._session_id += 1
        await self._send_subscription(current_session_id, sub_query)
        self.async_callbacks[current_session_id] = callback
        return current_session_id

    async def _send_subscription(self, current_session_id, sub_query):
        subscription = {"query": sub_query,
                        "type": "subscription_start", "id": current_session_id}
        json_subscription = json.dumps(subscription)
        await self.websocket.send(json_subscription)

    async def unsubscribe(self, subscription_id):
        """Unsubscribe."""
        del self.async_callbacks[subscription_id]
        await self.websocket.send(json.dumps({"id": subscription_id,
                                              "type": "subscription_end"}))
