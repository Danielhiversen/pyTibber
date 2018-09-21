"""Subscription manager for Graph QL."""

import asyncio
import json
import logging
import ssl

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
        self.retry_timer = 15
        self._session_id = 0
        self._init_payload = init_payload

    def start(self):
        """Start websocket."""
        if self._state == STATE_RUNNING:
            return
        self._state = STATE_STARTING
        self.loop.create_task(self.running())
        for subscription_id in range(len(self.subscriptions)):
            callback, sub_query = self.subscriptions.get(subscription_id, (None, None))
            if callback is None:
                continue
            self.loop.create_task(self.subscribe(sub_query, callback, subscription_id))

    @property
    def is_running(self):
        return self._state == STATE_RUNNING

    async def running(self):
        """Start websocket connection."""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            self.websocket = await websockets.connect(self._url,
                                                      ssl=ssl_context,
                                                      subprotocols=["graphql-subscriptions"])
            self._state = STATE_RUNNING
        except Exception:  # pylint: disable=broad-except
            _LOGGER.error('Unexpected error', exc_info=True)
            self.retry()

        try:
            self.websocket.send(json.dumps({"type": "init",
                                            "payload": self._init_payload}))
            while self._state != STATE_STOPPED:
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
            if self._state != STATE_STOPPED:
                await self.retry()

    async def stop(self, blocking=False):
        """Close websocket connection."""
        for subscription_id in range(len(self.subscriptions)):
            await self.unsubscribe(subscription_id)
        while blocking and not self.subscriptions:
            await asyncio.sleep(0.1, loop=self.loop)

        self._state = STATE_STOPPED
        await self.websocket.close()
        while (blocking and
               self.websocket is not None and
               not self.websocket.closed):
            await asyncio.sleep(0.1, loop=self.loop)

    async def retry(self):
        """Retry to connect to werbsocket."""
        self.loop.call_later(self.retry_timer, self.start)
        _LOGGER.debug('Reconnecting to server in %i.', self.retry_timer)

    async def subscribe(self, sub_query, callback, current_session_id=None):
        """Add a new subscription."""
        print("a")
        if self._state != STATE_RUNNING:
            print("av")
            self.start()
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
