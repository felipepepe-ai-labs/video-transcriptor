"""Unit coverage for the X bookmarks progress registry.

Ported from the standalone x-bookmarks-curator's progress.test.js. The registry
is deliberately plain-threaded (queue.Queue, no asyncio): it is published to from
background worker threads and consumed by the SSE endpoint.
"""

import queue

import pytest

from x_progress import ProgressRegistry


@pytest.fixture
def registry():
    return ProgressRegistry()


def _drain(q):
    events = []
    while True:
        try:
            events.append(q.get_nowait())
        except queue.Empty:
            return events


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def test_a_subscriber_receives_published_events(registry):
    q = registry.subscribe()

    registry.publish({"type": "progress", "job": "sync", "message": "scrolling"})

    assert _drain(q) == [{"type": "progress", "job": "sync", "message": "scrolling"}]


def test_every_subscriber_receives_the_same_event(registry):
    first, second = registry.subscribe(), registry.subscribe()

    registry.publish({"type": "progress", "job": "sync"})

    assert _drain(first) == _drain(second)


def test_unsubscribing_stops_delivery(registry):
    q = registry.subscribe()
    registry.unsubscribe(q)

    registry.publish({"type": "progress", "job": "sync"})

    assert _drain(q) == []


def test_a_full_subscriber_never_blocks_the_others(registry):
    """A browser tab that stopped reading must not stall the whole registry."""
    stalled = registry.subscribe(maxsize=1)
    healthy = registry.subscribe()
    registry.publish({"type": "progress", "n": 1})

    registry.publish({"type": "progress", "n": 2})

    assert {e["n"] for e in _drain(healthy)} == {1, 2}


# ---------------------------------------------------------------------------
# Snapshot of in-flight work
# ---------------------------------------------------------------------------

def test_snapshot_starts_empty(registry):
    assert registry.snapshot() == []


def test_snapshot_holds_work_still_in_flight(registry):
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 7})

    assert registry.snapshot() == [
        {"type": "progress", "job": "download", "bookmark_id": 7}
    ]


def test_a_later_event_replaces_the_earlier_one_for_the_same_job(registry):
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 7, "pct": 10})
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 7, "pct": 90})

    assert [e["pct"] for e in registry.snapshot()] == [90]


def test_jobs_on_different_bookmarks_are_tracked_separately(registry):
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 1})
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 2})

    assert len(registry.snapshot()) == 2


@pytest.mark.parametrize("terminal", ["done", "error"])
def test_a_terminal_event_clears_the_job_from_the_snapshot(registry, terminal):
    registry.publish({"type": "progress", "job": "download", "bookmark_id": 7})

    registry.publish({"type": terminal, "job": "download", "bookmark_id": 7})

    assert registry.snapshot() == []


def test_a_terminal_event_is_still_delivered_to_subscribers(registry):
    q = registry.subscribe()

    registry.publish({"type": "done", "job": "sync"})

    assert _drain(q) == [{"type": "done", "job": "sync"}]


# ---------------------------------------------------------------------------
# reporter()
# ---------------------------------------------------------------------------

def test_reporter_tags_events_with_its_job_and_bookmark(registry):
    q = registry.subscribe()
    report = registry.reporter("download", bookmark_id=7)

    report(message="downloading", pct=42)

    assert _drain(q) == [
        {
            "type": "progress",
            "job": "download",
            "bookmark_id": 7,
            "message": "downloading",
            "pct": 42,
        }
    ]


def test_reporter_works_without_a_bookmark(registry):
    """Sync is a whole-account job, not a per-bookmark one."""
    q = registry.subscribe()

    registry.reporter("sync")(message="scrolling")

    assert _drain(q)[0]["bookmark_id"] is None
