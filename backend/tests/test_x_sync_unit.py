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
