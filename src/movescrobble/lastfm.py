"""Minimal Last.fm client: web auth flow + batch track.scrobble (stdlib only)."""
from __future__ import annotations

import hashlib
import time
import urllib.parse
from typing import Callable

from .config import read_json, secret, write_private_json
from .net import api_json
from .scrobble import Scrobble

API_URL = "https://ws.audioscrobbler.com/2.0/"
AUTH_URL = "https://www.last.fm/api/auth/"
MAX_AGE_SECONDS = 14 * 24 * 3600
BATCH = 50
SESSION_FILE = "lastfm.json"  # in config_dir()

Transport = Callable[[dict], dict]


def sign(params: dict, secret: str) -> str:
    """api_sig: md5 of sorted name+value pairs plus secret; `format` is not signed."""
    raw = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "format")
    return hashlib.md5((raw + secret).encode("utf-8")).hexdigest()


def http_post(params: dict) -> dict:
    return api_json(API_URL, urllib.parse.urlencode(params).encode())


def check(resp: dict) -> dict:
    if "error" in resp:
        raise RuntimeError(f"Last.fm error {resp['error']}: {resp.get('message')}")
    return resp


def _call(method: str, params: dict, key: str, secret: str, transport: Transport) -> dict:
    body = {**params, "method": method, "api_key": key}
    body["api_sig"] = sign(body, secret)
    body["format"] = "json"
    return check(transport(body))


def credentials() -> tuple[str, str]:
    key, shared = secret("LASTFM_API_KEY"), secret("LASTFM_API_SECRET")
    if not key or not shared:
        raise SystemExit("set LASTFM_API_KEY and LASTFM_API_SECRET (https://www.last.fm/api/account/create)")
    return key, shared


def authenticate(transport: Transport = http_post, wait: Callable[[str], None] = input) -> str:
    """Web auth: get token, user approves in browser, exchange for a session key."""
    key, secret = credentials()
    token = _call("auth.getToken", {}, key, secret, transport)["token"]
    wait(f"Open {AUTH_URL}?api_key={key}&token={token} , approve, then press Enter: ")
    sk = _call("auth.getSession", {"token": token}, key, secret, transport)["session"]["key"]
    write_private_json(SESSION_FILE, {"session_key": sk})
    return sk


def load_session_key() -> str:
    saved = read_json(SESSION_FILE)
    if not saved:
        raise SystemExit("not authenticated; run: movescrobble lastfm-auth")
    return saved["session_key"]


def check_window(scrobbles: list[Scrobble], now: float | None = None) -> list[Scrobble]:
    """Return scrobbles Last.fm would reject (older than 14 days or in the future)."""
    now = now if now is not None else time.time()
    return [s for s in scrobbles if not (now - MAX_AGE_SECONDS <= s.timestamp.timestamp() <= now)]


def love(artist: str, title: str, session_key: str, transport: Transport = http_post) -> None:
    key, secret = credentials()
    _call("track.love", {"artist": artist, "track": title, "sk": session_key}, key, secret, transport)


def submit(
    scrobbles: list[Scrobble],
    session_key: str,
    transport: Transport = http_post,
) -> int:
    """Send in batches of 50. Returns number accepted."""
    key, secret = credentials()
    accepted = 0
    for i in range(0, len(scrobbles), BATCH):
        params: dict = {"sk": session_key}
        for j, s in enumerate(scrobbles[i : i + BATCH]):
            params[f"artist[{j}]"] = s.artist
            params[f"track[{j}]"] = s.title
            params[f"timestamp[{j}]"] = int(s.timestamp.timestamp())
            if s.album:
                params[f"album[{j}]"] = s.album
            params[f"duration[{j}]"] = s.duration
        resp = _call("track.scrobble", params, key, secret, transport)
        accepted += int(resp["scrobbles"]["@attr"]["accepted"])
    return accepted
