"""Unit coverage for the human-readable media names.

The job id stays in front of every name because it is the only index there is:
the delete route, the audio/video endpoints and the narration reruns all locate
a job's files by deriving them from its id, with no DB column mapping the two.
So the tests care about two things above all -- that the id still finds the
file, and that files written before this scheme are still found.
"""

import pytest

import media_names


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Charla sobre Rust", "charla-sobre-rust"),
    ("Cómo instalar Postgres en Debian", "como-instalar-postgres-en-debian"),
    ("  spaces   everywhere  ", "spaces-everywhere"),
    ("Slashes/and\\backslashes", "slashes-and-backslashes"),
    ("Emoji 🎬 and ünïcode", "emoji-and-unicode"),
    ("Dots.in.the.middle", "dots-in-the-middle"),
])
def test_slugify_produces_a_safe_readable_name(title, expected):
    assert media_names.slugify(title) == expected


def test_slugify_falls_back_when_nothing_survives():
    """A title of pure emoji would otherwise leave the name a bare id."""
    assert media_names.slugify("🎬🎬🎬") == "video"
    assert media_names.slugify("") == "video"


def test_slugify_is_bounded():
    """Filesystems cap a name at 255 bytes, and the id shares the budget."""
    slug = media_names.slugify("palabra " * 100)

    assert len(slug) <= media_names.MAX_SLUG
    assert not slug.endswith("-")  # never leave a dangling separator


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_media_name_keeps_the_id_in_front():
    name = media_names.media_name("abc123", "Charla sobre Rust", ".mp4")

    assert name == "abc123__charla-sobre-rust.mp4"


def test_chapter_audio_puts_the_index_before_the_slug():
    """delete_job globs '{job_id}_ch*'; a slug in between would break it."""
    name = media_names.chapter_audio_name("abc123", 2, "Charla sobre Rust")

    assert name == "abc123_ch2__charla-sobre-rust.wav"


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_find_media_locates_the_new_scheme(tmp_path):
    (tmp_path / "abc123__charla.mp4").write_bytes(b"x")

    assert media_names.find_media(tmp_path, "abc123").name == "abc123__charla.mp4"


def test_find_media_still_locates_files_written_before_this_change(tmp_path):
    """Every job already on disk is named '{job_id}.ext'. Without this fallback
    they would all start 404ing their audio and video."""
    (tmp_path / "abc123.mp4").write_bytes(b"x")

    assert media_names.find_media(tmp_path, "abc123").name == "abc123.mp4"


def test_find_media_returns_none_when_there_is_nothing(tmp_path):
    assert media_names.find_media(tmp_path, "abc123") is None


def test_find_media_does_not_confuse_a_chapter_clip_with_the_narration(tmp_path):
    """'{job_id}_ch0__*' must not answer a lookup for the whole-job narration."""
    (tmp_path / "abc123_ch0__charla.wav").write_bytes(b"x")

    assert media_names.find_media(tmp_path, "abc123") is None


def test_find_media_does_not_match_a_different_job(tmp_path):
    (tmp_path / "abc123456__charla.mp4").write_bytes(b"x")

    assert media_names.find_media(tmp_path, "abc123") is None


def test_find_chapter_audio_locates_both_schemes(tmp_path):
    (tmp_path / "abc123_ch1__charla.wav").write_bytes(b"x")
    (tmp_path / "def456_ch1.wav").write_bytes(b"x")

    assert media_names.find_chapter_audio(tmp_path, "abc123", 1).name == "abc123_ch1__charla.wav"
    assert media_names.find_chapter_audio(tmp_path, "def456", 1).name == "def456_ch1.wav"


def test_find_chapter_audio_does_not_bleed_between_indexes(tmp_path):
    """'_ch1' must not answer for chapter 1 when only chapter 11 exists."""
    (tmp_path / "abc123_ch11__charla.wav").write_bytes(b"x")

    assert media_names.find_chapter_audio(tmp_path, "abc123", 1) is None


# ---------------------------------------------------------------------------
# The per-job video directory
# ---------------------------------------------------------------------------
# video/ holds a directory per job rather than a loose file. Its contents are
# already named well (dubbed.mp4, chapters/00.mp4); only the directory itself
# was a bare UUID.

def test_the_video_directory_is_named_like_everything_else():
    assert media_names.media_dir_name("abc123", "Charla sobre Rust") == "abc123__charla-sobre-rust"


def test_find_media_dir_locates_the_new_scheme(tmp_path):
    (tmp_path / "abc123__charla").mkdir()

    assert media_names.find_media_dir(tmp_path, "abc123").name == "abc123__charla"


def test_find_media_dir_still_locates_directories_from_before(tmp_path):
    (tmp_path / "abc123").mkdir()

    assert media_names.find_media_dir(tmp_path, "abc123").name == "abc123"


def test_find_media_dir_ignores_a_file_of_the_same_name(tmp_path):
    (tmp_path / "abc123__charla").write_bytes(b"x")

    assert media_names.find_media_dir(tmp_path, "abc123") is None
