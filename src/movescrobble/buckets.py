"""Judge every pool track against your listening history and sort it into a bucket.

obsession  by an artist you are into right now (decayed artist plays), any of their tracks
mid        played within the last 90 days, artist not hot
lost       played before (2+ times or loved), silent for 90+ days
fresh      never played, but by an artist you played in the past year
rec        never played, artist unknown to you: a discovery, scored by the taste graph (taste.py); one the graph
           does not connect to your listening is left out instead of filling the quota

Obsession is defined by *artist*, not by track: in your history only ~9% of a month's plays are tracks
from the month before, but 32% of the new-to-you tracks are by an artist you played that month.
"""
from __future__ import annotations

import re

from .affinity import Aff, ArtistAff, squash
from .generator import Candidate
from .library import Track
from .link import LocalIndex
from .matcher import norm
from .mix import DEFAULT_QUOTAS
from .taste import Taste

BUCKETS = ("obsession", "mid", "fresh", "lost", "rec")
DAY = 86400
HOT_ARTIST = 0.5  # squash(artist decayed plays, 30) at/above this = "obsession artist" (about 5 recent plays)
WISH_MIN_PLAYS = 3
WISH_MAX_AGE = 365 * DAY  # only ask for tracks you still listen to, not what you played in 2014
MAX_WISHES = 300  # split between buckets in proportion to their mix share
NO_GRAPH_FIT = 0.35  # without a taste graph (no Last.fm user) every track counts as a middling fit
REC_MIN = 0.2  # a discovery the taste graph scores below this is not worth a place in the playlist
# podcast / video / long-form titles that end up in Last.fm history but are not songs
NOT_A_SONG = re.compile(r"#\d+|\b(episode|odcinek|ep\.)\s*\d+|\||\bpodcast\b", re.I)


def looks_like_song(title: str) -> bool:
    return len(title) <= 70 and not NOT_A_SONG.search(title)


def _merge(a: Aff | None, b: Aff) -> Aff:
    if a is None:
        return b
    return Aff(a.now + b.now, a.long + b.long, a.total + b.total, max(a.last, b.last), a.weeks + b.weeks)


def relevance(a: Aff | None, art: ArtistAff | None) -> float:
    """0..1: this month's plays of the track, longer-term plays, and what the artist is doing now."""
    tn = squash(a.now, 6) if a else 0.0
    tl = squash(a.long, 25) if a else 0.0
    an = squash(art.now, 30) if art else 0.0
    return 0.5 * tn + 0.25 * tl + 0.25 * an


def classify(a: Aff | None, art: ArtistAff | None, loved: bool, now: float) -> str:
    hot = art is not None and squash(art.now, 30) >= HOT_ARTIST
    played = a is not None and a.total > 0
    if hot:
        return "obsession"
    if played and a.last >= now - 90 * DAY:
        return "mid"
    if (played and a.total >= 2) or loved:
        return "lost"
    if not played and art is not None and art.total >= 3 and art.last >= now - 365 * DAY:
        return "fresh"
    return "rec"


def score(bucket: str, a: Aff | None, art: ArtistAff | None, loved: bool, fit: float) -> float:
    """`fit`: the taste graph's 0..1 opinion of the track (or its artist)."""
    r = relevance(a, art)
    if bucket in ("obsession", "mid"):
        return max(0.15, r) + 0.01 * squash(a.total if a else 0, 1000)  # tiebreak among saturated scores
    if bucket == "lost":
        loyal = squash(a.weeks, 20) if a else 0.0
        return 0.2 + 0.5 * squash(a.long if a else 0.0, 3) + 0.2 * loyal + (0.1 if loved else 0.0)
    if bucket == "fresh":
        return 0.2 + 0.4 * squash(art.now if art else 0.0, 30) + 0.4 * fit
    return fit  # rec: only as good as its place in your taste graph


def assign(
    taste: Taste | None,
    tracks: dict[tuple[str, str], Aff],
    artists: dict[str, ArtistAff],
    library: list[Track],
    pool: set[tuple[str, str]] | None = None,
    idx: LocalIndex | None = None,
    now: float = 0.0,
) -> tuple[list[Candidate], list[Candidate]]:
    """Return (candidates for owned pool tracks, wanted-but-not-owned candidates).

    `pool` holds local keys (casefolded artist, title) eligible for the session; None = whole library.
    """
    idx = idx or LocalIndex(library)
    fit = taste.fit if taste and taste.artist_score else (lambda a, t: NO_GRAPH_FIT)
    taste = taste or Taste()
    lkey = lambda t: (t.artist.casefold(), t.title.casefold())  # noqa: E731

    # Last.fm spells artists/titles its own way: fold every history row onto the local file it matches
    owned_aff: dict[tuple[str, str], Aff] = {}
    foreign: dict[tuple[str, str], Aff] = {}  # played on Last.fm but no local file
    for (a, t), aff in tracks.items():
        tr = idx.find(a, t)
        if tr:
            owned_aff[lkey(tr)] = _merge(owned_aff.get(lkey(tr)), aff)
        else:
            foreign[(a, t)] = aff
    artist_aff: dict[str, ArtistAff] = {}
    for a, aff in artists.items():
        old = artist_aff.get(norm(a))
        artist_aff[norm(a)] = aff if old is None else ArtistAff(
            old.now + aff.now, max(old.last, aff.last), old.total + aff.total)

    loved = {lkey(tr) for a, t in taste.loved if (tr := idx.find(a, t))}

    owned: list[Candidate] = []
    for t in library:
        k = lkey(t)
        if pool is not None and k not in pool:
            continue
        a, art = owned_aff.get(k), artist_aff.get(norm(t.artist))
        is_loved = k in loved
        b = classify(a, art, is_loved, now)
        s = score(b, a, art, is_loved, fit(t.artist, t.title))
        if b != "rec" or s >= REC_MIN:
            owned.append(Candidate(t.artist, t.title, s, {b}))

    # wishes: what you clearly like (or Last.fm suggests) but have no file for
    wanted: dict[tuple[str, str], Candidate] = {}

    def want(a: str, t: str, bucket: str, s: float) -> None:
        wanted.setdefault((norm(a), norm(t)), Candidate(a, t, s, {bucket}))

    for (a, t), aff in foreign.items():
        recent_enough = not now or aff.last >= now - WISH_MAX_AGE
        if aff.total >= WISH_MIN_PLAYS and recent_enough and looks_like_song(t):  # played repeatedly, nothing local
            art = artist_aff.get(norm(a))
            b = classify(aff, art, False, now)
            want(a, t, b, score(b, aff, art, False, fit(a, t)))
    for a, t, s in taste.recs:  # the taste graph's picks you have no file for (never played: recs exclude those)
        if not idx.find(a, t):
            art = artist_aff.get(norm(a))
            b = classify(None, art, False, now)
            s = score(b, None, art, False, s)
            if b != "rec" or s >= REC_MIN:
                want(a, t, b, s)

    # cap per bucket (not overall), otherwise heavily played tracks crowd out discoveries
    per_bucket: dict[str, list[Candidate]] = {}
    for c in sorted(wanted.values(), key=lambda c: -c.score):
        per_bucket.setdefault(next(iter(c.sources)), []).append(c)
    wishes = sorted(
        (c for b, cs in per_bucket.items() for c in cs[: max(10, round(MAX_WISHES * DEFAULT_QUOTAS.get(b, 0.1)))]),
        key=lambda c: -c.score,
    )
    return sorted(owned, key=lambda c: -c.score), wishes
