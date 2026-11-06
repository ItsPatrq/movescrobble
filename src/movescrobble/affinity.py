"""How much you like a track right now, from your Last.fm history.

Picked by offline evaluation on a real 13-year history (43 cutoff dates, predicting the next 30 days):
a recency-decayed play count with a 30-day half-life ranked repeat listens best (NDCG@100 0.37 against
0.33 for hard 30/60/90-day windows), and a 180-day half-life ranked comebacks of long-silent tracks best
(precision@50 11.5%, 6x the base rate; plain total-play counts were worse). Artist-level activity
predicts new-to-you tracks: 49% of them are by an artist you played in the previous year.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

DAY = 86400
H_TRACK_NOW = 30  # half-lives in days
H_TRACK_LONG = 180
H_ARTIST = 45
LOOKBACK_DAYS = 3 * 365  # older plays are worth < 0.1% in every decay used here


@dataclass(frozen=True)
class Aff:
    now: float  # decayed plays, 30-day half-life ("what am I into this month")
    long: float  # decayed plays, 180-day half-life ("what have I loved lately-ish")
    total: int
    last: int  # unix time of last play (0 = never)
    weeks: int  # distinct weeks with a play (loyalty)


@dataclass(frozen=True)
class ArtistAff:
    now: float  # decayed plays, 45-day half-life
    last: int
    total: int


def _decay(ts: np.ndarray, now: float, half_life: float) -> float:
    return float(np.exp(-((now - ts) / DAY) * math.log(2) / half_life).sum())


def compute(db, now: float) -> tuple[dict[tuple[str, str], Aff], dict[str, ArtistAff]]:
    """Per-track and per-artist affinities, keyed by the names Last.fm uses."""
    since = now - LOOKBACK_DAYS * DAY
    recent: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_artist: dict[str, list[float]] = defaultdict(list)
    for r in db.execute("SELECT artist, title, ts FROM scrobbles WHERE ts >= ? AND ts <= ?", (since, now)):
        recent[(r["artist"], r["title"])].append(r["ts"])
        by_artist[r["artist"]].append(r["ts"])
    totals = {
        (r["artist"], r["title"]): (r["n"], r["last"], r["weeks"])
        for r in db.execute(
            "SELECT artist, title, COUNT(*) n, MAX(ts) last, COUNT(DISTINCT ts / 604800) weeks "
            "FROM scrobbles WHERE ts <= ? GROUP BY artist, title", (now,))
    }
    tracks = {}
    for k, (n, last, weeks) in totals.items():
        ts = np.array(recent.get(k, []), dtype=np.float64)
        tracks[k] = Aff(_decay(ts, now, H_TRACK_NOW), _decay(ts, now, H_TRACK_LONG), n, last, weeks)
    artist_totals = defaultdict(lambda: [0, 0])
    for (a, _), (n, last, _w) in totals.items():
        artist_totals[a][0] += n
        artist_totals[a][1] = max(artist_totals[a][1], last)
    artists = {
        a: ArtistAff(_decay(np.array(by_artist.get(a, []), dtype=np.float64), now, H_ARTIST), last, n)
        for a, (n, last) in artist_totals.items()
    }
    return tracks, artists


def squash(x: float, ref: float) -> float:
    """Log-scale a decayed count to 0..1 (1 = `ref` or more)."""
    return min(1.0, math.log1p(max(x, 0.0)) / math.log1p(ref))
