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

    def __init__(self, loop, access_token, url):
        """Create resources for websocket communication."""
        self.loop = loop
        self.async_callbacks = {}
        self._url = url
        self.is_running = False
        self.websocket = None
        self.retry_timer = 15
        self._session_id = 0
        self._access_token = access_token

    def start(self):
        """Start websocket."""
        if self.is_running:
            return
        self.is_running = True
        self._session_id = 0
        self.loop.create_task(self._connect())

    async def _connect(self):
        """Start websocket connection."""
        try:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            self.websocket = await websockets.connect(self._url,
                                                      ssl=ssl_context,
                                                      subprotocols=["graphql-subscriptions"])
            self.is_running = True
            self.websocket.send(json.dumps({"type": "init",
                                            "payload": "token={}".format(self._access_token)}))
            k = 0
            while self.is_running:
                k += 1
                print(k)
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
        else:
            if self.is_running:
                self.retry()

    def stop(self):
        """Close websocket connection."""
        self.is_running = False

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
        subscription = {"query": sub_query,
                        "type": "subscription_start", "id": current_session_id}
        json_subscription = json.dumps(subscription)

        await self.websocket.send(json_subscription)
        self.async_callbacks[current_session_id] = callback
        return current_session_id

    async def unsubscribe(self, subscription_id):
        """Unsubscribe."""
        del self.async_callbacks[subscription_id]
        await self.websocket.send(json.dumps({"id": subscription_id,
                                              "type": "subscription_end"}))
