"""Plain-file backup of the X bookmarks store.

The bookmarks live in a SQLite file, which is a fine store and a poor archive:
reading it back needs Python, the schema, and this application still existing.
So every export also writes what a person can open — one Markdown file per
bookmark, plus a single JSON dump that keeps the columns Markdown flattens.

Nothing here is the source of truth; it is a copy, and it is rewritten whole on
every run. That is deliberate: a backup that has to be merged is a backup nobody
trusts.
"""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import config
import x_bookmarks as xb
from media_names import slugify

logger = logging.getLogger(__name__)

# The author and the tweet id, straight out of the canonical permalink that
# x_sync._permalink_from() already normalised.
_STATUS_URL = re.compile(r"^https?://[^/]+/([^/]+)/status/(\d+)")


def backup_dir() -> Path:
    """Where the backup lands. Resolved per call, so DATA_ROOT can move."""
    return config.media_dirs()["x_backup"]


def _handle_and_id(tweet_url: str | None) -> tuple[str | None, str | None]:
    match = _STATUS_URL.match(tweet_url or "")
    return (match.group(1), match.group(2)) if match else (None, None)


def _file_stem(row: dict) -> str:
    """``{fecha}-{handle}-{tweet_id}``, sortable and unique.

    The date leads so the directory sorts chronologically in any file manager.
    The tweet id trails because it is the only part guaranteed to be unique --
    the same author bookmarked twice in the same second would otherwise collide.
    """
    handle, tweet_id = _handle_and_id(row.get("tweet_url"))
    # published_at only exists once the scraper starts reading it; until then
    # scraped_at is the closest honest answer.
    date = (row.get("published_at") or row.get("scraped_at") or "")[:10] or "sin-fecha"
    author = slugify(handle or row.get("author") or "", fallback="sin-autor")
    # A bookmark whose URL never parsed still needs a unique name, and the row
    # id is the only other thing guaranteed to be one.
    suffix = tweet_id or f"id{row['id']}"
    return f"{date}-{author}-{suffix}"


def _front_matter(row: dict) -> str:
    """YAML header. Values go through json.dumps because YAML is a JSON superset.

    Hand-rolling the quoting is what breaks the moment a tweet contains a colon
    or a quotation mark, and neither is exactly rare.
    """
    handle, tweet_id = _handle_and_id(row.get("tweet_url"))
    fields = {
        "url": row.get("tweet_url"),
        "author": row.get("author"),
        "handle": handle,
        "tweet_id": tweet_id,
        "published_at": row.get("published_at"),
        "scraped_at": row.get("scraped_at"),
        "status": row.get("status"),
        "has_media": row.get("has_media"),
        "local_file_path": row.get("local_file_path"),
        "lang": row.get("lang"),
    }
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _markdown(row: dict) -> str:
    """One bookmark as a document: front matter, then the tweet, then its links."""
    # The expanded version is the whole tweet; `text` is the timeline's ~280
    # truncated characters, which is all there is when the permalink failed.
    body = row.get("expanded_text") or row.get("text") or ""
    parts = [_front_matter(row), "", body.strip()]

    links = row.get("links_json")
    if links:
        try:
            parsed = json.loads(links)
        except (TypeError, ValueError):
            parsed = []
        if parsed:
            parts += ["", "## Enlaces", ""]
            parts += [
                f"- [{link.get('text') or link['url']}]({link['url']})"
                for link in parsed
                if link.get("url")
            ]

    if row.get("transcript_original"):
        parts += ["", "## Transcripción", "", row["transcript_original"].strip()]

    return "\n".join(parts).rstrip() + "\n"


def export_bookmarks(dest_dir: Path | str | None = None, db=None) -> dict:
    """Write the whole store out as files. Returns what it wrote.

    Raises whatever the filesystem raises: the caller decides whether a failed
    backup is fatal. For the sync worker it is not -- an unplugged NAS must not
    undo a scrape that worked.
    """
    dest = Path(dest_dir) if dest_dir is not None else backup_dir()
    md_dir = dest / "md"
    md_dir.mkdir(parents=True, exist_ok=True)

    conn = xb._get_conn() if db is None else db
    try:
        # Column names come from the cursor rather than from a row_factory, so a
        # connection handed in by the caller is read, never reconfigured.
        cursor = conn.execute("SELECT * FROM bookmarks ORDER BY id")
        columns = [description[0] for description in cursor.description]
        rows = [dict(zip(columns, values)) for values in cursor.fetchall()]
    finally:
        if db is None:
            conn.close()

    # Written aside and renamed: a dump interrupted halfway is worse than no
    # dump at all, because it looks like one.
    payload = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(rows),
        "bookmarks": rows,
    }
    tmp = dest / "bookmarks.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dest / "bookmarks.json")

    written: set[str] = set()
    for row in rows:
        name = f"{_file_stem(row)}.md"
        (md_dir / name).write_text(_markdown(row), encoding="utf-8")
        written.add(name)

    # A bookmark deleted since the last run would otherwise linger for ever,
    # and the backup would slowly stop describing the store it copies.
    stale = [path for path in md_dir.glob("*.md") if path.name not in written]
    for path in stale:
        path.unlink()

    logger.info(
        "exported %d bookmarks to %s (%d stale file(s) removed)",
        len(rows),
        dest,
        len(stale),
    )
    return {
        "dir": str(dest),
        "json": str(dest / "bookmarks.json"),
        "markdown": len(written),
        "removed": len(stale),
    }
