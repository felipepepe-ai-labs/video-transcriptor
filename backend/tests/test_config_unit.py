"""Where the media files land.

A single downloaded X video weighed 681 MB, so the directories holding media
have to be movable off the repo without touching code. The databases
deliberately do not follow this root — see test_databases_do_not_follow.
"""

import json
from pathlib import Path

import pytest

import config


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """Point the settings file at a throwaway path for every test."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    return path


def test_data_root_defaults_to_the_backend_directory(monkeypatch):
    """The default must not move anything: an unset DATA_ROOT is the status quo."""
    monkeypatch.delenv("DATA_ROOT", raising=False)

    assert config.data_root() == Path(config.__file__).parent


def test_data_root_follows_the_environment(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", "/mnt/big-disk/vt")

    assert config.data_root() == Path("/mnt/big-disk/vt")


def test_every_media_directory_hangs_off_the_root(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", "/mnt/big-disk/vt")

    assert config.media_dirs() == {
        "uploads": Path("/mnt/big-disk/vt/uploads"),
        "audio": Path("/mnt/big-disk/vt/audio"),
        "video": Path("/mnt/big-disk/vt/video"),
        "x_downloads": Path("/mnt/big-disk/vt/x-downloads"),
    }


def test_databases_do_not_follow_the_media_root(monkeypatch):
    """SQLite over a network share fails to lock — verified on this project's own
    NAS mount, where the backend would not even start. DATA_ROOT is meant to point
    at exactly such a disk, so chaining the databases to it would turn a sensible
    config change into a backend that refuses to boot."""
    monkeypatch.setenv("DATA_ROOT", "/mnt/big-disk/vt")
    monkeypatch.delenv("X_BOOKMARKS_DB", raising=False)

    import x_bookmarks

    assert "/mnt/big-disk/vt" not in str(x_bookmarks._resolve_db_path())


# ---------------------------------------------------------------------------
# Saved settings, edited from the panel
# ---------------------------------------------------------------------------

def test_saved_settings_win_over_the_environment(settings_file, monkeypatch):
    """The file is what the user just typed into the panel; it must take effect."""
    monkeypatch.setenv("DATA_ROOT", "/from/env")
    config.save_settings({"data_root": "/from/panel"})

    assert config.data_root() == Path("/from/panel")


def test_the_environment_still_rules_when_nothing_was_saved(monkeypatch):
    """Nobody's existing setup breaks just because the panel exists."""
    monkeypatch.setenv("DATA_ROOT", "/from/env")

    assert config.data_root() == Path("/from/env")


def test_a_corrupt_settings_file_is_ignored_rather_than_fatal(settings_file, monkeypatch):
    """A broken file must not stop the backend from starting."""
    settings_file.write_text("{ this is not json")
    monkeypatch.setenv("DATA_ROOT", "/from/env")

    assert config.data_root() == Path("/from/env")


def test_log_level_follows_the_same_precedence(settings_file, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert config.log_level() == "WARNING"

    config.save_settings({"log_level": "DEBUG"})
    assert config.log_level() == "DEBUG"


def test_saving_keeps_the_settings_that_were_already_there(settings_file):
    config.save_settings({"data_root": "/keep/me"})
    config.save_settings({"log_level": "DEBUG"})

    assert json.loads(settings_file.read_text())["data_root"] == "/keep/me"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validation_accepts_a_writable_absolute_path(tmp_path):
    assert config.validate_settings({"data_root": str(tmp_path)}) == {}


def test_validation_rejects_a_relative_path():
    """Relative to what? The backend's cwd is not visible from the browser."""
    errors = config.validate_settings({"data_root": "some/where"})

    assert "data_root" in errors


def test_validation_rejects_a_path_it_cannot_write_to():
    errors = config.validate_settings({"data_root": "/proc/nope/vt"})

    assert "data_root" in errors


def test_validation_accepts_a_directory_that_does_not_exist_yet(tmp_path):
    """Naming a new directory is normal; it gets created on save."""
    assert config.validate_settings({"data_root": str(tmp_path / "brand-new")}) == {}


def test_validation_rejects_an_invented_log_level():
    errors = config.validate_settings({"log_level": "CHATTY"})

    assert "log_level" in errors
