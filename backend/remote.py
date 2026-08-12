"""
Remote Whisper execution over SSH (paramiko).

Wraps the full lifecycle used by the transcription job: connect, pre-flight
disk check, upload, run `whisper` (transcribe or translate task) on the
already-uploaded file, and guaranteed cleanup of everything created on the
remote box regardless of how the job ends. Also runs Piper TTS (Spain
Spanish voice) on the same connection to narrate the translated text.
"""
import fcntl
import io
import json
import os
import queue
import re
import select
import shutil
import subprocess
import tarfile
import threading
import uuid
from pathlib import Path

import logging
import paramiko

logger = logging.getLogger(__name__)


class RemoteUnavailable(Exception):
    """SSH connection to the remote host failed or was refused."""


class UploadFailed(RemoteUnavailable):
    """rsync could not put the file on the remote box.

    Subclasses RemoteUnavailable so callers that already treat "the remote is
    not usable" as one case keep working, while anything that cares can tell an
    upload apart from a refused connection.
    """


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
TRANSFER_TIMEOUT_SECONDS = 120  # rsync saying nothing at all for this long
# Keepalive is what turns a link that silently died into an exception: without
# it TCP alone can sit on a dead socket for hours before giving up.
KEEPALIVE_SECONDS = 30

# One progress report per whole percentage point. rsync repaints its progress
# line many times a second, which for a 681 MB file would be thousands of
# SQLite writes.
_PROGRESS_STEP = 1.0

# The percentage out of `rsync --info=progress2`, e.g. "1,234,567  45%  11MB/s".
_RSYNC_PERCENT = re.compile(r"(\d{1,3})%")

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

    def upload(
        self,
        local_path: str | Path,
        filename: str,
        on_progress=None,
        stall_timeout: float = TRANSFER_TIMEOUT_SECONDS,
    ) -> str:
        """Push the video to remote /tmp with rsync over SSH.

        rsync is the only transfer path, deliberately: the one it replaced
        (SFTP) is exactly what wedged on a 681 MB file, and a second path is a
        second thing to keep timing out correctly. `scp` is not an alternative
        either -- since OpenSSH 9.0 it speaks the SFTP protocol underneath.

        `on_progress` receives a float 0-100, at most once per whole point --
        rsync repaints its progress line constantly, and a caller that persists
        every repaint would write to SQLite thousands of times per upload.
        """
        if self._ssh is None:
            self.connect()
        self._file_id = uuid.uuid4().hex[:8]
        suffix = Path(filename).suffix or ".mp4"
        self._remote_path = f"/tmp/whisper_{self._file_id}{suffix}"

        if shutil.which("rsync") is None:
            raise UploadFailed(
                "rsync is not installed and it is the only upload path "
                "(apt install rsync)"
            )

        local_path = str(local_path)
        remote_spec = f"{self.user}@{self.host}:{self._remote_path}"

        # BatchMode=yes turns a bad key into an error instead of a hang: without
        # it ssh sits on a password prompt that nothing can answer from a
        # subprocess with no TTY.
        ssh_args = (
            "ssh -o StrictHostKeyChecking=yes "
            "-o BatchMode=yes "
            f"-o ConnectTimeout={CONNECT_TIMEOUT_SECONDS} "
            f"-o ServerAliveInterval={KEEPALIVE_SECONDS} "
            "-o ServerAliveCountMax=3"
        )

        # --info=progress2 reports one running total for the transfer as a
        # whole. --no-compress because video is already compressed: deflating it
        # again only burns CPU this box does not have to spare.
        cmd = [
            "rsync", "-a", "--no-compress", "--info=progress2",
            "-e", ssh_args,
            local_path, remote_spec,
        ]

        logger.info("rsync uploading %s -> %s", local_path, self._remote_path)
        # Popen rather than run(): run() only hands back output once the process
        # is over, so there would be nothing to report while 681 MB go up.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,  # universal newlines, so rsync's \r repaints read as lines
            bufsize=1,
        )

        # A reader thread does the blocking so this one can wait on a queue it
        # *can* put a deadline on -- same shape as x_sync.download_video().
        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        last = -1.0
        output: list[str] = []
        try:
            while True:
                try:
                    line = lines.get(timeout=stall_timeout)
                except queue.Empty:
                    # Kill rather than abandon it: an orphaned rsync would keep
                    # pushing bytes at a box we have given up on.
                    proc.kill()
                    raise UploadFailed(
                        f"rsync stalled: no output for {stall_timeout}s "
                        f"uploading {local_path}"
                    )
                if line is None:
                    break
                output.append(line)
                if on_progress:
                    found = _RSYNC_PERCENT.search(line)
                    if found:
                        percent = float(found.group(1))
                        if percent - last >= _PROGRESS_STEP or (percent >= 100 > last):
                            last = percent
                            on_progress(percent)
            proc.wait(timeout=stall_timeout)
        finally:
            proc.stdout.close()

        if proc.returncode != 0:
            raise UploadFailed(
                f"rsync exited {proc.returncode}: {''.join(output).strip()[-500:]}"
            )

        logger.info("rsync upload finished (%s)", self._remote_path)
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

        # One tar down the exec channel rather than a file-by-file download: the
        # channel is already open and authenticated, and a chunk's WAVs are read
        # into memory either way. No -z: PCM barely deflates and the CPU cost
        # lands on the box that can least afford it.
        _, out_tar, _ = self._ssh.exec_command(f"tar -cf - -C {out_dir} .")
        with tarfile.open(fileobj=io.BytesIO(out_tar.read())) as tf:
            # Piper writes with --output-dir-naming timestamp, so ordering by
            # name is what puts the clips back in the order their text went in.
            members = sorted(
                (m for m in tf.getmembers() if m.isfile()), key=lambda m: m.name
            )
            if len(members) != len(chunk):
                raise TTSFailed(
                    f"Piper produced {len(members)} files for {len(chunk)} input lines"
                )
            return [tf.extractfile(m).read() for m in members]

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

        # whisper writes one SRT per run, so the first match is the one.
        # 2>/dev/null keeps a missing directory quiet: the empty result is
        # already the error path, just below.
        _, out_ls, _ = self._ssh.exec_command(f"ls -1 {out_dir}/*.srt 2>/dev/null")
        listing = [n for n in out_ls.read().decode(errors="replace").splitlines() if n.strip()]
        remote_srt = listing[0] if listing else None

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
