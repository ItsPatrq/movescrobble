"""Read a YouTube Music playlist via yt-dlp (flat, no downloads)."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Entry:
    video_id: str
    artist: str
    title: str
    duration: float | None
    index: int = 0  # 1-based position in the playlist (for --playlist-items)
    playlist: str = ""  # URL of the playlist this entry came from


def parse(data: dict) -> list[Entry]:
    out = []
    for pos, e in enumerate(data.get("entries") or [], 1):
        if not e or not e.get("id"):
            continue
        title = (e.get("title") or "").strip()
        artist = (e.get("channel") or e.get("uploader") or "").strip()
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")]
        elif " - " in title:  # plain uploads are usually "Artist - Title"
            artist, title = (x.strip() for x in title.split(" - ", 1))
        out.append(Entry(e["id"], artist, title, e.get("duration"), e.get("playlist_index") or pos))
    return out


def playlist_id(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return (parse_qs(urlparse(url).query).get("list") or [url])[0]


def add_to_playlist(auth_file, playlist_url: str, video_ids: list[str]) -> None:
    """Needs browser auth (`movescrobble ytm-auth`); reading playlists does not."""
    from ytmusicapi import YTMusic

    YTMusic(str(auth_file)).add_playlist_items(playlist_id(playlist_url), video_ids, duplicates=False)


def fetch(url: str, run: Callable = subprocess.run, cookies: str | None = None) -> list[Entry]:
    cmd = ["yt-dlp", *(["--cookies", cookies] if cookies else []), "--flat-playlist", "-J", url]
    proc = run(cmd, capture_output=True, text=True, check=True)
    return [replace(e, playlist=url) for e in parse(json.loads(proc.stdout))]
