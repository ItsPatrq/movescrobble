"""What you will probably enjoy next: a taste graph built from Last.fm data, walked from what you play now.

Nodes are artists, tags (genres) and tracks. Edges:
- artist - artist: artist.getSimilar's `match` (how much their listeners overlap);
- artist - tag: the artist's top tags, IDF-weighted so "darkwave" links more than "electronic" or "rock";
- track - track: track.getSimilar for the tracks you play most right now, each track hanging off its artist.
A random walk with restart (personalised PageRank) starts from your artists, weighted by how much you play them
*now*. The share of time it spends on a node is how close that node is to your taste through every path at once:
a band that three of your favourites point to ranks above one that only one of them mentions. The same walk from
what you blocked or keep skipping is subtracted, and every artist is then weighted by how well its genres match
your genre profile. Tracks to try are the top tracks of the best artists you barely know (and of the ones you do:
those become "fresh"), plus what Last.fm calls similar to your current favourites. Popular-with-everyone hits
are never pulled in from a genre's global chart: everything has to be reached from your own listening.
Responses are cached in the database for CACHE_DAYS, so only the first sync makes many requests.
"""
from __future__ import annotations

import functools
import json
import math
import time
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .config import secret
from .lastfm import API_URL, check
from .matcher import norm
from .net import request_json

Transport = Callable[[dict], dict]
Progress = Callable[[str], None]

DAY = 86400
CACHE_DAYS = 30
SEED_ARTISTS = 40  # your most-played artists right now start the walk
SEED_TRACKS = 15  # ...and your most-played tracks right now
TAG_ARTISTS = 200  # besides seeds and library artists, the best candidates after the first walk get their genres
TRY_ARTISTS = 40  # discoveries whose top tracks become recommendations
TRY_KNOWN = 20  # known artists whose top tracks you have not played yet become "fresh" candidates
RESTART = 0.3  # high restart = short walks = close relatives count most
STEPS = 40
W_SIMILAR, W_TAG, W_TRACK, W_MEMBER = 1.0, 0.6, 0.8, 0.5
NEG_WEIGHT = 0.8  # how strongly closeness to blocked / skipped music pulls a score down
DEGREE_DAMP = 0.3  # divides by degree^this: hubs everyone links to are not automatically "your taste"
GENRE_FLOOR = 0.35  # an artist with no genre overlap keeps this share of its graph score
SPREAD = 0.7  # each further discovery reached mainly through the same artist of yours counts this much less
KNOWN_PLAYS = 3  # below this many plays an artist is still a discovery
NO_GENRE_FIT = 0.25  # an artist Last.fm has no genres for is usually obscure or not music: a weak fit
TALK_TITLES = 32  # a title longer than this (or that looks like an episode) sounds like talk, not a song
NOT_MUSIC_TAGS = {"podcast", "podcasts", "audiobook", "audiobooks", "talk", "news", "comedy", "radio show"}
GENERIC_TAGS = {
    "seen live", "favorites", "favourites", "favourite", "albums i own", "male vocalists",
    "female vocalists", "under 2000 listeners", "all", "good", "awesome", "love", "my music", "beautiful",
    "cool", "best", "amazing", "spotify", "soundtrack", "british", "american", "uk", "usa", "german",
}


def http_get(params: dict) -> dict:
    return request_json(f"{API_URL}?{urllib.parse.urlencode(params)}")


def _get(method: str, transport: Transport, **params) -> dict:
    key = secret("LASTFM_API_KEY")
    if not key:
        raise SystemExit("set LASTFM_API_KEY")
    return check(transport({"method": method, "api_key": key, "format": "json", **params}))


def _items(x) -> list:
    """Last.fm returns a lone result as a dict instead of a one-element list."""
    return [x] if isinstance(x, dict) else list(x or [])


class Lastfm:
    """Read-only Last.fm calls, cached in the database (a miss, like an unknown artist, is cached too)."""

    def __init__(self, db, transport: Transport = http_get, now: float | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.db, self.transport, self.sleep = db, transport, sleep
        self.now = now if now is not None else time.time()

    def call(self, method: str, ttl_days: float = CACHE_DAYS, **params) -> dict:
        k = method + "?" + urllib.parse.urlencode(sorted((p, str(v).casefold()) for p, v in params.items()))
        row = self.db.execute("SELECT at, body FROM lastfm_cache WHERE key=?", (k,)).fetchone()
        if row and self.now - row["at"] < ttl_days * DAY:
            return json.loads(row["body"])
        try:
            body = _get(method, self.transport, **params)
        except RuntimeError:  # Last.fm said no (unknown artist / track): remember, do not ask again
            body = {}
        self.sleep(0.2)  # stay under Last.fm's 5 requests a second
        self.db.execute("INSERT OR REPLACE INTO lastfm_cache(key, at, body) VALUES(?,?,?)", (k, self.now, json.dumps(body)))
        self.db.commit()
        return body

    def similar_artists(self, artist: str, limit: int = 50) -> list[tuple[str, float]]:
        d = self.call("artist.getSimilar", artist=artist, limit=limit, autocorrect=1)
        return [(a["name"], float(a.get("match", 0))) for a in _items(d.get("similarartists", {}).get("artist"))]

    def similar_tracks(self, artist: str, title: str, limit: int = 30) -> list[tuple[str, str, float]]:
        d = self.call("track.getSimilar", artist=artist, track=title, limit=limit, autocorrect=1)
        return [(t["artist"]["name"], t["name"], float(t.get("match", 0)))
                for t in _items(d.get("similartracks", {}).get("track"))]

    def tags(self, artist: str, limit: int = 10) -> list[tuple[str, float]]:
        """(tag, weight 0..1) for the artist's genres; the generic ones ("seen live") are left out."""
        d = self.call("artist.getTopTags", artist=artist, autocorrect=1)
        out = []
        for t in _items(d.get("toptags", {}).get("tag")):
            name, count = t["name"].casefold().strip(), int(t.get("count", 0))
            if count >= 5 and name not in GENERIC_TAGS and norm(name) != norm(artist):
                out.append((name, count / 100))
        return out[:limit]

    def top_tracks(self, artist: str, limit: int = 10) -> list[tuple[str, str]]:
        d = self.call("artist.getTopTracks", artist=artist, limit=limit, autocorrect=1)
        return [(t["artist"]["name"], t["name"]) for t in _items(d.get("toptracks", {}).get("track"))]

    def loved(self, user: str, limit: int = 200) -> list[tuple[str, str]]:
        d = self.call("user.getLovedTracks", ttl_days=1, user=user, limit=limit)
        return [(t["artist"]["name"], t["name"]) for t in _items(d.get("lovedtracks", {}).get("track"))]


class Graph:
    """Weighted undirected graph over named nodes, walked with restart."""

    def __init__(self):
        self.ids: dict[str, int] = {}
        self.src: list[int] = []
        self.dst: list[int] = []
        self.w: list[float] = []

    def node(self, name: str) -> int:
        return self.ids.setdefault(name, len(self.ids))

    def link(self, a: str, b: str, w: float) -> None:
        if w <= 0 or a == b:
            return
        i, j = self.node(a), self.node(b)
        self.src += [i, j]
        self.dst += [j, i]
        self.w += [w, w]

    def degree(self) -> np.ndarray:
        return np.bincount(np.array(self.src, dtype=int), minlength=len(self.ids)).astype(float)

    def walk(self, restart: dict[str, float]) -> np.ndarray:
        """Personalised PageRank: where a walker that keeps jumping back to `restart` spends its time."""
        n = len(self.ids)
        s = np.zeros(n)
        for name, v in restart.items():
            if name in self.ids:
                s[self.ids[name]] += v
        if not n or s.sum() <= 0:
            return np.zeros(n)
        s /= s.sum()
        src, dst, w = np.array(self.src, dtype=int), np.array(self.dst, dtype=int), np.array(self.w)
        out = np.bincount(src, w, minlength=n)
        share = w / out[src] if len(w) else w
        r = s.copy()
        for _ in range(STEPS):
            spread = np.bincount(dst, r[src] * share, minlength=n)
            r = RESTART * s + (1 - RESTART) * (spread + r[out == 0].sum() * s)  # dead ends jump back too
        return r


@dataclass
class Taste:
    """What the taste graph thinks of artists and tracks; keys are norm()-alised."""

    loved: list[tuple[str, str]] = field(default_factory=list)
    artist_score: dict[str, float] = field(default_factory=dict)  # norm(artist) -> 0..1 how likely you enjoy them
    recs: list[tuple[str, str, float]] = field(default_factory=list)  # tracks to try (owned or not), best first
    why: dict[str, list[str]] = field(default_factory=dict)  # norm(artist) -> your artists that lead to it

    @functools.cached_property
    def _rec_score(self) -> dict[tuple[str, str], float]:
        return {(norm(a), norm(t)): s for a, t, s in self.recs}

    def fit(self, artist: str, title: str) -> float:
        """0..1: the track's own recommendation score, or its artist's if that is higher."""
        return max(self._rec_score.get((norm(artist), norm(title)), 0.0), self.artist_score.get(norm(artist), 0.0))


def seed_weights(tracks: dict, artists: dict, n: int = SEED_ARTISTS) -> dict[str, float]:
    """Artist -> restart weight: this month's plays (30-day half-life) count most, the last few months less."""
    w: dict[str, float] = defaultdict(float)
    for (a, _), aff in tracks.items():
        w[a] += aff.now
    for a, aff in artists.items():
        w[a] += 0.3 * aff.now
    best = sorted(((a, v) for a, v in w.items() if v > 0.5), key=lambda kv: -kv[1])[:n]
    return {a: math.log1p(v) for a, v in best}


def talk_share(titles: list[str]) -> float:
    """How many of an artist's titles read like podcast episodes ("Why Are Games So Much Like Jobs?")."""
    from .buckets import looks_like_song

    return sum(not looks_like_song(t) or len(t) > TALK_TITLES for t in titles) / max(len(titles), 1)


def spoken(titles: list[str], tags: list[tuple[str, float]]) -> bool:
    """A podcast or talk channel in your history: tagged so, or mostly talk-like titles (and no genres to say
    otherwise). Those must not seed the taste graph, or it recommends more talk."""
    if any(t in NOT_MUSIC_TAGS for t, _ in tags):
        return True
    share = talk_share(titles)
    return len(titles) >= 5 and (share >= 0.5 or (share >= 0.3 and not tags))


def dislikes(db) -> dict[str, float]:
    """Artist -> how strongly you rejected them: the whole artist blocked, or skipped on swims again and again.
    One blocked track says nothing about the artist (it is left out on its own), so it does not count here."""
    from .history import skip_counts

    out: dict[str, float] = defaultdict(float)
    for r in db.execute("SELECT artist FROM blocked WHERE title=''"):
        out[r["artist"]] += 1.0
    for a, n in skip_counts(db)[1].items():
        out[a] += min(1.0, 0.25 * n)
    return dict(out)


def _scaled(x: np.ndarray, mask: np.ndarray) -> np.ndarray:
    top = np.quantile(x[mask], 0.99) if mask.any() else 0.0
    return x / top if top > 0 else np.zeros_like(x)


def diversified(ranked: list[str], scores: dict[str, float], owner: dict[str, str], n: int) -> dict[str, float]:
    """Pick `n` artists, best first, but each further one led to by the same artist of yours is worth SPREAD times
    less: one tight cluster (artists who all list each other) must not take every slot. Returns pick -> the
    discounted score, used for its tracks."""
    pool, used, out = list(ranked[: n * 5]), Counter(), {}
    while pool and len(out) < n:
        best = max(pool, key=lambda a: scores[a] * SPREAD ** used[owner[a]])
        out[best] = scores[best] * SPREAD ** used[owner[best]]
        used[owner[best]] += 1
        pool.remove(best)
    return out


def recommend(
    db,
    user: str,
    tracks: dict,
    artists: dict,
    library_artists: list[str] = (),
    lf: Lastfm | None = None,
    progress: Progress = lambda _: None,
) -> Taste:
    """Build and walk the taste graph. `tracks` / `artists` are affinity.compute's output (your history)."""
    from .buckets import looks_like_song

    lf = lf or Lastfm(db)
    g = Graph()
    names: dict[str, str] = {}  # norm(artist) -> a display spelling

    def art(a: str) -> str:
        names.setdefault(norm(a), a)
        return "a:" + norm(a)

    def trk(a: str, t: str) -> str:
        return f"t:{norm(a)}|{norm(t)}"

    tags_of: dict[str, list[tuple[str, float]]] = {}

    def tags(n: str) -> list[tuple[str, float]]:
        if n not in tags_of:
            tags_of[n] = lf.tags(names[n[2:]])
        return tags_of[n]

    titles: dict[str, list[str]] = defaultdict(list)
    for a, t in tracks:
        titles[a].append(t)
    talk: set[str] = set()
    pre = seed_weights(tracks, artists, 2 * SEED_ARTISTS)
    for i, a in enumerate(pre, 1):
        progress(f"taste graph: your artists' genres {i}/{len(pre)}")
        if spoken(titles[a], tags(art(a))):
            talk.add(a)
    seed = {a: w for a, w in pre.items() if a not in talk}
    seed = dict(list(seed.items())[:SEED_ARTISTS])
    restart = {art(a): w for a, w in seed.items()}
    led_by: dict[str, Counter] = defaultdict(Counter)  # who brought each artist in, for `why`
    for i, (a, w) in enumerate(seed.items(), 1):
        progress(f"taste graph: artists similar to yours {i}/{len(seed)}")
        for b, m in lf.similar_artists(a):
            g.link(art(a), art(b), W_SIMILAR * m)
            led_by[norm(b)][a] += w * m

    hot = sorted(((k, aff) for k, aff in tracks.items()
                  if aff.now > 0.5 and looks_like_song(k[1]) and k[0] not in talk and k[0] in pre),
                 key=lambda kv: -kv[1].now)[:SEED_TRACKS]
    for i, ((a, t), aff) in enumerate(hot, 1):
        progress(f"taste graph: tracks similar to your current favourites {i}/{len(hot)}")
        g.link(trk(a, t), art(a), W_MEMBER)
        restart[trk(a, t)] = 0.5 * math.log1p(aff.now)
        for b, u, m in lf.similar_tracks(a, t):
            g.link(trk(a, t), trk(b, u), W_TRACK * m)
            g.link(trk(b, u), art(b), W_MEMBER)
            led_by[norm(b)][a] += 0.5 * m

    # genres: for your artists, your library's, and the best candidates of a first walk without them
    first = g.walk(restart)
    seeds = [n for n in restart if n.startswith("a:")]
    cands = sorted((n for n in g.ids if n.startswith("a:") and n not in restart), key=lambda n: -first[g.ids[n]])
    tagged = list(dict.fromkeys([*seeds, *(art(a) for a in library_artists), *cands[:TAG_ARTISTS]]))
    for i, n in enumerate(tagged, 1):
        if i % 10 == 0 or i == len(tagged):
            progress(f"taste graph: genres {i}/{len(tagged)}")
        tags(n)
    talk |= {names[n[2:]] for n, ts in tags_of.items() if any(t in NOT_MUSIC_TAGS for t, _ in ts)}
    df = Counter(t for ts in tags_of.values() for t, _ in ts)
    idf = {t: math.log(1 + len(tags_of) / c) for t, c in df.items()}
    top_idf = max(idf.values(), default=1.0)
    for n, ts in tags_of.items():
        for t, c in ts:
            g.link(n, "g:" + t, W_TAG * c * idf[t] / top_idf)

    neg = {art(a): v for a, v in dislikes(db).items()}
    pos_r, neg_r = g.walk(restart), g.walk(neg)
    # scaled against the candidates, not your own artists (who get the restart mass): a top discovery reaches 1
    cand = np.array([n.startswith("a:") and n not in restart for n in g.ids])
    damp = np.maximum(g.degree(), 1.0) ** DEGREE_DAMP
    graph = np.clip(_scaled(pos_r / damp, cand) - NEG_WEIGHT * _scaled(neg_r / damp, cand), 0, 1)

    # genre fit: cosine between the artist's tags and your profile (your artists' tags, weighted like the walk)
    profile: dict[str, float] = defaultdict(float)
    for n in seeds:
        for t, c in tags_of.get(n, []):
            profile[t] += restart[n] * c * idf[t]
    p_norm = math.sqrt(sum(v * v for v in profile.values())) or 1.0

    def genre_fit(n: str) -> float | None:
        ts = tags_of.get(n)
        if not ts:
            return None
        v = {t: c * idf[t] for t, c in ts}
        return sum(profile.get(t, 0.0) * x for t, x in v.items()) / (p_norm * (math.sqrt(sum(x * x for x in v.values())) or 1.0))

    scores: dict[str, float] = {}
    for n, i in g.ids.items():
        if n.startswith("a:"):
            fit = genre_fit(n)
            fit = NO_GENRE_FIT if fit is None else min(1.0, fit / 0.6)  # 0.6 cosine is already a close genre match
            scores[n[2:]] = float(graph[i]) ** 0.5 * (GENRE_FLOOR + (1 - GENRE_FLOOR) * fit)
    banned = [r["artist"] for r in db.execute("SELECT artist FROM blocked WHERE title=''")]
    for a in [*banned, *talk]:  # never recommend an artist you blocked (or talk), however close it sits
        scores.pop(norm(a), None)

    # tracks to try
    played = {(norm(a), norm(t)) for a, t in tracks}
    plays_of: dict[str, int] = defaultdict(int)
    for a, aff in artists.items():
        plays_of[norm(a)] += aff.total
    ranked = sorted(scores, key=lambda a: -scores[a])
    owner = {a: led_by[a].most_common(1)[0][0] if led_by.get(a) else a for a in ranked}
    new_artists = diversified([a for a in ranked if plays_of[a] < KNOWN_PLAYS], scores, owner, TRY_ARTISTS)
    known = diversified([a for a in ranked if plays_of[a] >= KNOWN_PLAYS], scores, owner, TRY_KNOWN)
    picks = {**new_artists, **known}
    recs: dict[tuple[str, str], tuple[str, str, float]] = {}

    def offer(a: str, t: str, s: float) -> None:
        k = (norm(a), norm(t))
        if k not in played and looks_like_song(t) and s > recs.get(k, ("", "", 0.0))[2]:
            recs[k] = (a, t, s)

    for i, (a, s) in enumerate(picks.items(), 1):
        progress(f"taste graph: top tracks {i}/{len(picks)}")
        for rank, (ta, tt) in enumerate(lf.top_tracks(names[a])):
            offer(ta, tt, s * 0.9 ** rank)
    for (a, t), _ in hot:
        for b, u, m in lf.similar_tracks(a, t):  # cached by now
            offer(b, u, 0.6 * scores.get(norm(b), 0.0) + 0.4 * min(1.0, m))

    why = {a: [s for s, _ in led_by[a].most_common(2)] for a in ranked[:300] if led_by.get(a)}
    return Taste(loved=lf.loved(user) if user else [], artist_score=scores,
                 recs=sorted(recs.values(), key=lambda r: -r[2]), why=why)
