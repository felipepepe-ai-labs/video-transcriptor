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


class DownloadFailed(SyncError):
    """yt-dlp could not download a bookmark's video."""


class NoMediaFound(SyncError):
    """The tweet simply has no video.

    Deliberately not a DownloadFailed: not every bookmark is a video, so this is
    an ordinary outcome that settles the bookmark in 'no_media' rather than an
    error the user has to fix.
    """


# ── Imports ──

import collections
import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from x_bookmarks import DATA_DIR, download_dir, init_db, sync_bookmarks

init_db()  # ensure directory structure exists before any operation

logger = logging.getLogger(__name__)

# Cookie values are credentials: log names and domains, never values.


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
                # Keep the leading dot: in Netscape format ".x.com" means "x.com
                # and its subdomains", and Playwright honours that distinction.
                # Stripping it made every cookie host-only, so none reached
                # api.x.com — where the GraphQL calls go — and a perfectly live
                # session answered 401 on every request.
                "domain": domain,
                "path": path,
                "name": name,
                "value": value,
                "httpOnly": http_only.lower() == "true",
                "secure": secure_str.lower() == "true",
                # Playwright wants -1 for a session cookie, not a null expiry.
                "expires": int(expiry) if expiry.isdigit() and int(expiry) > 0 else -1,
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
    # session.json holds auth_token in clear: as sensitive as the jar itself.
    os.chmod(str(session_path), 0o600)

    # yt-dlp reads this jar directly, so keep the export verbatim rather than
    # re-serialising it — a lossy rewrite silently produces an unusable file.
    shutil.copyfile(Path(netscape_path), cookies_file)
    os.chmod(str(cookies_file), 0o600)

    # The per-domain breakdown is the tell for the host-only bug that made every
    # live session answer 401: cookies without a leading dot never reach api.x.com.
    by_domain = dict(collections.Counter(c["domain"] for c in cookies))
    logger.info(
        "imported %d X cookies into %s — domains: %s, required present: %s",
        len(cookies),
        session_path,
        by_domain,
        sorted(found),
    )

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


# A bookmark card links to its tweet several times over — the timestamp, the
# photo, the analytics page — and all of them carry the same status id.
_STATUS_URL = re.compile(r"^(https?://[^/]+/[^/]+/status/\d+)")


def _permalink_from(url: str | None) -> str | None:
    """Reduce a tweet link to its canonical permalink, or None if it isn't one.

    The card's first anchor is the author's profile, and feeding that to yt-dlp
    earns an "Unsupported URL" — a profile page is not a video. Everything past
    the status id (``/photo/1``, ``/analytics``, query strings) is noise.
    """
    if not url:
        return None
    match = _STATUS_URL.match(url)
    return match.group(1) if match else None


def _extract_tweets(page) -> list[dict]:
    """Extract visible tweet data from the current DOM via JavaScript evaluation.

    Uses x.com's ARIA-based selectors:
    - Tweet container: ``[data-testid='tweet']``
    - Author: ``[data-testid='User-Name']``
    - Text: ``[data-testid='tweetText']`` (the tweet body)
    - Thumbnail/media: ``[data-testid='tweetPhoto']`` or video preview images

    Returns a list of dicts with keys matching the bookmark DB schema.
    """
    scraped = page.evaluate("""
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

                // The permalink is the anchor wrapping the timestamp. The card's
                // *first* anchor is the author's avatar, which points at their
                // profile — useless to yt-dlp.
                const statusLinks = Array.from(node.querySelectorAll("a[href*='/status/']"));
                const permalink =
                    (statusLinks.find(a => a.querySelector('time')) || statusLinks[0])?.href
                    ?? null;

                // Expand long tweets / articles by clicking "Show more"
                let expanded_text = textEl?.innerText?.trim() || null;
                if (!expanded_text) {
                    expanded_text = permalink;
                }

                results.push({
                    tweet_url: permalink,
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

    # Normalise in Python rather than in the page: it is the one part with logic
    # of its own, and here it can be unit-tested. Cards without a permalink are
    # loading skeletons or ads — dropping them lets sync_bookmarks' empty-url
    # guard do the rest.
    for tweet in scraped:
        tweet["tweet_url"] = _permalink_from(tweet.get("tweet_url"))
    return [tweet for tweet in scraped if tweet["tweet_url"]]


# X's GDPR dialog carries no data-testid and follows the account locale, so its
# visible text is the only handle there is. Refusing comes first: closing the
# dialog is all the scrape needs, and there's no reason to opt into tracking.
_CONSENT_BUTTON_TEXTS = (
    "Rechazar cookies no necesarias",
    "Refuse non-essential cookies",
    "Aceptar todas las cookies",
    "Accept all cookies",
)
_CONSENT_TIMEOUT_MS = 3_000

# The timeline is polled in short rounds rather than waited on once, so the
# banner can be dismissed whenever it decides to show up.
_TIMELINE_ROUNDS = 10
_TIMELINE_ROUND_MS = 2_000


def _dismiss_cookie_banner(page) -> bool:
    """Close X's cookie consent dialog, which blocks the timeline from hydrating.

    Only one label can match a given locale, so each candidate is checked with
    ``count()`` before clicking: probing blind would spend the click timeout on
    three candidates that will never exist.

    Returns whether a banner was dismissed. Its absence is normal — X doesn't
    show it in every region, nor once a consent cookie exists — so it is never
    an error, and failures here are swallowed: the scrape is better off trying
    the timeline than dying on the doorstep.
    """
    for text in _CONSENT_BUTTON_TEXTS:
        locator = page.get_by_text(re.compile(re.escape(text), re.I))
        try:
            if not locator.count():
                continue
            locator.first.click(timeout=_CONSENT_TIMEOUT_MS)
            logger.info("dismissed X's cookie consent dialog via %r", text)
            return True
        except Exception:
            continue
    return False


def _await_timeline(page) -> None:
    """Wait for the bookmarks timeline, dismissing the consent banner as needed.

    The banner surfaces at an unpredictable moment — measured against x.com, it
    was still absent 4s after load on two of three tries — so dismissing it once
    up front races it and loses. Retrying between short waits absorbs that, and
    costs nothing extra when the timeline shows up first.

    Raises the underlying timeout when the timeline never arrives, so the caller
    can still tell an expired session from a markup change.
    """
    last_error: Exception | None = None
    for round_num in range(1, _TIMELINE_ROUNDS + 1):
        try:
            page.wait_for_selector("[data-testid='tweet']", timeout=_TIMELINE_ROUND_MS)
            logger.info("bookmarks timeline rendered after %d round(s)", round_num)
            return
        except Exception as exc:
            last_error = exc
            logger.info(
                "timeline not up yet (round %d/%d), retrying the consent dialog",
                round_num,
                _TIMELINE_ROUNDS,
            )
            _dismiss_cookie_banner(page)

    logger.warning("timeline never rendered after %d rounds", _TIMELINE_ROUNDS)
    raise last_error


def _watch_for_auth_failure(page) -> list[int]:
    """Record failed X API responses, so a dead session can be named as such.

    Returns a list that fills up as the page navigates: X answers 401 to every
    GraphQL call once the session cookies stop being valid, which is otherwise
    indistinguishable from a selector change.

    Only 401 counts. A working session still draws the odd 403 on secondary
    calls while the timeline loads and the scrape succeeds, so treating those as
    a dead session would be the same false diagnosis in reverse.
    """
    unauthorized: list[int] = []

    def on_response(response) -> None:
        if "/graphql/" not in response.url:
            return
        if response.status in (401, 403, 429):
            # Logged whatever the status: reporting an odd response and concluding
            # the session is dead are different jobs.
            logger.warning(
                "X API answered %d for %s", response.status, response.url.split("/")[-1][:60]
            )
        if response.status == 401:
            unauthorized.append(response.status)

    page.on("response", on_response)
    return unauthorized


def _explain_scrape_failure(unauthorized: list[int], exc: Exception) -> str:
    """Turn a scrape failure into a message that says what to actually do.

    Everything used to surface as `Page.wait_for_selector: Timeout 15000ms
    exceeded`, which cannot tell an expired session from a DOM change and sends
    whoever reads it on a long hunt.
    """
    if unauthorized:
        return (
            "X session expired (the API answered "
            f"{unauthorized[0]}) — re-import your cookies via POST /x/import-cookies"
        )
    if "Timeout" in str(exc):
        return (
            "the bookmarks timeline never rendered; X may have changed its "
            f"markup, or the page failed to load: {exc}"
        )
    return f"scraping failed: {exc}"


def _count_tweets(page) -> int:
    """How many tweet cards are currently rendered, for progress reporting."""
    return page.evaluate("document.querySelectorAll(\"[data-testid='tweet']\").length")


def _scroll_until_stable(page, on_progress=None) -> bool:
    """Scroll the bookmarks page until no new content appears.

    Strategy (from x-bookmarks-curator):
    - Scroll up to 40 rounds.
    - After each scroll, pause ``_SCROLL_PAUSE`` seconds for content to render.
    - Track page height after each round; if height stays the same for
      ``_SCROLL_STABLE_ROUNDS`` consecutive rounds, consider loading done.

    ``on_progress`` is an optional callback taking keyword arguments; at 1.2s per
    round this loop can run for the better part of a minute, so without it the UI
    has nothing to show but a frozen message. Kept a plain callback rather than an
    import of the progress registry, so this module stays a pure scraping seam.

    Returns True if stable (new content found), False if no more content appeared.
    """
    prev_height = 0
    stable_count = 0

    for round_num in range(1, _SCROLL_ROUNDS_MAX + 1):
        # Scroll to bottom (then back up a bit so infinite scroll refires).
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(_SCROLL_PAUSE)

        curr_height = page.evaluate("document.body.scrollHeight")

        # Guarded, not just level-gated: the arguments themselves cost a DOM query
        # per round, and this is the noisiest loop in the module (up to 40 rounds).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "scroll round %d: height=%s, tweets=%s",
                round_num,
                curr_height,
                _count_tweets(page),
            )

        if on_progress:
            found = _count_tweets(page)
            on_progress(
                message=f"Cargando bookmarks… ({found} encontrados)",
                round=round_num,
                found=found,
            )

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


def sync_x_bookmarks(session_path: str | Path, db, on_progress=None) -> list[dict]:
    """Scrape X bookmarks using a saved Playwright session.

    Parameters
    ----------
    session_path :
        Path to the Playwright ``storageState`` JSON (produced by ``import_cookies``).
    db :
        SQLite connection or module-level handle for ``sync_bookmarks()``.
    on_progress :
        Optional keyword-argument callback reporting how far the scrape has got.
        The scroll phase alone can run for the better part of a minute, so a caller
        driving a UI needs this to say anything truthful while it waits.

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
    # Validate the session before reaching for playwright: needing a browser
    # installed just to be told the cookies were never imported is unhelpful.
    session_path = Path(session_path)
    if not session_path.exists():
        raise ScrapingError(f"session file not found: {session_path}")

    storage_state = json.loads(session_path.read_text())
    if not storage_state.get("cookies"):
        raise ScrapingError("session has no cookies — import cookies first")

    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ScrapingError("playwright is not installed; run: pip install playwright && playwright install chromium") from exc

    logger.info(
        "starting X bookmarks scrape with %d cookies from %s",
        len(storage_state["cookies"]),
        session_path,
    )

    results: list[dict] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        unauthorized = _watch_for_auth_failure(page)

        try:
            page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded", timeout=30_000)

            # Wait for the bookmarks list container, clearing the consent dialog
            # if it appears: while that dialog is up the timeline never hydrates.
            _await_timeline(page)

            if on_progress:
                on_progress(message="Bookmarks cargados, recorriendo la lista…")

            # Scroll until stable.
            _scroll_until_stable(page, on_progress=on_progress)

            # A second mini-scroll pass in case lazy-loading missed something.
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(_SCROLL_PAUSE)

            results = _extract_tweets(page)
            logger.info("extracted %d bookmarks from the timeline", len(results))
        except Exception as exc:
            logger.warning("scrape failed: %s", exc)
            raise ScrapingError(_explain_scrape_failure(unauthorized, exc)) from exc
        finally:
            browser.close()

    if not results:
        # With a 401 on record this is not a guess, so say so outright.
        raise ScrapingError(
            _explain_scrape_failure(unauthorized, RuntimeError("no bookmarks found"))
            if unauthorized
            else "no bookmarks found — session may have expired"
        )

    if on_progress:
        on_progress(
            message=f"{len(results)} bookmarks encontrados, guardando…",
            found=len(results),
        )

    # Persist to DB and return.
    count = sync_bookmarks(db, results)
    return results


# ---------------------------------------------------------------------------
# Download helper (yt-dlp via Python subprocess, matching curator's approach)
# ---------------------------------------------------------------------------

# yt-dlp's progress output, as emitted with --newline. Verbatim samples:
#   [download]   4.4% of  337.53KiB at  852.37KiB/s ETA 00:00
#   [download] 100% of  337.53KiB in 00:00:00 at 2.36MiB/s
_YTDLP_PROGRESS = re.compile(
    r"^\[download\]\s+(?P<percent>\d+(?:\.\d+)?)%\s+of\s+~?\s*(?P<size>\S+)"
    r"(?:\s+at\s+(?P<speed>\S+))?(?:\s+in\s+\S+\s+at\s+(?P<final_speed>\S+))?"
)
_YTDLP_FORMATS = re.compile(r"Downloading (\d+) format\(s\)")

# The deadline is on silence, not on total duration: a large file over a slow
# line legitimately takes as long as it takes, whereas yt-dlp saying nothing at
# all never becomes healthy again.
_YTDLP_STALL_SECONDS = 120


def _parse_progress_line(line: str) -> dict | None:
    """Turn one line of yt-dlp output into a progress fact, or None if it says nothing.

    Pure on purpose: the fiddly parts — two shapes of percent line, the format
    count, the start of each pass — are then testable without a subprocess.
    """
    line = line.strip()
    if not line:
        return None

    match = _YTDLP_PROGRESS.match(line)
    if match:
        return {
            "percent": float(match.group("percent")),
            "size": match.group("size"),
            "speed": match.group("speed") or match.group("final_speed"),
        }

    formats = _YTDLP_FORMATS.search(line)
    if formats:
        return {"formats": int(formats.group(1))}

    if line.startswith("[download] Destination:"):
        return {"pass_started": True}

    if "Merging formats" in line:
        return {"merging": True}

    return None


class _ProgressTracker:
    """Folds yt-dlp's per-pass percentages into one that only ever goes up.

    With `bestvideo+bestaudio` yt-dlp downloads two files, counting 0-100 for
    each. Reporting that raw sends the bar back to zero halfway through, which
    reads as a restart — worse than showing nothing at all.
    """

    def __init__(self) -> None:
        self.total_passes = 1
        self.completed_passes = -1  # the first Destination line makes this 0

    def feed(self, line: str) -> dict | None:
        fact = _parse_progress_line(line)
        if fact is None:
            return None

        if "formats" in fact:
            self.total_passes = max(1, fact["formats"])
            return None

        if fact.get("pass_started"):
            self.completed_passes += 1
            return None

        if fact.get("merging"):
            return {"message": "Uniendo pistas…"}

        done = max(0, self.completed_passes)
        overall = (done + fact["percent"] / 100) / self.total_passes * 100
        return {
            "percent": round(min(overall, 100.0), 1),
            "size": fact["size"],
            "speed": fact["speed"],
        }


def download_video(
    tweet_url: str,
    cookies_path: str | Path,
    dest_dir: Path | None = None,
    on_progress=None,
    stall_timeout: float = _YTDLP_STALL_SECONDS,
) -> str:
    """Download a video from the given tweet using yt-dlp.

    Returns the path to the downloaded file.

    Raises
    ------
    NoMediaFound
        The tweet has no video — an ordinary outcome, not an error.
    DownloadFailed
        yt-dlp failed, went ``stall_timeout`` seconds without saying anything,
        or reported success without writing anything.
    """
    # Explicit dest_dir wins (tests inject one); otherwise DATA_ROOT decides.
    dest_dir = dest_dir or download_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Use yt-dlp directly (already a dependency of the app).
    import subprocess  # noqa: F811 (late import to avoid cold-start cost)

    # Snapshot the directory so we can tell which file *this* call produced,
    # rather than handing back an unrelated earlier download.
    before = set(dest_dir.iterdir())

    cmd = [
        "yt-dlp",
        "--cookies", str(cookies_path),
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
        "--merge-output-format", "mp4",
        # --newline: without it yt-dlp repaints one line with \r, and reading
        # by lines would block until the download finished.
        "--newline",
        "-o", str(dest_dir / "%(id)s.%(ext)s"),
        tweet_url,
    ]

    logger.info("yt-dlp downloading %s into %s", tweet_url, dest_dir)

    # Popen rather than run(): run() only hands back its output once the process
    # is over, so there would be nothing to report while a 681 MB file downloads.
    # Both streams go to one pipe — reading two of them from a single thread
    # deadlocks as soon as the one you are not reading fills up.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line buffered, so progress arrives as it happens
    )

    # Iterating the pipe directly is what makes live progress possible, but it
    # also blocks with no deadline: a wedged yt-dlp would hang this worker for
    # good. So a reader thread does the blocking, and this one waits on a queue
    # it *can* put a timeout on. The sentinel marks end of output.
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for line in proc.stdout:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=pump, daemon=True).start()

    tracker = _ProgressTracker()
    output_lines: list[str] = []
    try:
        while True:
            try:
                line = lines.get(timeout=stall_timeout)
            except queue.Empty:
                # Kill rather than just give up: subprocess.run() used to do this
                # on timeout, and an orphaned yt-dlp would keep pulling against X.
                proc.kill()
                raise DownloadFailed(
                    f"yt-dlp stalled: no output for {stall_timeout}s "
                    f"downloading {tweet_url}"
                )
            if line is None:
                break
            output_lines.append(line)
            if on_progress:
                update = tracker.feed(line)
                if update:
                    on_progress(**update)
        proc.wait(timeout=stall_timeout)
    finally:
        proc.stdout.close()

    stderr = "".join(output_lines)
    logger.info("yt-dlp exited %d for %s", proc.returncode, tweet_url)
    if stderr.strip():
        logger.debug("yt-dlp output: %s", stderr.strip()[-1000:])
    # Same test as the curator's download.js: only these two phrasings mean
    # "this tweet is not a video". A generic 'missing' is a real failure.
    if re.search(r"no video|no media found", stderr, re.IGNORECASE):
        raise NoMediaFound(f"no video in {tweet_url}")
    if proc.returncode != 0:
        raise DownloadFailed(f"yt-dlp exited {proc.returncode}: {stderr.strip()[:500]}")

    written = [
        p
        for p in dest_dir.iterdir()
        if p not in before
        and p.suffix in (".mp4", ".mkv", ".webm")
        and p.stat().st_size > 0
    ]
    if not written:
        raise DownloadFailed(f"yt-dlp reported success but wrote no file for {tweet_url}")

    return str(max(written, key=lambda p: p.stat().st_mtime))
