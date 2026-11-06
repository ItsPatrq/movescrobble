"""Artists and tracks you never want to hear: kept out of every playlist, the pool and the wishlist.

An artist block also catches collaborations ("Daft Punk, The Weeknd", "Song (feat. The Weeknd)").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .matcher import norm

_FEATURED = re.compile(r"\b(?:feat|ft|featuring|with)\b\.?(.*)", re.IGNORECASE)


def block(db, artist: str, title: str = "") -> None:
    """title "" blocks the whole artist."""
    db.execute("INSERT OR IGNORE INTO blocked(artist, title) VALUES(?,?)", (artist.strip(), title.strip()))
    db.commit()


def unblock(db, artist: str, title: str = "") -> bool:
    n = db.execute("DELETE FROM blocked WHERE lower(artist)=lower(?) AND lower(title)=lower(?)",
                   (artist.strip(), title.strip())).rowcount
    db.commit()
    return n > 0


def entries(db) -> list[tuple[str, str]]:
    return [(r["artist"], r["title"]) for r in db.execute("SELECT artist, title FROM blocked ORDER BY artist, title")]


def describe(artist: str, title: str = "") -> str:
    return f"{artist} - {title}" if title else f"{artist} (all tracks)"


def _has_words(haystack: str, needle: str) -> bool:
    return bool(needle) and f" {needle} " in f" {haystack} "


@dataclass(frozen=True)
class Blocklist:
    artists: frozenset[str] = frozenset()  # normalised
    tracks: frozenset[tuple[str, str]] = frozenset()  # normalised (artist, title)

    @classmethod
    def load(cls, db) -> Blocklist:
        rows = entries(db)
        return cls(frozenset(norm(a) for a, t in rows if not t), frozenset((norm(a), norm(t)) for a, t in rows if t))

    def blocks(self, artist: str, title: str) -> bool:
        a, t = norm(artist), norm(title)
        featured = norm(m.group(1)) if (m := _FEATURED.search(title)) else ""
        if any(_has_words(a, b) or _has_words(featured, b) for b in self.artists):
            return True
        return any(t == bt and _has_words(a, ba) for ba, bt in self.tracks)

    def keep(self, items: list) -> list:
        """Items (anything with .artist and .title) that are not blocked."""
        if not self.artists and not self.tracks:
            return items
        return [x for x in items if not self.blocks(x.artist, x.title)]
