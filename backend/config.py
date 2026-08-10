"""Where media files live.

One downloaded X video weighed 681 MB, so the directories holding media must be
movable off the repo — and off this disk — without touching code. `DATA_ROOT`
does that; it defaults to `backend/`, so an unset environment keeps every file
exactly where it is today.

The SQLite databases and the X credentials deliberately do NOT hang off this
root. SQLite cannot lock over a network share (this project's own NAS mount
refuses to open `jobs.db` at all), and `DATA_ROOT` is meant to point at exactly
that kind of disk — chaining the databases to it would turn a reasonable config
change into a backend that will not start. They keep their own variables:
`JOBS_DB_PATH`, `X_BOOKMARKS_DB` and `X_DATA_DIR`.
"""

import json
import logging
import os
from pathlib import Path

# Kept next to this file, deliberately NOT under DATA_ROOT: a settings file
# living inside the root it defines would become unreachable the moment someone
# changed that root.
SETTINGS_PATH = Path(__file__).parent / "settings.json"

_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def load_settings() -> dict:
    """Settings saved from the panel. A missing or corrupt file reads as empty.

    Swallowing a broken file is deliberate: configuration should never be the
    reason the backend refuses to start, and the environment still provides
    every value underneath.
    """
    try:
        loaded = json.loads(SETTINGS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_settings(changes: dict) -> dict:
    """Merge *changes* into the saved settings and write them back."""
    settings = load_settings() | changes
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    return settings


def _setting(name: str, env_var: str, default: str) -> tuple[str, str]:
    """Resolve one setting, returning its value and where it came from.

    Saved settings beat the environment: the file holds what the user just typed
    into the panel, so it has to win, while an unset key leaves whatever the
    environment already dictated untouched.
    """
    saved = load_settings().get(name)
    if saved:
        return str(saved), "settings"
    from_env = os.getenv(env_var)
    if from_env:
        return from_env, "environment"
    return default, "default"


def data_root() -> Path:
    """Root of every media directory. Defaults to this file's directory."""
    return Path(_setting("data_root", "DATA_ROOT", str(Path(__file__).parent))[0])


def log_level() -> str:
    """Backend log level. DEBUG adds the per-round trace of the X scroll."""
    return _setting("log_level", "LOG_LEVEL", "INFO")[0].upper()


def describe() -> dict:
    """Effective settings plus the origin of each — the panel shows both, so it
    can tell a value someone chose from one that is merely the default."""
    value, origin = _setting("data_root", "DATA_ROOT", str(Path(__file__).parent))
    level, level_origin = _setting("log_level", "LOG_LEVEL", "INFO")
    return {
        "data_root": {"value": value, "origin": origin},
        "log_level": {"value": level.upper(), "origin": level_origin},
        "directories": {k: str(v) for k, v in media_dirs().items()},
        # uploads/audio/video are frozen into module constants at import time;
        # only these two take effect without a restart.
        "applies_immediately": ["log_level", "x_downloads"],
    }


def validate_settings(changes: dict) -> dict:
    """Check proposed settings, returning {field: reason} for whatever is wrong.

    Pure and side-effect free apart from probing the filesystem, so the rules can
    be tested without going through HTTP.
    """
    errors: dict[str, str] = {}

    if "data_root" in changes:
        raw = str(changes["data_root"]).strip()
        path = Path(raw)
        if not raw:
            errors["data_root"] = "la ruta no puede estar vacía"
        elif not path.is_absolute():
            # Relative to what? The backend's cwd isn't visible from the browser.
            errors["data_root"] = "la ruta debe ser absoluta (empezar por /)"
        elif path.exists() and not path.is_dir():
            errors["data_root"] = "esa ruta existe y no es un directorio"
        else:
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".vt-write-test"
                probe.touch()
                probe.unlink()
            except OSError as exc:
                errors["data_root"] = f"no se puede escribir ahí: {exc.strerror or exc}"

    if "log_level" in changes:
        if str(changes["log_level"]).upper() not in _LOG_LEVELS:
            errors["log_level"] = f"nivel desconocido; usa uno de {', '.join(_LOG_LEVELS)}"

    return errors


def apply_log_level() -> None:
    """Push the current level onto the root logger, without a restart."""
    logging.getLogger().setLevel(log_level())


def media_dirs() -> dict[str, Path]:
    """The media directories, resolved against the current root."""
    root = data_root()
    return {
        "uploads": root / "uploads",
        "audio": root / "audio",
        "video": root / "video",
        # Kept apart from X_DATA_DIR, which holds the cookies: credentials have
        # no business travelling with gigabytes of video.
        "x_downloads": root / "x-downloads",
    }
