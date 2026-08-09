"""Unit coverage for the X bookmark video download.

Ported from the standalone x-bookmarks-curator's download.test.js. The point of
this suite is the distinction the original was careful about: "this tweet has no
video" is a normal outcome, not a failure — not every bookmark is a video.
"""

import subprocess

import pytest

import x_sync


class FakeCompletedProcess:
    def __init__(self, returncode=0, stderr="", stdout=""):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture
def fake_ytdlp(monkeypatch, tmp_path):
    """Fake yt-dlp: records the command and optionally writes an output file."""
    calls = []

    def install(returncode=0, stderr="", writes=None):
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if writes:
                (tmp_path / "downloads" / writes).write_bytes(b"video-bytes")
            return FakeCompletedProcess(returncode=returncode, stderr=stderr)

        monkeypatch.setattr(subprocess, "run", fake_run)
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
