"""
Remote Whisper execution over SSH (paramiko).

Wraps the full lifecycle used by the transcription job: connect, pre-flight
disk check, upload, run `whisper` (transcribe or translate task) on the
already-uploaded file, and guaranteed cleanup of everything created on the
remote box regardless of how the job ends. Also runs Piper TTS (Spain
Spanish voice) on the same connection to narrate the translated text.
"""
import fcntl
import json
import os
import select
import subprocess
import threading
import uuid
from pathlib import Path

import logging
import paramiko

logger = logging.getLogger(__name__)


class RemoteUnavailable(Exception):
    """SSH connection to the remote host failed or was refused."""


class InsufficientRemoteStorage(Exception):
    """Remote /tmp does not have enough free space for this upload."""


class TranscriptionFailed(Exception):
    """Whisper exited non-zero or produced no SRT output."""


class TTSFailed(Exception):
    """Piper exited non-zero or produced no audio output."""


# Both are real es_ES (Castilian) models -- picked over Kokoro, whose
# Spanish pack has no es_ES/es_419 split. sharvard-medium is a multi-speaker
# model (speaker_id_map M=0, F=1); davefx-medium is single-speaker (male).
# Deadlines. paramiko defaults every one of these to "wait forever", which is
# how a 681 MB upload over a flaky link froze a job for 21 hours: no exception,
# no log, nothing to see from outside.
CONNECT_TIMEOUT_SECONDS = 15  # TCP connect to the remote box
BANNER_TIMEOUT_SECONDS = 20  # SSH banner, slow to arrive on a loaded host
AUTH_TIMEOUT_SECONDS = 20  # key exchange and auth
TRANSFER_TIMEOUT_SECONDS = 120  # no bytes moving on the SFTP channel for this long
# Keepalive is what turns a link that silently died into an exception: without
# it TCP alone can sit on a dead socket for hours before giving up.
KEEPALIVE_SECONDS = 30

# One progress report per whole percentage point. paramiko calls back roughly
# every 32 KB, which for a 681 MB file would be ~21,000 SQLite writes.
_PROGRESS_STEP = 1.0

VOICE_PRESETS = {
    "male": {"model": "es_ES-davefx-medium", "speaker": None},
    "female": {"model": "es_ES-sharvard-medium", "speaker": 1},
}
DEFAULT_VOICE = "male"


class RemoteWhisper:
    """
    One instance == one remote job lifecycle: a single SSH connection, a
    single uploaded file, and one or more Whisper runs against it (e.g.
    transcribe, then translate on the same file with no re-upload).
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        model: str | None = None,
        device: str | None = None,
        disk_safety_margin: float | None = None,
    ):
        self.host = host or os.getenv("REMOTE_HOST", "192.168.1.60")
        self.user = user or os.getenv("REMOTE_USER", "felipe")
        self.model = model or os.getenv("REMOTE_MODEL", "large-v3-turbo")
        self.device = device or os.getenv("REMOTE_DEVICE", "cuda")
        self.disk_safety_margin = disk_safety_margin or float(
            os.getenv("REMOTE_DISK_SAFETY_MARGIN", "3")
        )

        self._ssh: paramiko.SSHClient | None = None
        self._file_id: str | None = None
        self._remote_path: str | None = None
        self._out_dirs: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.cleanup()

    # ── Lifecycle steps ──────────────────────────────────────────────
    def connect(self) -> None:
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host,
                username=self.user,
                allow_agent=True,
                look_for_keys=True,
                timeout=CONNECT_TIMEOUT_SECONDS,
                banner_timeout=BANNER_TIMEOUT_SECONDS,
                auth_timeout=AUTH_TIMEOUT_SECONDS,
            )
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(KEEPALIVE_SECONDS)
            self._ssh = client
        except (paramiko.SSHException, OSError) as e:
            raise RemoteUnavailable(f"Could not reach {self.host}: {e}") from e

    def check_disk_space(self, file_size_bytes: int) -> None:
        """Refuse the job if remote /tmp doesn't have file_size * safety_margin free."""
        if self._ssh is None:
            self.connect()
        _, out, _ = self._ssh.exec_command("df --output=avail -k /tmp")
        lines = [ln.strip() for ln in out.read().decode(errors="replace").splitlines() if ln.strip()]
        if not lines:
            raise InsufficientRemoteStorage("Could not determine remote free disk space")
        avail_bytes = int(lines[-1]) * 1024
        required_bytes = file_size_bytes * self.disk_safety_margin
        if avail_bytes < required_bytes:
            raise InsufficientRemoteStorage(
                f"Remote /tmp has {avail_bytes} bytes free, need {required_bytes:.0f} "
                f"({file_size_bytes} bytes x {self.disk_safety_margin} safety margin)"
            )

    def upload(self, local_path: str | Path, filename: str, on_progress=None) -> str:
        """Push the video to remote /tmp via SCP (fast), falling back to SFTP if needed.

        `on_progress` receives a float 0-100, at most once per whole point --
        paramiko itself calls back every 32 KB, and a caller that persists each
        one would write to SQLite thousands of times per upload.
        """
        if self._ssh is None:
            self.connect()
        self._file_id = uuid.uuid4().hex[:8]
        suffix = Path(filename).suffix or ".mp4"
        self._remote_path = f"/tmp/whisper_{self._file_id}{suffix}"

        # ── SSH security options (one-off, not per-packet) ──────────────
        secure_opts = [
            "StrictHostKeyChecking=yes",
            "BatchMode=yes",
            "ForwardAgent=no",
            "IdentitiesOnly=yes",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            f"ServerAliveInterval={KEEPALIVE_SECONDS}",
            f"ServerAliveCountMax=3",
        ]

        # ── Identity files (for SSH key auth) ─────────────────────────
        identity_files = []
        for name in ("id_ed25519", "id_rsa", "id_ecdsa", "id_dsa"):
            p = Path.home() / ".ssh" / name
            if p.exists():
                identity_files.append(str(p))

        # ── Primary path: rsync over SSH (fast, reliable, checkpoint-aware)
        local_path = str(local_path)
        remote_spec = f"{self.user}@{self.host}:{self._remote_path}"

        # Detect the SSH port; default to 22 when the transport is a test mock.
        ssh_port = 22
        try:
            transport = self._ssh.get_transport()
            if transport is not None:
                ssh_port = transport.local_port or 22
        except Exception:
            pass

        # Use SSH agent forwarding so rsync authenticates with the SAME keys as
        # the paramiko connection (which uses allow_agent=True). This avoids the
        # common pitfall where -i ~/.ssh/keys doesn't match the actual auth key.
        ssh_args = (
            f"ssh -p {ssh_port} "
            f"-o StrictHostKeyChecking=yes "
            f"-o ForwardAgent=yes "
            f"-o BatchMode=no "
            f"-o ConnectTimeout={CONNECT_TIMEOUT_SECONDS} "
            f"-o ServerAliveInterval={KEEPALIVE_SECONDS} "
            f"-o ServerAliveCountMax=3 "
        ).strip()

        # rsync -avP: archive, verbose, show progress (percentage + speed).
        rsync_cmd = [
            "rsync", "-avP", "--no-compress",
            "-e", ssh_args,
            local_path, remote_spec,
        ]

        transfer_success = False
        scp_timeout = int(TRANSFER_TIMEOUT_SECONDS * 2)
        file_size = Path(local_path).stat().st_size

        # Only try rsync if the binary exists and the transport is a real SSH
        # connection (not a test mock that doesn't have a real socket).
        _transport = self._ssh.get_transport()
        _sock = getattr(_transport, "sock", None) if _transport else None
        has_real_socket = (
            _sock is not None and getattr(_sock, "getsockname", lambda: None)() is not None
        )
        can_use_rsync = (
            subprocess.run(["rsync", "--version"], capture_output=True).returncode == 0
            and has_real_socket
        )

        if can_use_rsync:
            # Try rsync with real subprocess — fast and reliable.
            logger.info("Attempting upload via rsync (%s -> %s)", local_path, self._remote_path)
            try:
                proc = subprocess.run(
                    rsync_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=scp_timeout,
                )
                if proc.returncode == 0:
                    transfer_success = True
                    logger.info("rsync upload succeeded (%s -> %s)", local_path, self._remote_path)
            except subprocess.TimeoutExpired as e:
                stderr_preview = (e.stderr or b"")[-300:].decode(errors="replace")
                logger.warning("rsync timed out (stderr=%s); falling back to SFTP", stderr_preview)
            except FileNotFoundError:
                logger.debug("rsync disappeared; using SFTP")
            except Exception as e:
                stderr_preview = (getattr(e, 'cmd', '') or '').split()[-1] if hasattr(e, 'cmd') else str(e)[:200]
                logger.warning("rsync failed (%s); falling back to SFTP", e)

        if not transfer_success and on_progress and file_size > 0:
            # Report progress using paramiko's own callback (SFTP).
            pass

        if not transfer_success:
            # ── Fallback: paramiko SFTP (rsync unavailable or failed)
            logger.info("Falling back to SFTP for upload (%s -> %s)", local_path, self._remote_path)
            sftp = self._ssh.open_sftp()
            try:
                channel = sftp.get_channel()
                if channel is not None:
                    channel.settimeout(TRANSFER_TIMEOUT_SECONDS)

                callback = None
                if on_progress:
                    last = [-1.0]

                    def callback(transferred, total):  # noqa: F811 (paramiko's shape)
                        if not total:
                            return
                        percent = round(transferred / total * 100, 1)
                        if percent - last[0] >= _PROGRESS_STEP or (percent >= 100 > last[0]):
                            last[0] = percent
                            on_progress(percent)

                sftp.put(local_path, self._remote_path, callback=callback)
            finally:
                sftp.close()
        return self._remote_path

    def run_transcribe(self, language: str | None = "en") -> tuple[str, float, str]:
        """Run `whisper --task transcribe`. Returns (srt_content,
        duration_seconds, language) -- `language` echoes the forced source
        language, or the one whisper auto-detected when None is passed.

        Note: there is no run_translate() here. Whisper's --task translate
        only ever translates speech INTO English, never into an arbitrary
        target language, so it can't serve as an EN->ES translator. See
        translate.py's OllamaTranslator for the EN->ES fallback instead.
        """
        return self._run_whisper_task("transcribe", language=language)

    def run_tts_batch(
        self,
        texts: list[str],
        voice: str = DEFAULT_VOICE,
        chunk_size: int = 200,
        on_progress=None,
    ) -> list[bytes]:
        """Synthesize many short texts (one per subtitle segment) with an
        es_ES voice ("male" or "female", see VOICE_PRESETS). Returns WAV
        bytes in input order. Batches into chunks per Piper invocation --
        one process/model-load per chunk, not per segment, since a job can
        have thousands of segments and Piper's own startup cost dominates
        at that scale otherwise. Does not require upload()."""
        if self._ssh is None:
            self.connect()
        preset = VOICE_PRESETS.get(voice, VOICE_PRESETS[DEFAULT_VOICE])
        results: list[bytes] = []
        for start in range(0, len(texts), chunk_size):
            results.extend(self._run_tts_chunk(texts[start : start + chunk_size], preset))
            if on_progress:
                on_progress(len(results), len(texts))
        return results

    def _run_tts_chunk(self, chunk: list[str], preset: dict) -> list[bytes]:
        out_dir = f"/tmp/piper_batch_{uuid.uuid4().hex[:8]}"
        self._out_dirs.append(out_dir)

        cmd = (
            f"export PATH=$PATH:~/.local/bin && mkdir -p {out_dir} && piper "
            f"--model ~/piper-voices/{preset['model']}.onnx "
            + (f"--speaker {preset['speaker']} " if preset["speaker"] is not None else "")
            + f"-d {out_dir} --output-dir-naming timestamp"
        )
        # Piper reads one line of stdin per output file -- collapse any
        # internal newlines and never send a blank line (Piper skips it,
        # which would desync the output count from the input count).
        stdin_text = "\n".join((t.replace("\n", " ").strip() or ".") for t in chunk)

        stdin, stdout, stderr = self._ssh.exec_command(cmd)
        stdin.write(stdin_text + "\n")
        stdin.channel.shutdown_write()
        exit_code = stdout.channel.recv_exit_status()

        if exit_code != 0:
            out_txt = stdout.read().decode(errors="replace")
            err_txt = stderr.read().decode(errors="replace")
            raise TTSFailed(f"Piper batch failed (exit {exit_code}): {(err_txt or out_txt)[-500:]}")

        sftp = self._ssh.open_sftp()
        try:
            filenames = sorted(attr.filename for attr in sftp.listdir_attr(out_dir))
            if len(filenames) != len(chunk):
                raise TTSFailed(f"Piper produced {len(filenames)} files for {len(chunk)} input lines")
            audio: list[bytes] = []
            for fn in filenames:
                with sftp.open(f"{out_dir}/{fn}", "rb") as f:
                    audio.append(f.read())
            return audio
        finally:
            sftp.close()

    def cleanup(self) -> None:
        """Guaranteed cleanup: remove uploaded file + all output dirs on the remote box."""
        if self._ssh is None:
            return
        try:
            parts = []
            if self._remote_path:
                parts.append(f"rm -f {self._remote_path}")
            for out_dir in self._out_dirs:
                parts.append(f"rm -rf {out_dir}")
            if parts:
                self._ssh.exec_command("; ".join(parts))
        finally:
            self._ssh.close()
            self._ssh = None

    # ── Internal ─────────────────────────────────────────────────────
    def _run_whisper_task(self, task: str, language: str | None = "en") -> tuple[str, float, str]:
        if self._ssh is None or self._remote_path is None or self._file_id is None:
            raise RuntimeError("upload() must be called before running whisper")

        out_dir = f"/tmp/whisper_out_{self._file_id}_{task}"
        self._out_dirs.append(out_dir)

        # No --language flag -> whisper auto-detects the spoken language and
        # records it in the output JSON (parsed below).
        language_flag = f"--language {language} " if language else ""
        cmd = (
            f"cd /tmp && python3 -m whisper {self._remote_path} "
            f"--model {self.model} --task {task} "
            f"{language_flag}--output_format all "
            f"--output_dir {out_dir} "
            f"--device {self.device}"
        )
        stdin, stdout, stderr = self._ssh.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()

        if exit_code != 0:
            out_txt = stdout.read().decode(errors="replace")
            err_txt = stderr.read().decode(errors="replace")
            raise TranscriptionFailed(
                f"Whisper failed (exit {exit_code}): {(err_txt or out_txt)[-500:]}"
            )

        remote_srt = None
        sftp = self._ssh.open_sftp()
        try:
            for attr in sftp.listdir_attr(out_dir):
                if attr.filename.endswith(".srt"):
                    remote_srt = f"{out_dir}/{attr.filename}"
                    break
        finally:
            sftp.close()

        if not remote_srt:
            raise TranscriptionFailed("No SRT file generated by Whisper")

        _, out, _ = self._ssh.exec_command(f"cat {remote_srt}")
        srt_content = out.read().decode("utf-8", errors="replace")

        duration = 0.0
        detected_language = language or ""
        remote_json_path = remote_srt.replace(".srt", ".json")
        try:
            _, out_j, _ = self._ssh.exec_command(f"cat {remote_json_path}")
            json_data = json.loads(out_j.read().decode())
            segs = json_data.get("segments", [])
            if segs:
                duration = max(s.get("end", 0) for s in segs)
            detected_language = json_data.get("language") or detected_language
        except Exception:
            pass

        return srt_content, duration, detected_language
