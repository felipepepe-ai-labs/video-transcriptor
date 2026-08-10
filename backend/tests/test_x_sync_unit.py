"""Unit coverage for X cookie import and the sync error paths.

Ported from the standalone x-bookmarks-curator's cookies.test.js and
sync.failure.test.js. The DOM scraping itself (`_extract_tweets`) is not covered
here: it only has meaning against a live x.com page, exactly as in the original.
"""

import json
import stat

import pytest

import x_sync


NETSCAPE_SAMPLE = "\n".join(
    [
        "# Netscape HTTP Cookie File",
        "# This is a generated file! Do not edit.",
        "",
        ".x.com\tTRUE\t/\tTRUE\t1893456000\tauth_token\tsecret-auth-value",
        ".x.com\tTRUE\t/\tTRUE\t1893456000\tct0\tsecret-csrf-value",
        ".x.com\tTRUE\t/\tFALSE\t0\ttwid\tu%3D12345",
    ]
)


@pytest.fixture
def cookies_file(tmp_path):
    path = tmp_path / "export.txt"
    path.write_text(NETSCAPE_SAMPLE, encoding="utf-8")
    return path


@pytest.fixture
def data_dir(tmp_path):
    target = tmp_path / "x-bookmarks"
    target.mkdir()
    return target


# ---------------------------------------------------------------------------
# Netscape parsing
# ---------------------------------------------------------------------------

def test_parse_netscape_reads_every_cookie_line():
    cookies = x_sync._parse_netscape(NETSCAPE_SAMPLE)

    assert [c["name"] for c in cookies] == ["auth_token", "ct0", "twid"]
    assert cookies[0]["value"] == "secret-auth-value"


def test_parse_netscape_skips_comments_and_blank_lines():
    assert x_sync._parse_netscape("# just a comment\n\n   \n") == []


def test_parse_netscape_skips_malformed_lines():
    """A truncated line must be dropped, not crash the whole import."""
    text = ".x.com\tTRUE\t/\n" + NETSCAPE_SAMPLE

    assert len(x_sync._parse_netscape(text)) == 3


def test_parse_netscape_marks_session_cookies_as_non_expiring():
    """Playwright rejects a null expiry; a session cookie must be -1."""
    cookies = x_sync._parse_netscape(NETSCAPE_SAMPLE)

    twid = next(c for c in cookies if c["name"] == "twid")
    assert twid["expires"] == -1


def test_parse_netscape_reads_the_secure_flag():
    cookies = x_sync._parse_netscape(NETSCAPE_SAMPLE)

    assert next(c for c in cookies if c["name"] == "auth_token")["secure"] is True
    assert next(c for c in cookies if c["name"] == "twid")["secure"] is False


# ---------------------------------------------------------------------------
# import_cookies
# ---------------------------------------------------------------------------

def test_import_cookies_writes_a_playwright_session(cookies_file, data_dir):
    result = x_sync.import_cookies(cookies_file, data_dir=data_dir)

    state = json.loads((data_dir / "session.json").read_text())
    assert {c["name"] for c in state["cookies"]} == {"auth_token", "ct0", "twid"}
    assert result["ok"] is True
    assert result["found_cookies"] == ["auth_token", "ct0", "twid"]


def test_import_cookies_writes_a_usable_yt_dlp_cookie_jar(cookies_file, data_dir):
    """The jar is fed to yt-dlp, so its lines must be cookies, not comments."""
    x_sync.import_cookies(cookies_file, data_dir=data_dir)

    written = (data_dir / "cookies.txt").read_text()
    cookie_lines = [
        line
        for line in written.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(cookie_lines) == 3
    assert all("\t" in line for line in cookie_lines)
    assert any("auth_token\tsecret-auth-value" in line for line in cookie_lines)


def test_import_cookies_restricts_permissions_on_both_credential_files(
    cookies_file, data_dir
):
    """session.json carries auth_token too — it is as sensitive as the jar."""
    x_sync.import_cookies(cookies_file, data_dir=data_dir)

    for name in ("session.json", "cookies.txt"):
        mode = stat.S_IMODE((data_dir / name).stat().st_mode)
        assert mode == 0o600, f"{name} is {oct(mode)}, expected 0o600"


def test_import_cookies_rejects_an_empty_file(tmp_path, data_dir):
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n")

    with pytest.raises(x_sync.CookieParseError, match="empty"):
        x_sync.import_cookies(empty, data_dir=data_dir)


def test_import_cookies_rejects_a_file_with_no_cookie_lines(tmp_path, data_dir):
    only_comments = tmp_path / "comments.txt"
    only_comments.write_text("# Netscape HTTP Cookie File\n# nothing else\n")

    with pytest.raises(x_sync.CookieParseError, match="no valid cookie lines"):
        x_sync.import_cookies(only_comments, data_dir=data_dir)


def test_import_cookies_names_the_missing_x_auth_cookies(tmp_path, data_dir):
    """A logged-out export parses fine but is useless — say which are missing."""
    partial = tmp_path / "partial.txt"
    partial.write_text(".x.com\tTRUE\t/\tTRUE\t1893456000\tauth_token\tv\n")

    with pytest.raises(x_sync.CookieParseError) as exc:
        x_sync.import_cookies(partial, data_dir=data_dir)

    assert "ct0" in str(exc.value) and "twid" in str(exc.value)


def test_import_cookies_never_leaks_a_cookie_value_in_its_result(
    cookies_file, data_dir
):
    """The result is returned over HTTP; it must carry names, never values."""
    result = x_sync.import_cookies(cookies_file, data_dir=data_dir)

    assert "secret-auth-value" not in json.dumps(result)


# ---------------------------------------------------------------------------
# sync_x_bookmarks error paths
# ---------------------------------------------------------------------------

def test_sync_rejects_a_missing_session_file(tmp_path):
    with pytest.raises(x_sync.ScrapingError, match="session file not found"):
        x_sync.sync_x_bookmarks(tmp_path / "absent.json", db=None)


def test_sync_rejects_a_session_without_cookies(tmp_path):
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"cookies": [], "origins": []}))

    with pytest.raises(x_sync.ScrapingError, match="import cookies first"):
        x_sync.sync_x_bookmarks(session, db=None)


# ---------------------------------------------------------------------------
# Scroll progress reporting
# ---------------------------------------------------------------------------

class FakePage:
    """The two `page.evaluate` shapes `_scroll_until_stable` depends on.

    Heights are consumed one per round; tweet counts grow with each scroll so a
    test can assert the reported figure actually moves.
    """

    def __init__(self, heights, counts=None):
        self.heights = list(heights)
        self.counts = list(counts or [])
        self.scrolls = 0

    def evaluate(self, script):
        if "scrollTo" in script:
            self.scrolls += 1
            return None
        if "scrollHeight" in script:
            return self.heights.pop(0) if self.heights else 0
        if "querySelectorAll" in script:
            return self.counts.pop(0) if self.counts else 0
        raise AssertionError(f"unexpected script: {script}")


@pytest.fixture
def instant_scroll(monkeypatch):
    """The real pause is 1.2s per round; a unit test must not wait for it."""
    monkeypatch.setattr(x_sync.time, "sleep", lambda _seconds: None)


def test_scroll_reports_once_per_round(instant_scroll):
    page = FakePage(heights=[100, 200, 200, 200, 200], counts=[1, 2, 3, 4, 5])
    reported = []

    x_sync._scroll_until_stable(page, on_progress=lambda **event: reported.append(event))

    assert [event["round"] for event in reported] == [1, 2, 3, 4, 5]


def test_scroll_reports_how_many_bookmarks_it_has_found(instant_scroll):
    """A frozen message is the whole problem; the number is what shows progress."""
    page = FakePage(heights=[100, 100, 100, 100], counts=[7, 12, 12, 12])
    reported = []

    x_sync._scroll_until_stable(page, on_progress=lambda **event: reported.append(event))

    assert reported[0]["found"] == 7
    assert "7" in reported[0]["message"]


def test_scroll_without_a_reporter_behaves_exactly_as_before(instant_scroll):
    page = FakePage(heights=[100, 100, 100, 100])

    assert x_sync._scroll_until_stable(page) is True
    assert page.scrolls == 4


# ---------------------------------------------------------------------------
# Cookie consent banner
# ---------------------------------------------------------------------------

class FakeLocator:
    """Playwright's locator, reduced to what dismissing the banner needs."""

    def __init__(self, page, matches):
        self._page = page
        self._matches = matches

    @property
    def first(self):
        return self

    def count(self):
        return len(self._matches)

    def wait_for(self, timeout=None):
        if not self._matches:
            raise TimeoutError("locator resolved to no element")

    def click(self, timeout=None):
        if not self._matches:
            raise TimeoutError("locator resolved to no element")
        self._page.clicked.append(self._matches[0])


class BannerPage:
    """A page whose banner button matches (or doesn't) the text regex."""

    def __init__(self, button_texts=()):
        self.button_texts = list(button_texts)
        self.clicked = []

    def get_by_text(self, pattern):
        return FakeLocator(self, [t for t in self.button_texts if pattern.search(t)])


def test_dismisses_the_consent_banner_when_it_is_there():
    """X's GDPR dialog blocks the timeline from hydrating; it must be closed."""
    page = BannerPage(["Aceptar todas las cookies"])

    assert x_sync._dismiss_cookie_banner(page) is True
    assert page.clicked == ["Aceptar todas las cookies"]


def test_reports_no_banner_without_raising():
    """X doesn't show it in every region, and that is not a failure."""
    page = BannerPage([])

    assert x_sync._dismiss_cookie_banner(page) is False


def test_prefers_refusing_cookies_over_accepting_them():
    """Closing the dialog is the goal; there's no reason to opt into tracking."""
    page = BannerPage(["Aceptar todas las cookies", "Rechazar cookies no necesarias"])

    x_sync._dismiss_cookie_banner(page)

    assert page.clicked == ["Rechazar cookies no necesarias"]


def test_dismisses_an_english_banner_too():
    """The banner follows the account locale, and carries no testid to match on."""
    page = BannerPage(["Refuse non-essential cookies"])

    assert x_sync._dismiss_cookie_banner(page) is True


# ---------------------------------------------------------------------------
# Waiting for the timeline while the banner races it
# ---------------------------------------------------------------------------

class TimelinePage(BannerPage):
    """A page whose timeline appears only after `appears_on_round` waits.

    Models the real race: X's consent banner can surface seconds after load, so
    a single dismissal attempt made too early simply misses it.
    """

    def __init__(self, appears_on_round, button_texts=()):
        super().__init__(button_texts)
        self.appears_on_round = appears_on_round
        self.waits = 0

    def wait_for_selector(self, selector, timeout=None):
        self.waits += 1
        if self.waits < self.appears_on_round:
            raise TimeoutError(f"Timeout {timeout}ms exceeded waiting for {selector}")
        return object()


def test_returns_as_soon_as_the_timeline_is_there():
    page = TimelinePage(appears_on_round=1)

    x_sync._await_timeline(page)

    assert page.waits == 1
    assert page.clicked == []  # no banner in the way, nothing to dismiss


def test_keeps_dismissing_the_banner_while_waiting():
    """The banner can show up late; one early attempt races it and loses."""
    page = TimelinePage(appears_on_round=3, button_texts=["Accept all cookies"])

    x_sync._await_timeline(page)

    assert page.waits == 3
    assert page.clicked  # it kept trying instead of giving up after round one


def test_raises_when_the_timeline_never_arrives():
    page = TimelinePage(appears_on_round=999)

    with pytest.raises(TimeoutError):
        x_sync._await_timeline(page)


# ---------------------------------------------------------------------------
# Failure diagnosis
# ---------------------------------------------------------------------------

def test_auth_watcher_records_only_failed_graphql_responses():
    class FakeResponse:
        def __init__(self, status, url):
            self.status, self.url = status, url

    class HookPage:
        def __init__(self):
            self.handler = None

        def on(self, event, handler):
            assert event == "response"
            self.handler = handler

    page = HookPage()
    unauthorized = x_sync._watch_for_auth_failure(page)

    page.handler(FakeResponse(401, "https://api.x.com/graphql/abc/Bookmarks"))
    page.handler(FakeResponse(200, "https://api.x.com/graphql/abc/HomeTimeline"))
    page.handler(FakeResponse(404, "https://abs.twimg.com/bundle.js"))

    assert unauthorized == [401]


def test_expired_session_is_named_as_such():
    """The whole point: a 401 must not surface as a selector timeout."""
    message = x_sync._explain_scrape_failure([401], TimeoutError("Timeout 15000ms exceeded"))

    assert "session expired" in message.lower()
    assert "cookies" in message.lower()


def test_a_timeout_without_a_401_does_not_blame_the_session():
    """Blaming the session for a selector change would cost the same wasted hunt."""
    message = x_sync._explain_scrape_failure([], TimeoutError("Timeout 15000ms exceeded"))

    assert "session expired" not in message.lower()


def test_an_unrecognised_failure_keeps_its_original_text():
    message = x_sync._explain_scrape_failure([], RuntimeError("browser vanished"))

    assert "browser vanished" in message
