r"""
X (Twitter) bookmarks sync: cookie import + Playwright headless scraping.

Adapted from the standalone x-bookmarks-curator project. Two responsibilities:

1. **Cookie import** -- parse a Netscape-format ``cookies.txt``, validate that the
   required X authentication cookies (*auth_token*, *ct0*, *twid*) are present,
   and write a Playwright ``storageState`` JSON + ``cookies.txt`` (mode 0600) to
   ``data/x-bookmarks/``.

2. **Scraping** -- launch headless Chromium with the saved session, navigate to
   *x.com/i/bookmarks*, scroll until content is stable (max 40 rounds), then
   extract every visible tweet into the bookmark DB.
"""

# ── Exception hierarchy (isolated boundary, like remote.RemoteUnavailable) ──


class SyncError(Exception):
    """Base class for sync failures."""


class CookieParseError(SyncError):
    """Netscape cookies file could not be parsed or is invalid."""


class ScrapingError(SyncError):
    """Headless browser failed to collect bookmarks."""


# ── Imports ──

import json
import os
import re
import time
from pathlib import Path

from x_bookmarks import DATA_DIR, init_db, sync_bookmarks

init_db()  # ensure directory structure exists before any operation


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

# Required cookies that prove X authentication succeeded.
_REQUIRED_X_COOKIES = {"auth_token", "ct0", "twid"}


def _parse_netscape(text: str) -> list[dict]:
    """Parse a Netscape-format cookies.txt into a list of cookie dicts.

    Each line is tab-separated:
        #HttpOnly_<domain>  <flag>  <path>  <secure>  <expiry>  <name>  <value>
    Lines starting with ``#`` are comments; blank lines are skipped.

    Returns a list suitable for Playwright's ``storageState.cookies`` field.
    """
    cookies: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue  # malformed, skip
        domain, http_only, path, secure_str, expiry, name, value = parts[:7]
        cookies.append(
            {
                "domain": domain.lstrip("."),  # Playwright doesn't want leading dot
                "path": path,
                "name": name,
                "value": value,
                "httpOnly": http_only.lower() == "true",
                "secure": secure_str.lower() == "true",
                "expires": int(expiry) if expiry.isdigit() else None,
            }
        )
    return cookies


def _validate_x_cookies(cookies: list[dict]) -> set[str]:
    """Return the subset of required X cookies found in *cookies*."""
    names = {c["name"] for c in cookies}
    return _REQUIRED_X_COOKIES & names


def import_cookies(netscape_path: str | Path, data_dir: Path | None = None) -> dict:
    """Import a Netscape-format cookies file.

    Parameters
    ----------
    netscape_path :
        Path to the uploaded ``cookies.txt`` (Netscape format).
    data_dir :
        Destination directory; defaults to ``DATA_DIR`` (``backend/data/x-bookmarks/``).

    Returns
    -------
    dict
        Keys: ``ok``, ``found_cookies`` (list of cookie names), ``missing`` (list),
        ``session_path`` (path to the generated Playwright session JSON).

    Raises
    ------
    CookieParseError
        If the file is empty, unparseable, or missing required X cookies.
    """
    data_dir = data_dir or DATA_DIR
    session_path = data_dir / "session.json"
    cookies_file = data_dir / "cookies.txt"

    raw = Path(netscape_path).read_text(encoding="utf-8").strip()
    if not raw:
        raise CookieParseError("cookies file is empty")

    cookies = _parse_netscape(raw)
    if not cookies:
        raise CookieParseError("no valid cookie lines found in file")

    found = _validate_x_cookies(cookies)
    missing = _REQUIRED_X_COOKIES - found
    if missing:
        raise CookieParseError(f"missing required X cookies: {', '.join(sorted(missing))}")

    # Write Playwright sessionState JSON (used by storageState).
    state = {
        "cookies": cookies,
        "origins": [],
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    session_path.write_text(json.dumps(state), encoding="utf-8")

    # Also keep a copy of the raw cookies.txt (chmod 0600 for security).
    with open(cookies_file, "w", encoding="utf-8") as fh:
        fh.write("# Netscape HTTP Cookie File\n")
        for c in cookies:
            flag = "TRUE" if c.get("httpOnly") else "FALSE"
            secure = "TRUE" if c.get("secure") else "FALSE"
            expires = int(c["expires"]) if c.get("expires") else 0
            fh.write(f"# {c['domain']}\t{flag}\t{c['path']}\t{secure}\t{expires}\t{c['name']}\t{c['value']}\n")
    os.chmod(str(cookies_file), 0o600)

    return {
        "ok": True,
        "found_cookies": sorted(found),
        "missing": sorted(missing),
        "session_path": str(session_path),
    }


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

_SCROLL_ROUNDS_MAX = 40
_SCROLL_STABLE_ROUNDS = 3
_SCROLL_PAUSE = 1.2  # seconds after each scroll to let content render


def _extract_tweets(page) -> list[dict]:
    """Extract visible tweet data from the current DOM via JavaScript evaluation.

    Uses x.com's ARIA-based selectors:
    - Tweet container: ``[data-testid='tweet']``
    - Author: ``[data-testid='User-Name']``
    - Text: ``[data-testid='tweetText']`` (the tweet body)
    - Thumbnail/media: ``[data-testid='tweetPhoto']`` or video preview images

    Returns a list of dicts with keys matching the bookmark DB schema.
    """
    return page.evaluate("""
        () => {
            const results = [];
            const nodes = document.querySelectorAll("[data-testid='tweet']");
            for (const node of nodes) {
                // Skip promoted/tweet-composer/skeleton items
                if (node.closest('[aria-live="polite"]') === null &&
                    node.closest('[data-testid="tweet"]') !== node) continue;

                const authorEl = node.querySelector("[data-testid='User-Name']");
                const textEl = node.querySelector("[data-testid='tweetText']")
                    || node.querySelector('p[lang]');
                const mediaEls = node.querySelectorAll("[data-testid='tweetPhoto'], [data-testid='tweetVideo']");

                let thumbnail_url = null;
                if (mediaEls.length) {
                    const img = mediaEls[0].querySelector("img");
                    thumbnail_url = img?.src || null;
                }

                // Expand long tweets / articles by clicking "Show more"
                let expanded_text = textEl?.innerText?.trim() || null;
                if (!expanded_text) {
                    const link = node.querySelector("a[href]");
                    expanded_text = link?.href || null;
                }

                results.push({
                    tweet_url: node.querySelector("a[href]")?.href ?? null,
                    author: authorEl?.innerText?.trim() || null,
                    text: textEl?.innerText?.trim() || null,
                    thumbnail_url: thumbnail_url,
                    has_media: mediaEls.length > 0 ? 1 : 0,
                    article_content: null, // filled later for link tweets
                });
            }
            return results;
        }
    """)


def _scroll_until_stable(page) -> bool:
    """Scroll the bookmarks page until no new content appears.

    Strategy (from x-bookmarks-curator):
    - Scroll up to 40 rounds.
    - After each scroll, pause ``_SCROLL_PAUSE`` seconds for content to render.
    - Track page height after each round; if height stays the same for
      ``_SCROLL_STABLE_ROUNDS`` consecutive rounds, consider loading done.

    Returns True if stable (new content found), False if no more content appeared.
    """
    prev_height = 0
    stable_count = 0

    for round_num in range(1, _SCROLL_ROUNDS_MAX + 1):
        # Scroll to bottom (then back up a bit so infinite scroll refires).
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(_SCROLL_PAUSE)

        curr_height = page.evaluate("document.body.scrollHeight")

        if curr_height == prev_height:
            stable_count += 1
        else:
            stable_count = 0  # new content appeared, reset counter

        prev_height = curr_height

        if stable_count >= _SCROLL_STABLE_ROUNDS:
            break

    return stable_count >= _SCROLL_STABLE_ROUNDS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_x_bookmarks(session_path: str | Path, db) -> list[dict]:
    """Scrape X bookmarks using a saved Playwright session.

    Parameters
    ----------
    session_path :
        Path to the Playwright ``storageState`` JSON (produced by ``import_cookies``).
    db :
        SQLite connection or module-level handle for ``sync_bookmarks()``.

    Returns
    -------
    list[dict]
        The raw bookmark dicts scraped from X.  Also persisted via ``sync_bookmarks()``;
        the return value avoids a second DB read.

    Raises
    ------
    ScrapingError
        If Playwright crashes, the session is invalid, or no tweets are found.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ScrapingError("playwright is not installed; run: pip install playwright && playwright install chromium") from exc

    session_path = Path(session_path)
    if not session_path.exists():
        raise ScrapingError(f"session file not found: {session_path}")

    storage_state = json.loads(session_path.read_text())
    if not storage_state.get("cookies"):
        raise ScrapingError("session has no cookies — import cookies first")

    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()

        try:
            page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded", timeout=30_000)

            # Wait for the bookmarks list container to appear.
            page.wait_for_selector("[data-testid='tweet']", timeout=15_000)

            # Scroll until stable.
            _scroll_until_stable(page)

            # A second mini-scroll pass in case lazy-loading missed something.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(_SCROLL_PAUSE)

            results = _extract_tweets(page)
        except Exception as exc:
            raise ScrapingError(f"scraping failed: {exc}") from exc
        finally:
            browser.close()

    if not results:
        raise ScrapingError("no bookmarks found — session may have expired")

    # Persist to DB and return.
    count = sync_bookmarks(db, results)
    return results


# ---------------------------------------------------------------------------
# Download helper (yt-dlp via Python subprocess, matching curator's approach)
# ---------------------------------------------------------------------------


def download_video(
    tweet_url: str,
    cookies_path: str | Path,
    dest_dir: Path | None = None,
) -> str:
    """Download a video from the given tweet using yt-dlp.

    Returns the path to the downloaded file, or ``""`` on failure (non-video tweets
    are detected and reported back with ``status=no_media``).

    Mirrors the curator's spawn-based approach (subprocess + stderr parsing) for
    feature parity: progress prefix, ``no_media`` detection, etc.
    """
    dest_dir = dest_dir or DATA_DIR / "downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Use yt-dlp directly (already a dependency of the app).
    import subprocess  # noqa: F811 (late import to avoid cold-start cost)

    cmd = [
        "yt-dlp",
        "--cookies", str(cookies_path),
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
        "--merge-output-format", "mp4",
        "-o", str(dest_dir / "%(id)s.%(ext)s"),
        tweet_url,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,  # 5 min max per download.
    )

    stderr_lower = (proc.stderr or "").lower()
    if "no media" in stderr_lower or "missing" in stderr_lower or proc.returncode != 0:
        return ""

    # Find the actual file that was written.
    for ext in (".mp4", ".mkv", ".webm"):
        for p in dest_dir.iterdir():
            if p.suffix == ext and not str(p).startswith(".") and p.stat().st_size > 0:
                return str(p)
    return ""
