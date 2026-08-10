"""The /config endpoints behind the settings panel.

The panel writes a filesystem path, so the validation that guards it is the
interesting part: a bad value must be refused *and* leave the stored settings
untouched, or one typo would strand every future download.
"""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    return TestClient(app_module.app)


def test_config_reports_the_effective_values(client):
    body = client.get("/config").json()

    assert body["data_root"]["value"]
    assert body["log_level"]["value"] in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def test_config_says_where_each_value_came_from(client, monkeypatch):
    """A default and a deliberate choice look the same otherwise."""
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    body = client.get("/config").json()

    assert body["log_level"]["origin"] == "environment"


def test_config_lists_the_resolved_directories(client):
    """The panel shows real paths rather than guessing at them."""
    dirs = client.get("/config").json()["directories"]

    assert set(dirs) == {"uploads", "audio", "video", "x_downloads"}


def test_saving_a_setting_takes_effect(client, tmp_path):
    target = tmp_path / "media"

    response = client.put("/config", json={"data_root": str(target)})

    assert response.status_code == 200
    body = client.get("/config").json()
    assert body["data_root"]["value"] == str(target)
    assert body["data_root"]["origin"] == "settings"


def test_saving_creates_the_directory(client, tmp_path):
    target = tmp_path / "brand-new"

    client.put("/config", json={"data_root": str(target)})

    assert target.is_dir()


def test_a_relative_path_is_refused_with_a_reason(client):
    response = client.put("/config", json={"data_root": "some/where"})

    assert response.status_code == 400
    assert "data_root" in response.json()["detail"]


def test_a_refused_value_is_not_written(client, tmp_path):
    """Half-applying an invalid save would be worse than refusing it."""
    good = tmp_path / "good"
    client.put("/config", json={"data_root": str(good)})

    client.put("/config", json={"data_root": "relative/path"})

    assert config.load_settings()["data_root"] == str(good)


def test_an_unknown_log_level_is_refused(client):
    response = client.put("/config", json={"log_level": "CHATTY"})

    assert response.status_code == 400
    assert "log_level" in response.json()["detail"]


def test_the_log_level_changes_without_a_restart(client):
    client.put("/config", json={"log_level": "DEBUG"})

    assert logging.getLogger().level == logging.DEBUG


def test_the_response_names_what_needs_a_restart(client, tmp_path):
    """uploads/audio/video are frozen at import time; saying so beats pretending."""
    response = client.put("/config", json={"data_root": str(tmp_path / "m")})

    assert "restart_required" in response.json()


# ---------------------------------------------------------------------------
# Browsing for a folder
# ---------------------------------------------------------------------------
# A browser cannot reveal a real filesystem path, so picking a folder on the
# server means the server has to list it.

@pytest.fixture
def tree(tmp_path):
    (tmp_path / "videos").mkdir()
    (tmp_path / "audios").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "readme.txt").write_text("not a directory")
    return tmp_path


def test_browsing_lists_only_directories(client, tree):
    body = client.get("/config/browse", params={"path": str(tree)}).json()

    assert [e["name"] for e in body["entries"]] == ["audios", "videos"]


def test_browsing_hides_dot_directories(client, tree):
    body = client.get("/config/browse", params={"path": str(tree)}).json()

    assert not any(e["name"].startswith(".") for e in body["entries"])


def test_browsing_gives_each_entry_a_full_path(client, tree):
    """The UI hands this straight back as the new root, so it must be absolute."""
    body = client.get("/config/browse", params={"path": str(tree)}).json()

    assert body["entries"][0]["path"] == str(tree / "audios")


def test_browsing_points_at_the_parent(client, tree):
    body = client.get("/config/browse", params={"path": str(tree)}).json()

    assert body["parent"] == str(tree.parent)


def test_the_root_has_no_parent(client):
    """So the UI knows when to stop offering "up"."""
    body = client.get("/config/browse", params={"path": "/"}).json()

    assert body["parent"] is None


def test_browsing_defaults_to_the_home_directory(client):
    body = client.get("/config/browse").json()

    assert body["path"] == str(Path.home())


def test_browsing_a_missing_directory_is_a_404(client, tmp_path):
    response = client.get("/config/browse", params={"path": str(tmp_path / "nope")})

    assert response.status_code == 404


def test_browsing_a_file_is_a_404(client, tree):
    response = client.get("/config/browse", params={"path": str(tree / "readme.txt")})

    assert response.status_code == 404


def test_an_unreadable_directory_answers_403_not_a_traceback(client, tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o000)
    try:
        response = client.get("/config/browse", params={"path": str(locked)})
        assert response.status_code == 403
    finally:
        locked.chmod(0o755)  # so pytest can clean it up
