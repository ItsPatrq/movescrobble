"""Link wanted tracks (playlist entries, Last.fm tracks) to files in the local library."""
from __future__ import annotations

from collections import defaultdict

from .library import Track
from .matcher import norm


def _artist_ok(a: str, b: str) -> bool:
    """Same artist, tolerating 'A, B' / 'A & B' credits and missing artist tags."""
    a, b = norm(a), norm(b)
    if not a or not b or a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    return ta <= tb or tb <= ta  # one credit contains the other's words


class LocalIndex:
    def __init__(self, library: list[Track]):
        self._by_title: dict[str, list[Track]] = defaultdict(list)
        for t in library:
            self._by_title[norm(t.title)].append(t)

    def find(self, artist: str, title: str, duration: float | None = None) -> Track | None:
        hits = [t for t in self._by_title.get(norm(title), []) if _artist_ok(artist, t.artist)]
        if not hits:
            return None
        if duration:  # several versions (live, remix): prefer the closest length
            hits.sort(key=lambda t: abs(t.duration - duration))
            if abs(hits[0].duration - duration) > 20 and len(hits) == 1:
                return None  # same title but a very different cut
        return hits[0]
