"""YouTube Music login, taken from a browser you are already signed in to.

YouTube Music has no simple login for a tool like this: it authorises requests with your browser session's cookies. So
those are what gets stored, and they can act as your Google account on YouTube: only the youtube.com ones are kept, in a
file only you can read, and nothing is saved until a read-only call shows they work.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
from collections.abc import Iterable
from pathlib import Path

ORIGIN = "https://music.youtube.com"
LOGIN_COOKIE = "__Secure-3PAPISID"  # only present when signed in; the request signature is computed from it
BROWSERS = {  # yt-dlp's name -> where macOS keeps the app
    "firefox": "/Applications/Firefox.app",
    "chrome": "/Applications/Google Chrome.app",
    "brave": "/Applications/Brave Browser.app",
    "edge": "/Applications/Microsoft Edge.app",
    "vivaldi": "/Applications/Vivaldi.app",
    "opera": "/Applications/Opera.app",
    "safari": "/Applications/Safari.app",
}
WHAT_TO_EXPECT = {
    "chrome": "macOS will ask to let this read \"Chrome Safe Storage\": that key decrypts Chrome's cookies.",
    "brave": "macOS will ask to let this read \"Brave Safe Storage\": that key decrypts Brave's cookies.",
    "edge": "macOS will ask to let this read \"Microsoft Edge Safe Storage\": that key decrypts Edge's cookies.",
    "vivaldi": "macOS will ask to let this read \"Vivaldi Safe Storage\": that key decrypts Vivaldi's cookies.",
    "opera": "macOS will ask to let this read \"Opera Safe Storage\": that key decrypts Opera's cookies.",
    "safari": "Safari's cookies need Full Disk Access for your terminal (System Settings > Privacy & Security).",
}


class LoginError(Exception):
    """Something you can act on: the message says what."""


def installed() -> list[str]:
    return [name for name, app in BROWSERS.items() if Path(app).exists()]


def read_browser(browser: str, profile: str | None = None) -> Iterable:
    from yt_dlp.cookies import extract_cookies_from_browser  # heavy; only when logging in

    try:
        return extract_cookies_from_browser(browser, profile)
    except Exception as e:
        raise LoginError(f"could not read {browser}'s cookies: {e}") from e


def read_file(path: Path) -> Iterable:
    """A cookies.txt exported from a signed-in browser."""
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as e:
        raise LoginError(f"could not read {path}: {e}") from e
    return jar


def headers_from(cookies: Iterable, account: int = 0) -> dict:
    """Request headers from a browser's cookies. `account`: which Google account, for several signed in (0 = first)."""
    from ytmusicapi import setup

    youtube = {c.name: c.value for c in cookies if c.domain.lstrip(".").endswith("youtube.com")}
    if LOGIN_COOKIE not in youtube:
        raise LoginError(f"no YouTube login there: sign in at {ORIGIN} in that browser, then run this again")
    cookie = "; ".join(f"{name}={value}" for name, value in youtube.items())
    # ytmusicapi replaces the authorization value on every request; it only needs to be present
    raw = f"cookie: {cookie}\nauthorization: SAPISIDHASH 0\nx-goog-authuser: {account}\nx-origin: {ORIGIN}\n"
    return json.loads(setup(headers_raw=raw))


def account_name(headers: dict, client=None) -> str:
    """A read-only call: who these cookies sign in as."""
    if client is None:
        from ytmusicapi import YTMusic as client
    try:
        return client(headers).get_account_info()["accountName"]
    except Exception as e:
        detail = str(e)[:80]  # ytmusicapi's parser errors run long and mean little on their own
        raise LoginError(f"YouTube Music did not accept that login: sign in again at {ORIGIN} in that browser, "
                         f"then run this again ({detail})") from e


def save(path: Path, headers: dict) -> None:
    """Readable by you only, from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(headers, f)
    path.chmod(0o600)  # an older file keeps its mode through O_CREAT
