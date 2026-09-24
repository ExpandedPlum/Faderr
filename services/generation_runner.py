"""Runs playlist generation as a background task, independent of any client.

Generation used to live inside the SSE response, so closing the tab (or a
phone going to sleep) cancelled it. Now clients subscribe to a shared run:
disconnecting only unsubscribes, and a second request joins the run already in
progress instead of starting a competing one.
"""
import asyncio
import logging
from typing import Optional

from services import triage_service

logger = logging.getLogger(__name__)


class GenerationRunner:
    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._subscribers: set[asyncio.Queue] = set()
        self._last_event: Optional[dict] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def subscribe(self) -> asyncio.Queue:
        """Start receiving events. A subscriber joining mid-run first gets the
        latest progress event so its progress bar isn't blank."""
        queue: asyncio.Queue = asyncio.Queue()
        if self.running and self._last_event is not None:
            queue.put_nowait(self._last_event)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def start(self) -> bool:
        """Start a run unless one is already in progress. Returns True if started."""
        if self.running:
            return False
        self._last_event = None
        self._task = asyncio.create_task(self._run())
        return True

    async def _publish(self, event: dict) -> None:
        self._last_event = event
        for queue in list(self._subscribers):
            queue.put_nowait(event)

    async def _run(self) -> None:
        try:
            await triage_service.generate_triage_playlist(on_progress=self._publish)
        except Exception as exc:
            logger.exception("Generation failed")
            await self._publish({"stage": "error", "message": str(exc)})


runner = GenerationRunner()
