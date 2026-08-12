"""Unit coverage for X cookie import and the sync error paths.

Ported from the standalone x-bookmarks-curator's cookies.test.js and
sync.failure.test.js. The DOM scraping itself (`_extract_tweets`) is not covered
here: it only has meaning against a live x.com page, exactly as in the original.
"""

import json
import logging
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


def test_parse_netscape_keeps_the_domain_dot():
    """`.x.com` covers subdomains; flattening it keeps auth off api.x.com, which
    is where every GraphQL call goes — a live session then reads as expired."""
    cookies = x_sync._parse_netscape(NETSCAPE_SAMPLE)

    assert next(c for c in cookies if c["name"] == "auth_token")["domain"] == ".x.com"


def test_parse_netscape_leaves_a_host_only_cookie_host_only():
    """The dot is meaningful in both directions: don't invent one either."""
    host_only = "x.com\tFALSE\t/\tTRUE\t1893456000\tnight_mode\t2"

    cookies = x_sync._parse_netscape(host_only)

    assert cookies[0]["domain"] == "x.com"


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


def test_import_cookies_never_leaks_a_cookie_value_into_the_log(
    cookies_file, data_dir, caplog
):
    """Logs are the other way a credential escapes; names and domains only."""
    with caplog.at_level(logging.DEBUG):
        x_sync.import_cookies(cookies_file, data_dir=data_dir)

    assert "secret-auth-value" not in caplog.text
    assert "secret-csrf-value" not in caplog.text


def test_import_cookies_logs_the_domains_it_stored(cookies_file, data_dir, caplog):
    """The per-domain count is exactly what would have exposed the host-only bug
    that made every X session read as expired."""
    with caplog.at_level(logging.INFO):
        x_sync.import_cookies(cookies_file, data_dir=data_dir)

    assert ".x.com" in caplog.text


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
# Tweet permalinks
# ---------------------------------------------------------------------------

def test_permalink_keeps_a_plain_status_url():
    url = "https://x.com/0xCodila/status/2086547599033536913"

    assert x_sync._permalink_from(url) == url


def test_permalink_rejects_a_profile_url():
    """The bug this fixes: the card's first link is the author's profile, and
    yt-dlp answers 'Unsupported URL' because a profile page is not a video."""
    assert x_sync._permalink_from("https://x.com/0xCodila") is None


@pytest.mark.parametrize(
    "suffix", ["/photo/1", "/analytics", "/video/1", "?s=20&t=abc"]
)
def test_permalink_trims_the_sub_pages_of_the_same_tweet(suffix):
    """A card carries several /status/ links; they all point at one tweet."""
    base = "https://x.com/alexconia/status/2086511274100461642"

    assert x_sync._permalink_from(base + suffix) == base


def test_permalink_handles_nothing_at_all():
    assert x_sync._permalink_from(None) is None


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


def test_auth_watcher_ignores_403():
    """403s show up on secondary calls while the scrape succeeds, so treating
    them as a dead session would be the same false diagnosis in reverse."""
    class FakeResponse:
        def __init__(self, status, url):
            self.status, self.url = status, url

    class HookPage:
        def on(self, event, handler):
            self.handler = handler

    page = HookPage()
    unauthorized = x_sync._watch_for_auth_failure(page)

    page.handler(FakeResponse(403, "https://api.x.com/graphql/abc/Something"))

    assert unauthorized == []


def test_auth_watcher_still_logs_a_403_it_does_not_count(caplog):
    """Reporting an odd response and concluding the session is dead are two
    different things; the log should show it even when the diagnosis ignores it."""
    class FakeResponse:
        def __init__(self, status, url):
            self.status, self.url = status, url

    class HookPage:
        def on(self, event, handler):
            self.handler = handler

    page = HookPage()
    x_sync._watch_for_auth_failure(page)

    with caplog.at_level(logging.WARNING):
        page.handler(FakeResponse(403, "https://api.x.com/graphql/abc/Something"))

    assert "403" in caplog.text


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


# ---------------------------------------------------------------------------
# Collecting through a virtualised timeline
#
# X unmounts tweet cards that scroll far from the viewport, so the DOM never
# holds the whole list at once. A single extraction at the end therefore keeps
# only whatever survived the last scroll -- the OLDEST bookmarks. Against the
# real account that was 4 of 33.
# ---------------------------------------------------------------------------

class FakeTimeline:
    """A timeline that renders a different slice of tweets on every round.

    Unlike `FakePage`, `evaluate` here also answers the `_extract_tweets` shape,
    which is what makes the virtualisation observable: `rounds[i]` is all that
    exists in the DOM during round i, and earlier slices are gone for good.
    """

    def __init__(self, rounds, heights=None):
        self.rounds = [list(r) for r in rounds]
        self.index = 0
        # Enough equal heights to trip the stability counter and stop.
        self.heights = list(heights or [100] * (len(rounds) + 4))
        self.goto_calls = []

    @property
    def _current(self):
        return self.rounds[min(self.index, len(self.rounds) - 1)]

    def evaluate(self, script):
        if "scrollTo" in script:
            self.index += 1
            return None
        if "scrollHeight" in script:
            return self.heights.pop(0) if self.heights else 0
        if "tweet_url" in script:  # _extract_tweets
            return [dict(t) for t in self._current]
        if "querySelectorAll" in script:  # _count_tweets
            return len(self._current)
        raise AssertionError(f"unexpected script: {script}")


def _card(n):
    return {
        "tweet_url": f"https://x.com/someone/status/{n}",
        "author": f"user{n}",
        "text": f"texto {n}",
        "thumbnail_url": None,
        "has_media": 0,
        "article_content": None,
    }


def test_collect_keeps_tweets_that_only_existed_mid_scroll(instant_scroll):
    """The bug in one test: card 2 is unmounted before the scroll settles."""
    page = FakeTimeline(rounds=[[_card(1)], [_card(2)], [_card(3)], [_card(3)]])

    collected = x_sync._collect_bookmarks(page)

    urls = {t["tweet_url"] for t in collected}
    assert urls == {
        "https://x.com/someone/status/1",
        "https://x.com/someone/status/2",
        "https://x.com/someone/status/3",
    }


def test_collect_includes_the_top_of_the_list_before_any_scroll(instant_scroll):
    """Round 0 is where the newest bookmarks live -- exactly what went missing."""
    page = FakeTimeline(rounds=[[_card(99)], [_card(1)], [_card(1)]])

    collected = x_sync._collect_bookmarks(page)

    assert "https://x.com/someone/status/99" in {t["tweet_url"] for t in collected}


def test_collect_deduplicates_a_tweet_seen_in_several_rounds(instant_scroll):
    page = FakeTimeline(rounds=[[_card(1)], [_card(1), _card(2)], [_card(1)], [_card(1)]])

    collected = x_sync._collect_bookmarks(page)

    assert len(collected) == 2


def test_collect_reports_progress_while_it_scrolls(instant_scroll):
    page = FakeTimeline(rounds=[[_card(1)], [_card(2)], [_card(2)]])
    reported = []

    x_sync._collect_bookmarks(page, on_progress=lambda **event: reported.append(event))

    assert reported, "the scroll phase must keep reporting"
    assert all("message" in event for event in reported)


# ---------------------------------------------------------------------------
# Full text from the permalink
#
# The timeline truncates long tweets behind "Show more"; the stored text tops
# out around 280 characters. The permalink page carries the whole thing.
# ---------------------------------------------------------------------------

class FakePermalinkPage:
    """`goto` + `wait_for_selector` + `evaluate`, the three calls the fetch makes."""

    def __init__(self, text=None, fail_on=None):
        self.text = text
        self.fail_on = fail_on
        self.visited = []

    def goto(self, url, **kwargs):
        self.visited.append(url)
        if self.fail_on == "goto":
            raise RuntimeError("navigation failed")

    def wait_for_selector(self, selector, **kwargs):
        if self.fail_on == "wait":
            raise RuntimeError("tweet never rendered")

    def evaluate(self, script):
        if self.fail_on == "evaluate":
            raise RuntimeError("page died")
        return self.text


def test_fetch_full_text_returns_the_whole_tweet():
    long_text = "palabra " * 300
    page = FakePermalinkPage(text=long_text)

    assert x_sync._fetch_full_text(page, "https://x.com/u/status/1") == long_text


def test_fetch_full_text_visits_the_permalink():
    page = FakePermalinkPage(text="hola")

    x_sync._fetch_full_text(page, "https://x.com/u/status/1")

    assert page.visited == ["https://x.com/u/status/1"]


@pytest.mark.parametrize("fail_on", ["goto", "wait", "evaluate"])
def test_fetch_full_text_degrades_to_none_instead_of_raising(fail_on):
    """A deleted or protected tweet must not take the whole sync down with it."""
    page = FakePermalinkPage(fail_on=fail_on)

    assert x_sync._fetch_full_text(page, "https://x.com/u/status/1") is None
