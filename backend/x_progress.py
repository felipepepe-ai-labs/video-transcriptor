"""In-memory progress registry for the X bookmarks background workers.

Ported from the standalone x-bookmarks-curator (progress.js / sse.js). Sync and
download run as FastAPI background tasks with nothing to poll, so without this
the UI can only guess when they finished.

Threading note: workers run in a threadpool while the SSE endpoint is async, so
subscribers are plain ``queue.Queue`` objects. Publishing is therefore an
ordinary thread-safe call from any worker, with no event loop involved.
"""

import queue
import threading

# Events that end a unit of work, so it stops counting as in-flight.
TERMINAL_TYPES = frozenset({"done", "error"})

# Bounded so a browser tab that stopped reading cannot grow forever.
DEFAULT_QUEUE_SIZE = 256


class ProgressRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._active: dict[str, dict] = {}

    def subscribe(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> queue.Queue:
        """Register a listener and return the queue its events arrive on."""
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not q]

    def snapshot(self) -> list[dict]:
        """Every unit of work currently in flight, for a client that just connected."""
        with self._lock:
            return list(self._active.values())

    def publish(self, event: dict) -> None:
        key = f"{event.get('job')}:{event.get('bookmark_id')}"
        with self._lock:
            if event.get("type") in TERMINAL_TYPES:
                self._active.pop(key, None)
            else:
                self._active[key] = event
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # A listener that stopped reading must not stall the others.
                pass

    def reporter(self, job: str, bookmark_id: int | None = None):
        """Build a callback that tags every event with this job and bookmark."""

        def report(**partial) -> None:
            self.publish({"type": "progress", "job": job, "bookmark_id": bookmark_id, **partial})

        return report


# One registry per process, shared by the routes and the workers.
registry = ProgressRegistry()
