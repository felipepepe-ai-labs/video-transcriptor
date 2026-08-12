"""Human-readable names for the media a job produces.

Browsing uploads/, audio/ or video/ used to show nothing but UUIDs. The fix is
a slug appended to the id rather than one replacing it, because the id is the
only index there is: ``delete_job``, the audio/video endpoints and the
narration reruns all locate a job's files by deriving them from its id, with no
DB column mapping the two. Keeping the id in front means those lookups become a
glob on the prefix instead of an exact name, and nothing else has to change.

``find_media`` also answers with files written before this scheme existed;
without that fallback every job already on disk would start 404ing its media.
"""

import re
import unicodedata
from pathlib import Path

# Separates the id from the slug. Two underscores rather than one so that
# '{job_id}_ch0__chapter' cannot be mistaken for a whole-job file.
SEPARATOR = "__"

# Filesystems cap a name at 255 bytes and the id, separator and extension all
# share that budget; 60 leaves plenty of room and still reads as a title.
MAX_SLUG = 60

_FALLBACK = "video"


def slugify(text: str, max_len: int = MAX_SLUG, fallback: str = _FALLBACK) -> str:
    """Lowercase ASCII words joined by hyphens, safe on every filesystem.

    *fallback* names what an unusable input becomes — a handle written entirely
    in kanji leaves nothing behind once the non-ASCII bytes are dropped, and
    calling that "video" would only be right for the media files this was
    written for.
    """
    # NFKD splits 'ü' into 'u' + combining diaeresis, and dropping the
    # non-ASCII bytes then leaves the plain letter rather than losing the word.
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or fallback


def media_name(job_id: str, title: str, suffix: str) -> str:
    """The name a job's main media file takes: ``{job_id}__{slug}{suffix}``."""
    return f"{job_id}{SEPARATOR}{slugify(title)}{suffix}"


def chapter_audio_name(job_id: str, index: int, title: str) -> str:
    """Per-chapter narration.

    The index stays immediately after the id because ``delete_job`` sweeps
    these with a ``{job_id}_ch*`` glob; a slug in between would break it.
    """
    return f"{job_id}_ch{index}{SEPARATOR}{slugify(title)}.wav"


def media_dir_name(job_id: str, title: str) -> str:
    """The per-job directory under video/. Its contents already read well."""
    return f"{job_id}{SEPARATOR}{slugify(title)}"


def find_media(directory: Path, job_id: str) -> Path | None:
    """Locate a job's main media file, whichever scheme wrote it.

    Returns None rather than raising: callers already answer 404 when a stage
    has not produced its file yet, and 'not there' is the same answer.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return None

    for pattern in (f"{job_id}{SEPARATOR}*", f"{job_id}.*"):
        for candidate in sorted(directory.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def find_chapter_audio(directory: Path, job_id: str, index: int) -> Path | None:
    """One chapter's narration, under either scheme.

    The patterns are anchored past the index so that chapter 1 cannot be
    answered by chapter 11's file.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return None

    stem = f"{job_id}_ch{index}"
    for pattern in (f"{stem}{SEPARATOR}*.wav", f"{stem}.wav"):
        for candidate in sorted(directory.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def find_media_dir(parent: Path, job_id: str) -> Path | None:
    """The video/ directory for a job, under either scheme."""
    parent = Path(parent)
    if not parent.is_dir():
        return None

    for pattern in (f"{job_id}{SEPARATOR}*", job_id):
        for candidate in sorted(parent.glob(pattern)):
            if candidate.is_dir():
                return candidate
    return None
