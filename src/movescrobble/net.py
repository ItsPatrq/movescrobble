"""The one place HTTP requests are built: shared User-Agent and timeout (some hosts 403 Python-urllib)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) movescrobble"


def open_url(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def request_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    with open_url(url, data, headers) as r:
        return json.load(r)


def api_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    """Like request_json, for APIs that answer an error with an HTTP status and a JSON body saying what went wrong
    (Last.fm: {"error": 9, "message": "Invalid session key"}): that body comes back for the caller to read."""
    try:
        return request_json(url, data, headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            raise e from None
        if isinstance(body, dict) and "error" in body:
            return body
        raise
