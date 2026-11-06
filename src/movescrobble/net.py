"""The one place HTTP requests are built: shared User-Agent and timeout (some hosts 403 Python-urllib)."""
from __future__ import annotations

import json
import urllib.request

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) movescrobble"


def open_url(url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def request_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    with open_url(url, data, headers) as r:
        return json.load(r)
