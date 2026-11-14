"""Invalidări WebSocket fără date de business; clienții recitesc API-ul REST."""
import asyncio
from uuid import uuid4


class LiveUpdates:
    def __init__(self):
        self.epoch = uuid4().hex
        self.revision = 0
        self._loop = None
        self._subscribers: set[asyncio.Queue] = set()

    @property
    def version(self):
        return f"{self.epoch}:{self.revision}"

    def start(self):
        self._loop = asyncio.get_running_loop()

    def changed(self):
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._publish)

    def _publish(self):
        self.revision += 1
        for queue in self._subscribers:
            if not queue.empty():
                queue.get_nowait()
            queue.put_nowait(self.version)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._subscribers.discard(queue)
