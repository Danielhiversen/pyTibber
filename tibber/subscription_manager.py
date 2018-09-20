"""Subscription manager for Graph QL."""

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
        self.subscriptions = {}
        self._url = url
        self.is_running = False
        self.websocket = None
        self.retry_timer = 15
        self._session_id = 0
        self._init_payload = init_payload

    async def start(self):
        """Start websocket."""
        if self.is_running:
            return
        self._session_id = len(self.subscriptions)
        self.loop.create_task(self._connect())
        for subscription_id in range(len(self.subscriptions)):
            callback, sub_query = self.subscriptions.get(subscription_id, (None, None))
            if callback is None:
                continue
            await self.subscribe(sub_query, callback, subscription_id)

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
            self.websocket.send(json.dumps({"type": "init",
                                            "payload": self._init_payload}))
            self.is_running = True

            while self.is_running:
                print("-------")
                result = json.loads(await self.websocket.recv())
                print(result)
                subscription_id = result.get('id')
                if subscription_id is None:
                    continue

                callback, _ = self.subscriptions.get(subscription_id, (None, None))
                if callback is None:
                    continue

                if result.get('type', '') == 'complete':
                    del self.subscriptions[subscription_id]
                    continue

                data = result.get('payload', {})
                if data is None:
                    continue
                await callback(data)

        except Exception:  # pylint: disable=broad-except
            _LOGGER.error('Unexpected error', exc_info=True)
        finally:
            print("finish")
            await self.websocket.close()
            if self.is_running:
                self.is_running = False
                await self.retry()

    async def stop(self, blocking=False):
        """Close websocket connection."""
        for subscription_id in range(len(self.subscriptions)):
            await self.unsubscribe(subscription_id)
        while blocking and not self.subscriptions:
            await asyncio.sleep(0.1, loop=self.loop)

        self.is_running = False
        await self.websocket.close()
        while (blocking and
               self.websocket is not None and
               not self.websocket.closed):
            await asyncio.sleep(0.1, loop=self.loop)

    async def retry(self):
        """Retry to connect to werbsocket."""
        await asyncio.sleep(self.retry_timer, loop=self.loop)
        await self.start()
        _LOGGER.debug('Reconnecting to server in %i.', self.retry_timer)

    async def subscribe(self, sub_query, callback, current_session_id=None):
        """Add a new subscription."""
        print("a")
        if not self.is_running:
            print("av")
            await self.start()
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
        print("avddd")
        self.subscriptions[current_session_id] = (callback, sub_query)
        return current_session_id

    async def unsubscribe(self, subscription_id):
        """Unsubscribe."""
        await self.websocket.send(json.dumps({"id": subscription_id,
                                              "type": "subscription_end"}))
