"""Power songs: how fast you swam while each track played, from the watch's per-length data.

Every pool length has a start time, so it belongs to the track that was playing then. Three things keep that honest:
- the music starts before the watch (shower, warm-up: `warmup_minutes`), and that gap is a guess, so each length is
  spread over the tracks playing within +-UNCERTAINTY_MINUTES of it rather than pinned to one;
- the watch's mistakes are repaired: a missed turn (double time, double strokes) is two lengths, a stop is dropped;
- each length is compared with your average for the same stroke in that swim (breaststroke is not slow freestyle).
Per track that gives a speed relative to you that day, averaged over swims and shrunk towards zero until there are
enough lengths to trust. The mix then favours the tracks that make you faster.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median

from . import garmin
from .garmin import Length, Swim
from .planner import fmt_min

SHRINK_LENGTHS = 6  # a track needs about this many lengths before its own speed counts as much as "average"
MIN_LENGTHS = 2  # fewer than this in a swim (a track mostly heard in the shower) says nothing
UNCERTAINTY_MINUTES = 2.5  # how far off the warm-up guess may be
SPREAD_STEPS = 11  # offsets tried across that range
SLOW_LENGTH = 1.6  # a length this many times the median is a missed turn (if strokes doubled too) or a stop


@dataclass(frozen=True)
class Heard:
    artist: str
    title: str
    start: datetime
    seconds: float


@dataclass(frozen=True)
class Effort:
    artist: str
    title: str
    lengths: float  # how many lengths fell in this track (fractional: spread over the timing uncertainty)
    distance_m: float
    seconds: float
    hr: float | None
    swolf: float | None
    rel_speed: float  # +0.05 = 5% faster than your average for the stroke that day

    @property
    def pace(self) -> float:
        return self.seconds / (self.distance_m / 100)


def heard_in_session(db, session_id: int) -> list[Heard]:
    """What the playlist played and when: the scrobbled tracks (one play each, at its start), then the rest of the
    playlist in order, since the music kept going while you swam on."""
    row = db.execute("SELECT manifest FROM sessions WHERE id=?", (session_id,)).fetchone()
    order = sorted(json.loads(row["manifest"])["tracks"], key=lambda t: t["position"])
    durations = {(t["artist"], t["title"]): t["duration"] for t in order}
    plays = db.execute("SELECT artist, title, ts FROM plays WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
    heard = [Heard(p["artist"], p["title"], datetime.fromtimestamp(p["ts"]), durations.get((p["artist"], p["title"]), 0))
             for p in plays]
    if heard:
        last = next((i for i, t in enumerate(order) if (t["artist"], t["title"]) == (heard[-1].artist, heard[-1].title)),
                    len(order))
        t = heard[-1].start + timedelta(seconds=heard[-1].seconds)
        for tr in order[last + 1:]:
            heard.append(Heard(tr["artist"], tr["title"], t, tr["duration"]))
            t += timedelta(seconds=tr["duration"])
    return heard


def repaired(lengths: list[Length]) -> list[Length]:
    """A missed turn (twice the time and the strokes) becomes the two lengths it was; a stop at the wall is dropped."""
    if not lengths:
        return []
    typical = median(n.seconds for n in lengths)
    strokes = [n.strokes for n in lengths if n.strokes]
    typical_strokes = median(strokes) if strokes else None
    out = []
    for n in lengths:
        if n.seconds < SLOW_LENGTH * typical:
            out.append(n)
        elif typical_strokes and n.strokes and n.strokes >= SLOW_LENGTH * typical_strokes:
            out.append(replace(n, distance_m=n.distance_m * 2))
    return out


def _track_at(heard: list[Heard], t: datetime) -> int | None:
    return next((i for i, h in enumerate(heard) if h.start <= t < h.start + timedelta(seconds=h.seconds)), None)


def analyse(heard: list[Heard], lengths: list[Length], uncertainty_minutes: float = UNCERTAINTY_MINUTES) -> list[Effort]:
    """Split the swim's lengths between the tracks that were playing; tracks with too few lengths are left out."""
    lengths = repaired(lengths)
    if not lengths:
        return []
    by_stroke: dict[str | None, list[Length]] = {}
    for n in lengths:
        by_stroke.setdefault(n.stroke, []).append(n)
    speed = lambda ns: sum(n.distance_m for n in ns) / sum(n.seconds for n in ns)  # noqa: E731
    typical = {k: speed(ns) if len(ns) >= 3 else speed(lengths) for k, ns in by_stroke.items()}
    shifts = [timedelta(minutes=uncertainty_minutes * (2 * i / (SPREAD_STEPS - 1) - 1)) for i in range(SPREAD_STEPS)] \
        if uncertainty_minutes else [timedelta(0)]
    acc: dict[int, dict[str, float]] = {}
    for n in lengths:
        mid = n.start + timedelta(seconds=n.seconds / 2)
        rel = n.distance_m / n.seconds / typical[n.stroke]
        for shift in shifts:
            i = _track_at(heard, mid + shift)
            if i is None:
                continue
            a = acc.setdefault(i, dict.fromkeys(("w", "rel", "dist", "secs", "hr_w", "hr", "swolf_w", "swolf"), 0.0))
            w = 1 / len(shifts)
            a["w"] += w
            a["rel"] += w * n.seconds * rel
            a["dist"] += w * n.distance_m
            a["secs"] += w * n.seconds
            if n.hr:
                a["hr_w"] += w * n.seconds
                a["hr"] += w * n.seconds * n.hr
            if n.swolf:
                a["swolf_w"] += w * n.seconds
                a["swolf"] += w * n.seconds * n.swolf
    return [
        Effort(heard[i].artist, heard[i].title, round(a["w"], 2), a["dist"], a["secs"],
               a["hr"] / a["hr_w"] if a["hr_w"] else None, a["swolf"] / a["swolf_w"] if a["swolf_w"] else None,
               a["rel"] / a["secs"] - 1)
        for i, a in sorted(acc.items()) if a["w"] >= MIN_LENGTHS
    ]


def store_swim(db, s: Swim) -> None:
    """Remember a Garmin swim (for the recap); keeps the session link and analysis of an already-known one."""
    db.execute(
        "INSERT INTO swims(activity_id, start, kind, distance_m, elapsed_s, moving_s, avg_hr, max_hr, swolf, hr_zones) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(activity_id) DO UPDATE SET start=excluded.start, kind=excluded.kind, "
        "distance_m=excluded.distance_m, elapsed_s=excluded.elapsed_s, moving_s=excluded.moving_s, "
        "avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, swolf=excluded.swolf, hr_zones=excluded.hr_zones",
        (s.activity_id, s.start.timestamp(), s.kind, s.distance_m, s.minutes * 60,
         s.moving_minutes * 60 if s.moving_minutes else None, s.avg_hr, s.max_hr, s.swolf, json.dumps(s.hr_zones)),
    )


def store_efforts(db, activity_id: int, session_id: int, efforts: list[Effort]) -> None:
    db.execute("DELETE FROM swim_tracks WHERE activity_id=?", (activity_id,))
    db.executemany(
        "INSERT INTO swim_tracks(activity_id, artist, title, lengths, distance_m, seconds, hr, swolf, rel_speed) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [(activity_id, e.artist, e.title, e.lengths, e.distance_m, e.seconds, e.hr, e.swolf, e.rel_speed)
         for e in efforts])
    db.execute("UPDATE swims SET session_id=?, analysed=1 WHERE activity_id=?", (session_id, activity_id))


def _scored(db) -> list[tuple[str, str, float]]:
    """(artist, title, how much faster you swim to it), best first; shrunk towards 0 while there are few lengths."""
    return [(r["artist"], r["title"], r["score"]) for r in db.execute(
        "SELECT artist, title, SUM(rel_speed * lengths) / (SUM(lengths) + ?) score FROM swim_tracks "
        "GROUP BY lower(artist), lower(title) ORDER BY score DESC", (SHRINK_LENGTHS,))]


def scores(db) -> dict[tuple[str, str], float]:
    """Track (casefolded, like play_counts) -> score, for the mix."""
    return {(a.casefold(), t.casefold()): score for a, t, score in _scored(db)}


def leaderboard(db, n: int = 3) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """(fastest, slowest) tracks, each (artist, title, score); only tracks with a clear effect (1%+)."""
    ranked = _scored(db)
    return [r for r in ranked if r[2] >= 0.01][:n], [r for r in reversed(ranked) if r[2] <= -0.01][:n]


def soundtrack(heard: list[Heard], efforts: list[Effort]) -> str:
    timed = {(e.artist, e.title): e for e in efforts}
    lines = [f"{garmin.SOUNDTRACK_MARK} (movescrobble):"]
    for i, h in enumerate(heard, 1):
        e = timed.get((h.artist, h.title))
        lines.append(f"{i}. {h.artist} - {h.title}" + (f" ({fmt_min(round(e.pace))}/100m)" if e else ""))
    return "\n".join(lines)


def sync(db, cfg, days: int = 60, fetch_swims: Callable[[str, str], list[dict]] | None = None,
         fetch_lengths: Callable[[Swim], list[Length]] | None = None,
         write_soundtrack: Callable[[int, str], None] | None = None) -> str:
    """Remember the last `days` of Garmin swims, then time the tracks of every scrobbled session that has one
    (and, with `garmin_soundtrack`, write its soundtrack onto the activity). Safe to run any number of times."""
    fetch_swims = fetch_swims or garmin.fetch_swims
    fetch_lengths = fetch_lengths or garmin.fetch_lengths
    write_soundtrack = write_soundtrack or garmin.write_soundtrack
    now = datetime.now()
    swims = [s for a in fetch_swims((now - timedelta(days=days)).date().isoformat(), now.date().isoformat())
             if (s := garmin.parse(a)) and s.activity_id]
    for s in swims:
        store_swim(db, s)
    db.commit()
    taken = {r["activity_id"] for r in db.execute("SELECT activity_id FROM swims WHERE analysed=1")}
    analysed, written, failed = [], 0, ""
    sessions = db.execute("SELECT id, planned_at, scrobbled_at FROM sessions WHERE status='scrobbled' AND id NOT IN "
                          "(SELECT session_id FROM swims WHERE analysed=1 AND session_id IS NOT NULL)").fetchall()
    for row in sessions:
        heard = heard_in_session(db, row["id"])
        if not heard:
            continue
        first, last = heard[0].start, heard[-1].start + timedelta(seconds=heard[-1].seconds)
        swim = min((s for s in swims if s.activity_id not in taken and s.start < last
                    and s.start + timedelta(minutes=s.minutes) > first), key=lambda s: s.start, default=None)
        if swim is None:
            continue
        heard = [h for h in heard if h.start < swim.start + timedelta(minutes=swim.minutes)]
        efforts = analyse(heard, fetch_lengths(swim))
        store_efforts(db, swim.activity_id, row["id"], efforts)
        db.commit()
        taken.add(swim.activity_id)
        analysed.append(efforts)
        if cfg.garmin_soundtrack:
            try:
                write_soundtrack(swim.activity_id, soundtrack(heard, efforts))
                written += 1
            except Exception as e:  # the timing is stored either way; only the description is missing
                failed = f"soundtrack FAILED: {e}"
    parts = [f"{len(swims)} swims in {days} days"]
    if analysed:
        parts.append(f"timed {sum(map(len, analysed))} tracks in {len(analysed)} new swim(s)")
        best = max((e for es in analysed for e in es), key=lambda e: e.rel_speed, default=None)
        if best and best.rel_speed > 0:
            parts.append(f"fastest to {best.artist} - {best.title} ({best.rel_speed:+.0%})")
    if written:
        parts.append(f"soundtrack on {written} activit{'y' if written == 1 else 'ies'}")
    if failed:
        parts.append(failed)
    return ", ".join(parts)
