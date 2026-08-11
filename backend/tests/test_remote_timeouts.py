"""A stalled SSH link must fail, not hang.

The upload that started all this pushed 681 MB over SFTP with no timeout and no
progress callback: when the socket wedged, sftp.put() blocked forever -- no
exception, no log, no way to tell from the outside. TCP on its own can wait
hours before giving up, so the deadlines have to be explicit.
"""

import pytest

import remote as remote_module


class FakeTransport:
    def __init__(self):
        self.keepalive = None

    def set_keepalive(self, interval):
        self.keepalive = interval


class FakeSFTP:
    """Records what put() was given, and replays paramiko's progress calls."""

    def __init__(self, blocks=0, total=0):
        self.put_kwargs = None
        self._blocks = blocks
        self._total = total
        self.channel = FakeChannel()

    def put(self, local, remote, **kwargs):
        self.put_kwargs = kwargs
        callback = kwargs.get("callback")
        if callback:
            for i in range(1, self._blocks + 1):
                callback(int(self._total * i / self._blocks), self._total)

    def get_channel(self):
        return self.channel

    def close(self):
        pass


class FakeChannel:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class FakeSSHClient:
    def __init__(self, sftp=None):
        self.connect_kwargs = None
        self._transport = FakeTransport()
        self._sftp = sftp or FakeSFTP()

    def set_missing_host_key_policy(self, policy):
        pass

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self._transport

    def open_sftp(self):
        return self._sftp


@pytest.fixture
def fake_ssh(monkeypatch):
    def install(sftp=None):
        client = FakeSSHClient(sftp=sftp)
        monkeypatch.setattr(remote_module.paramiko, "SSHClient", lambda: client)
        return client

    return install


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

def test_upload_reports_progress(fake_ssh, tmp_path):
    sftp = FakeSFTP(blocks=4, total=1000)
    fake_ssh(sftp=sftp)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    seen = []

    r = remote_module.RemoteWhisper()
    r.connect()
    r.upload(video, "clip.mp4", on_progress=lambda pct: seen.append(pct))

    assert seen == [25.0, 50.0, 75.0, 100.0]


def test_upload_does_not_report_once_per_block(fake_ssh, tmp_path):
    """paramiko calls back every 32 KB: 681 MB would be ~21k SQLite writes."""
    sftp = FakeSFTP(blocks=5000, total=5000 * 32768)
    fake_ssh(sftp=sftp)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    seen = []

    r = remote_module.RemoteWhisper()
    r.connect()
    r.upload(video, "clip.mp4", on_progress=lambda pct: seen.append(pct))

    # One per whole percentage point, 0 through 100 -- not one per 32 KB block.
    assert len(seen) <= 101, f"{len(seen)} updates for one upload"
    assert seen == sorted(seen)
    assert seen[-1] == 100.0


def test_upload_without_a_listener_asks_for_no_callback(fake_ssh, tmp_path):
    sftp = FakeSFTP()
    fake_ssh(sftp=sftp)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    r = remote_module.RemoteWhisper()
    r.connect()
    r.upload(video, "clip.mp4")

    assert sftp.put_kwargs.get("callback") is None


def test_upload_bounds_the_socket(fake_ssh, tmp_path):
    """A wedged transfer raises instead of blocking on a dead socket forever."""
    sftp = FakeSFTP()
    fake_ssh(sftp=sftp)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")

    r = remote_module.RemoteWhisper()
    r.connect()
    r.upload(video, "clip.mp4")

    assert sftp.channel.timeout == remote_module.TRANSFER_TIMEOUT_SECONDS
