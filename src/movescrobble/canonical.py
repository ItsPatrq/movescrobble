"""Which Last.fm page a track belongs on, so one song does not end up under several spellings.

File tags credit everyone ("Apparat, Philipp Johann Thimm"), and Last.fm files each spelling under its own artist
page. Its own autocorrect does not merge them (track.getCorrection returns such credits unchanged), so this module
decides: your history first, then Last.fm's listener counts. Only what is sent changes; local records keep the file's
tags, because the pool and the Garmin analysis look tracks up by them.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace

from .matcher import norm
from .scrobble import Scrobble
from .taste import Lastfm, Transport, http_get

Listeners = Callable[[str, str], int]  # (artist, title) -> Last.fm listeners, 0 when unknown

WIDE_COMMA = "，"  # yt-dlp's way of keeping a comma that belongs to a name ("Mandy， Indiana") apart from a separator
_SEPARATOR = re.compile(r"\s*[,;]\s*|\s+(?:[&/+]|(?-i:i|x)|and|with|vs\.?|feat\.?|ft\.?|featuring)\s+", re.I)  # "i": Polish "and"
_TITLE_CREDIT = re.compile(r"\s*[(\[](?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", re.I)


def _names(artist: str) -> list[str]:
    """Everyone credited, in order, without repeats; display spelling."""
    seen, out = set(), []
    for part in _SEPARATOR.split(artist):
        name = part.strip().replace(WIDE_COMMA, ",")
        if norm(name) and norm(name) not in seen:
            seen.add(norm(name))
            out.append(name)
    return out


def _fits(page: str, artist: str) -> bool:
    """`page` is the same credit as `artist`, or a narrower one: never widen a lone artist to a group credit."""
    if norm(page) and norm(page) == norm(artist):
        return True
    credited = {norm(n) for n in _names(page)}
    return bool(credited) and credited <= {norm(n) for n in _names(artist)}


def _bare_title(title: str) -> str:
    return _TITLE_CREDIT.sub("", title).strip() or title


def _title_key(title: str) -> str:
    """Spelling and credits fold away; a version does not: "(Remix)" or "[Live]" is another track."""
    return norm(re.sub(r"[()\[\]]", " ", _bare_title(title)))


@dataclass
class _Page:
    artist: str
    title: str
    plays: int


def _load_pages(db) -> dict[str, list[_Page]]:
    """Your history by title. Last.fm ignores case, so spellings differing only in case are one page."""
    pages: dict[tuple[str, str], _Page] = {}
    rows = db.execute("SELECT artist, title, COUNT(*) n FROM scrobbles GROUP BY artist, title ORDER BY n DESC")
    for r in rows:
        key = (r["artist"].casefold(), r["title"].casefold())
        if key in pages:
            pages[key].plays += r["n"]
        else:
            pages[key] = _Page(r["artist"], r["title"], r["n"])  # most played spelling comes first
    out = defaultdict(list)
    for page in pages.values():
        out[_title_key(page.title)].append(page)
    return out


def listeners_from_lastfm(db, transport: Transport = http_get) -> Listeners:
    client = Lastfm(db, transport)

    def listeners(artist: str, title: str) -> int:
        try:
            d = client.call("track.getInfo", artist=artist, track=title, autocorrect=0)
            return int(d.get("track", {}).get("listeners", 0))
        except (Exception, SystemExit):  # offline or no API key: a scrobble must not fail over this lookup
            return 0

    return listeners


class Resolver:
    """(artist, title) from the file's tags -> (artist, title) to send to Last.fm."""

    def __init__(self, db, listeners: Listeners | None = None):
        self._listeners = listeners or listeners_from_lastfm(db)
        self._pages = _load_pages(db)

    def __call__(self, artist: str, title: str) -> tuple[str, str]:
        mine = [p for p in self._pages.get(_title_key(title), ()) if _fits(p.artist, artist)]
        if mine:
            best = mine[0] if len(mine) == 1 else max(
                mine, key=lambda p: (self._listeners(p.artist, p.title), p.plays))
            return best.artist, best.title
        return self._never_played(artist, title)

    def _never_played(self, artist: str, title: str) -> tuple[str, str]:
        """Do not start a page of your own for a credit if one of its artists is where the track really lives."""
        whole = artist.replace(WIDE_COMMA, ",")
        names = _names(artist)
        if len(names) < 2:
            return whole, title
        bare = _bare_title(title)
        reach = {name: self._listeners(name, bare) for name in names}
        top = max(names, key=reach.__getitem__)
        return (top, bare) if reach[top] > self._listeners(whole, title) else (whole, title)


def canonicalize(scrobbles: list[Scrobble], db, listeners: Listeners | None = None) -> list[Scrobble]:
    resolve = Resolver(db, listeners)
    out = []
    for s in scrobbles:
        artist, title = resolve(s.artist, s.title)
        out.append(replace(s, artist=artist, title=title))
    return out
