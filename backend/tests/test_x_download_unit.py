"""Unit coverage for the X bookmark video download.

Ported from the standalone x-bookmarks-curator's download.test.js. The point of
this suite is the distinction the original was careful about: "this tweet has no
video" is a normal outcome, not a failure — not every bookmark is a video.
"""

import itertools
import subprocess
import threading
import time

import pytest

import x_sync


class FakeStdout:
    """Just enough of a pipe: iterable once, and closeable so we can assert it was."""

    def __init__(self, lines):
        self._lines = iter(lines)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)

    def close(self):
        self.closed = True


class FakeProcess:
    """yt-dlp seen through Popen: its output arrives line by line while it runs."""

    def __init__(self, returncode=0, lines=()):
        self.returncode = returncode
        self.stdout = FakeStdout(lines)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.returncode


class Calls(list):
    """The commands yt-dlp was invoked with, plus the fake processes they produced."""

    def __init__(self):
        super().__init__()
        self.processes: list[FakeProcess] = []


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    """Fake yt-dlp: records the command and optionally writes an output file."""
    calls = Calls()

    def install(returncode=0, stderr="", writes=None, progress=()):
        def fake_popen(cmd, **kwargs):
            calls.append(cmd)
            if writes:
                (tmp_path / "downloads" / writes).write_bytes(b"video-bytes")
            # download_video folds stderr into stdout, as the real call does, so
            # failure messages still arrive through these same lines. Chained
            # lazily, not unpacked: `progress` may be a generator that blocks on
            # purpose, and unpacking it here would hang before the read begins.
            lines = itertools.chain(progress, stderr.splitlines() if stderr else [])
            proc = FakeProcess(returncode=returncode, lines=lines)
            calls.processes.append(proc)
            return proc

        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        return calls

    return install


@pytest.fixture
def dest_dir(tmp_path):
    target = tmp_path / "downloads"
    target.mkdir()
    return target


TWEET = "https://x.com/someone/status/1234567890"


def test_download_returns_the_downloaded_file(fake_ytdlp, dest_dir):
    fake_ytdlp(writes="1234567890.mp4")

    path = x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)

    assert path == str(dest_dir / "1234567890.mp4")


def test_download_passes_the_cookie_jar_to_yt_dlp(fake_ytdlp, dest_dir):
    calls = fake_ytdlp(writes="1234567890.mp4")

    x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)

    assert "--cookies" in calls[0]
    assert calls[0][calls[0].index("--cookies") + 1] == "/tmp/cookies.txt"
    assert calls[0][-1] == TWEET


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: No video could be found in this tweet",
        "ERROR: no media found for this url",
    ],
)
def test_download_reports_a_tweet_without_video_as_no_media(
    fake_ytdlp, dest_dir, stderr
):
    fake_ytdlp(returncode=1, stderr=stderr)

    with pytest.raises(x_sync.NoMediaFound):
        x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)


def test_download_reports_a_real_failure_as_a_failure(fake_ytdlp, dest_dir):
    """An expired cookie jar is a failure to fix, not a video-less tweet."""
    fake_ytdlp(returncode=1, stderr="ERROR: HTTP Error 401: Unauthorized")

    with pytest.raises(x_sync.DownloadFailed):
        x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)


def test_download_does_not_mistake_a_failure_for_no_media(fake_ytdlp, dest_dir):
    """'missing' appears in unrelated yt-dlp errors; it must not mean no_media."""
    fake_ytdlp(returncode=1, stderr="ERROR: ffmpeg is missing, install it")

    with pytest.raises(x_sync.DownloadFailed):
        x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)


def test_download_never_returns_a_previously_downloaded_file(fake_ytdlp, dest_dir):
    """A silent no-op must fail loudly, not hand back someone else's video."""
    (dest_dir / "9999.mp4").write_bytes(b"an older, unrelated download")
    fake_ytdlp(returncode=0, stderr="")  # succeeds but writes nothing

    with pytest.raises(x_sync.DownloadFailed):
        x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)


def test_download_picks_the_new_file_when_the_directory_is_not_empty(
    fake_ytdlp, dest_dir
):
    (dest_dir / "9999.mp4").write_bytes(b"an older, unrelated download")
    fake_ytdlp(writes="1234567890.mp4")

    path = x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)

    assert path == str(dest_dir / "1234567890.mp4")


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------
# All the fragile logic lives in a pure parser, so it can be exercised without
# launching anything. The lines below are verbatim from a real yt-dlp run.

def test_progress_line_yields_percent_size_and_speed():
    line = "[download]   4.4% of  337.53KiB at  852.37KiB/s ETA 00:00"

    assert x_sync._parse_progress_line(line) == {
        "percent": 4.4, "size": "337.53KiB", "speed": "852.37KiB/s",
    }


def test_the_closing_line_is_understood_too():
    """Its shape differs: 'in 00:00:00 at' rather than 'at … ETA'."""
    line = "[download] 100% of  337.53KiB in 00:00:00 at 2.36MiB/s"

    assert x_sync._parse_progress_line(line)["percent"] == 100.0


def test_the_number_of_formats_is_picked_up():
    """With two formats yt-dlp counts 0-100 twice, which is why this matters."""
    line = "[info] 2076688147555946496: Downloading 2 format(s): http-2176+http-audio"

    assert x_sync._parse_progress_line(line) == {"formats": 2}


def test_a_new_destination_starts_another_pass():
    line = "[download] Destination: /tmp/x/2076688147555946496.f2176.mp4"

    assert x_sync._parse_progress_line(line) == {"pass_started": True}


def test_merging_is_its_own_phase():
    line = '[Merger] Merging formats into "/tmp/x/2076688147555946496.mp4"'

    assert x_sync._parse_progress_line(line) == {"merging": True}


@pytest.mark.parametrize("line", [
    "[twitter] Extracting URL: https://x.com/a/status/1",
    "WARNING: Your yt-dlp version is older than 90 days!",
    "",
    "not yt-dlp output at all",
])
def test_noise_is_ignored(line):
    assert x_sync._parse_progress_line(line) is None


def test_progress_never_goes_backwards_across_two_passes():
    """A raw percent would drop to 0 midway, which is worse than no bar at all."""
    tracker = x_sync._ProgressTracker()
    seen = []
    for line in [
        "[info] 123: Downloading 2 format(s): video+audio",
        "[download] Destination: /tmp/x/123.f1.mp4",
        "[download]  50.0% of 10.00MiB at 1.00MiB/s ETA 00:05",
        "[download] 100% of 10.00MiB in 00:00:10 at 1.00MiB/s",
        "[download] Destination: /tmp/x/123.f2.m4a",
        "[download]  50.0% of  2.00MiB at 1.00MiB/s ETA 00:01",
        "[download] 100% of  2.00MiB in 00:00:02 at 1.00MiB/s",
    ]:
        update = tracker.feed(line)
        if update and "percent" in update:
            seen.append(update["percent"])

    assert seen == sorted(seen), f"progress went backwards: {seen}"
    assert seen[-1] == 100.0
    assert max(seen[:2]) <= 50.0  # first pass tops out at half of the whole


def test_download_reports_progress_to_its_caller(fake_ytdlp, dest_dir):
    """The whole point: the card shows a bar instead of a frozen hourglass."""
    fake_ytdlp(writes="1234567890.mp4", progress=[
        "[info] 123: Downloading 1 format(s): http-2176",
        "[download] Destination: /tmp/x/123.mp4",
        "[download]  25.0% of 10.00MiB at 1.00MiB/s ETA 00:07",
        "[download] 100% of 10.00MiB in 00:00:10 at 1.00MiB/s",
    ])
    reported = []

    x_sync.download_video(
        TWEET, "/tmp/cookies.txt", dest_dir=dest_dir,
        on_progress=lambda **update: reported.append(update),
    )

    assert [u["percent"] for u in reported] == [25.0, 100.0]
    assert reported[0]["speed"] == "1.00MiB/s"


def test_download_still_works_without_a_listener(fake_ytdlp, dest_dir):
    """No callback, no cost: parsing is skipped entirely."""
    fake_ytdlp(writes="1234567890.mp4", progress=["[download]  50.0% of 1.00MiB at 1.00MiB/s ETA 00:01"])

    assert x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)


def test_yt_dlp_is_told_to_print_progress_on_its_own_lines(fake_ytdlp, dest_dir):
    """Without --newline yt-dlp repaints one line with \\r and nothing streams."""
    calls = fake_ytdlp(writes="1234567890.mp4")

    x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)

    assert "--newline" in calls[0]


# ---------------------------------------------------------------------------
# Stall watchdog
# ---------------------------------------------------------------------------
# Reading the pipe line by line is what makes a progress bar possible, but it
# also blocks forever if yt-dlp wedges. The deadline is on silence rather than
# on total duration: a 681 MB file over a home line legitimately takes as long
# as it takes, whereas no output at all never becomes healthy.

def test_a_download_that_stops_reporting_is_killed(fake_ytdlp, dest_dir):
    """A wedged yt-dlp would otherwise hang the worker thread for good."""
    unblock = threading.Event()

    def stalls():
        yield "[download] Destination: /tmp/x/123.mp4\n"
        yield "[download]  10.0% of 100.00MiB at 1.00MiB/s ETA 01:30\n"
        unblock.wait(10)  # released below, so no thread outlives the test

    calls = fake_ytdlp(progress=stalls())

    try:
        with pytest.raises(x_sync.DownloadFailed) as excinfo:
            x_sync.download_video(
                TWEET, "/tmp/cookies.txt", dest_dir=dest_dir, stall_timeout=0.05
            )
    finally:
        unblock.set()

    # Killing is part of the contract: run() used to do it, Popen does not, and
    # an orphaned yt-dlp would keep pulling against X unattended.
    assert calls.processes[0].killed
    assert "stalled" in str(excinfo.value)


def test_a_slow_download_that_keeps_reporting_is_left_alone(fake_ytdlp, dest_dir):
    """Only silence means wedged — crawling along is a legitimate big download."""

    def crawls():
        for percent in (10.0, 20.0, 30.0):
            time.sleep(0.02)  # each gap comfortably under the deadline
            yield f"[download]  {percent}% of 700.00MiB at 100.00KiB/s ETA 02:00\n"

    calls = fake_ytdlp(writes="1234567890.mp4", progress=crawls())
    reported = []

    path = x_sync.download_video(
        TWEET, "/tmp/cookies.txt", dest_dir=dest_dir,
        stall_timeout=0.5, on_progress=lambda **update: reported.append(update),
    )

    assert path
    assert [u["percent"] for u in reported] == [10.0, 20.0, 30.0]
    assert not calls.processes[0].killed


def test_the_pipe_is_closed_once_the_download_is_over(fake_ytdlp, dest_dir):
    """Left open, every download would leak a file descriptor."""
    calls = fake_ytdlp(writes="1234567890.mp4")

    x_sync.download_video(TWEET, "/tmp/cookies.txt", dest_dir=dest_dir)

    assert calls.processes[0].stdout.closed
