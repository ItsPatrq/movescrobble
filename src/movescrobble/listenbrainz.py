"""ListenBrainz import (listen_type=import) with a user token."""
from __future__ import annotations

import json
from collections.abc import Callable

from .config import secret
from .net import request_json
from .scrobble import Scrobble

URL = "https://api.listenbrainz.org/1/submit-listens"
BATCH = 100

Transport = Callable[[dict, str], dict]


def http_post(payload: dict, token: str) -> dict:
    return request_json(URL, json.dumps(payload).encode(),
                        {"Authorization": f"Token {token}", "Content-Type": "application/json"})


def build_payload(scrobbles: list[Scrobble]) -> dict:
    return {
        "listen_type": "import",
        "payload": [
            {
                "listened_at": int(s.timestamp.timestamp()),
                "track_metadata": {
                    "artist_name": s.artist,
                    "track_name": s.title,
                    **({"release_name": s.album} if s.album else {}),
                    "additional_info": {"duration": s.duration, "submission_client": "movescrobble"},
                },
            }
            for s in scrobbles
        ],
    }


def submit(scrobbles: list[Scrobble], token: str | None = None, transport: Transport = http_post) -> int:
    token = token or secret("LISTENBRAINZ_TOKEN")
    if not token:
        raise SystemExit("set LISTENBRAINZ_TOKEN (https://listenbrainz.org/settings/)")
    sent = 0
    for i in range(0, len(scrobbles), BATCH):
        chunk = scrobbles[i : i + BATCH]
        resp = transport(build_payload(chunk), token)
        if resp.get("status") != "ok":
            raise RuntimeError(f"ListenBrainz error: {resp}")
        sent += len(chunk)
    return sent
