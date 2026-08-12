"""A stalled SSH link must fail, not hang.

The upload that started all this pushed 681 MB with no timeout and no progress
callback: when the socket wedged, the transfer blocked forever -- no exception,
no log, no way to tell from the outside. TCP on its own can wait hours before
giving up, so the deadlines have to be explicit.

Uploads go over rsync and nothing else. There is deliberately no fallback: a
second transfer path is a second thing to keep timing out correctly, and the
one it replaced (SFTP) is what hung in the first place.
"""

import queue
import threading

import pytest

import remote as remote_module


class FakeTransport:
    def __init__(self):
        self.keepalive = None

    def set_keepalive(self, interval):
        self.keepalive = interval


class FakeSSHClient:
    def __init__(self):
        self.connect_kwargs = None
        self._transport = FakeTransport()

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self._transport


class FakeStdout:
    """Feeds rsync's progress lines, then EOF. `block` never yields at all,
    standing in for a transfer whose bytes stopped moving."""

    def __init__(self, lines=(), block=False):
        self._lines = list(lines)
        self._block = block
        self.closed = False
        self._released = threading.Event()

    def __iter__(self):
        if self._block:
            # Blocks like a real pipe with nothing coming down it. The reader
            # thread is a daemon, so a test finishing never waits on this.
            self._released.wait()
            return
        yield from self._lines

    def close(self):
        self.closed = True
        self._released.set()


class FakePopen:
    """Stands in for the rsync subprocess."""

    def __init__(self, lines=(), returncode=0, block=False):
        self.stdout = FakeStdout(lines, block=block)
        self.returncode = returncode
        self.killed = False
        self.args = None

    def kill(self):
        self.killed = True
        self.stdout.close()

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture
def fake_ssh(monkeypatch):
    def install():
        client = FakeSSHClient()
        monkeypatch.setattr(remote_module.paramiko, "SSHClient", lambda: client)
        return client

    return install


@pytest.fixture
def fake_rsync(monkeypatch):
    """Installs a fake rsync binary + subprocess. Returns the FakePopen so a
    test can assert on what happened to it."""

    def install(proc=None, available=True):
        proc = proc or FakePopen()
        monkeypatch.setattr(
            remote_module.shutil, "which", lambda name: "/usr/bin/rsync" if available else None
        )

        def fake_popen(cmd, **kwargs):
            proc.args = cmd
            return proc

        monkeypatch.setattr(remote_module.subprocess, "Popen", fake_popen)
        return proc

    return install


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    return path


def _connected(fake_ssh):
    fake_ssh()
    r = remote_module.RemoteWhisper()
    r.connect()
    return r


# rsync -avP prints one repainted line per update; universal-newline mode turns
# its \r into line breaks, which is what makes them readable one at a time.
def _progress(*percents):
    return [f"  1,234,567  {p}%  11.00MB/s    0:00:03\n" for p in percents]


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------

def test_connect_bounds_every_phase_of_the_handshake():
    """Each of the three can hang independently; paramiko defaults none of them."""
    assert remote_module.CONNECT_TIMEOUT_SECONDS > 0
    assert remote_module.BANNER_TIMEOUT_SECONDS > 0
    assert remote_module.AUTH_TIMEOUT_SECONDS > 0


def test_connect_passes_the_timeouts_to_paramiko(fake_ssh):
    client = fake_ssh()

    remote_module.RemoteWhisper().connect()

    assert client.connect_kwargs["timeout"] == remote_module.CONNECT_TIMEOUT_SECONDS
    assert client.connect_kwargs["banner_timeout"] == remote_module.BANNER_TIMEOUT_SECONDS
    assert client.connect_kwargs["auth_timeout"] == remote_module.AUTH_TIMEOUT_SECONDS


def test_connect_turns_on_keepalive(fake_ssh):
    """Without it a link that dies mid-transfer never raises -- it just stops."""
    client = fake_ssh()

    remote_module.RemoteWhisper().connect()

    assert client.get_transport().keepalive == remote_module.KEEPALIVE_SECONDS


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------

def test_upload_reports_progress(fake_ssh, fake_rsync, video):
    fake_rsync(FakePopen(lines=_progress(25, 50, 75, 100)))
    seen = []

    _connected(fake_ssh).upload(video, "clip.mp4", on_progress=lambda pct: seen.append(pct))

    assert seen == [25.0, 50.0, 75.0, 100.0]


def test_upload_does_not_report_once_per_line(fake_ssh, fake_rsync, video):
    """rsync repaints constantly; 681 MB of that would be thousands of SQLite writes."""
    fake_rsync(FakePopen(lines=_progress(*([7] * 500 + [42] * 500 + [100]))))
    seen = []

    _connected(fake_ssh).upload(video, "clip.mp4", on_progress=lambda pct: seen.append(pct))

    # One per whole percentage point, 0 through 100 -- not one per repaint.
    assert len(seen) <= 101, f"{len(seen)} updates for one upload"
    assert seen == sorted(seen)
    assert seen[-1] == 100.0


def test_upload_survives_a_listener_free_call(fake_ssh, fake_rsync, video):
    """No on_progress means nothing to report -- and nothing to crash on."""
    fake_rsync(FakePopen(lines=_progress(50, 100)))

    _connected(fake_ssh).upload(video, "clip.mp4")  # must not raise


def test_upload_without_rsync_says_so(fake_ssh, fake_rsync, video):
    """There is no fallback any more, so the missing binary has to be loud."""
    fake_rsync(available=False)

    with pytest.raises(remote_module.UploadFailed, match="rsync"):
        _connected(fake_ssh).upload(video, "clip.mp4")


def test_upload_aborts_when_the_transfer_stalls(fake_ssh, fake_rsync, video):
    """A wedged transfer raises instead of blocking on a dead socket forever."""
    proc = fake_rsync(FakePopen(block=True))

    with pytest.raises(remote_module.UploadFailed, match="stall"):
        _connected(fake_ssh).upload(video, "clip.mp4", stall_timeout=0.1)

    # Killed, not merely abandoned: an orphaned rsync would keep pushing bytes.
    assert proc.killed


def test_upload_reports_a_failing_rsync(fake_ssh, fake_rsync, video):
    fake_rsync(FakePopen(lines=["rsync: connection unexpectedly closed\n"], returncode=12))

    with pytest.raises(remote_module.UploadFailed, match="12"):
        _connected(fake_ssh).upload(video, "clip.mp4")


def test_upload_never_prompts_for_a_password(fake_ssh, fake_rsync, video):
    """BatchMode=no would let ssh wait on a prompt no one can answer from a
    subprocess with no TTY -- it would hang until the stall timeout."""
    proc = fake_rsync(FakePopen(lines=_progress(100)))

    _connected(fake_ssh).upload(video, "clip.mp4")

    ssh_arg = proc.args[proc.args.index("-e") + 1]
    assert "BatchMode=yes" in ssh_arg


def test_upload_uses_rsync_and_only_rsync(fake_ssh, fake_rsync, video):
    proc = fake_rsync(FakePopen(lines=_progress(100)))

    _connected(fake_ssh).upload(video, "clip.mp4")

    assert proc.args[0] == "rsync"
    assert not hasattr(remote_module.RemoteWhisper, "_sftp_upload")
