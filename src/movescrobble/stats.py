"""Numbers for the dashboard and the stats screen (pure queries, no printing)."""
from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field

from . import history, power
from .config import Config, config_dir
from .planner import NEW_TO_YOU, fmt_min

DAY = 86400


@dataclass
class Dashboard:
    device_connected: bool
    device_free_gb: float | None
    staged: dict | None  # id, tracks, minutes, age_hours, swim_tracks, buckets
    scrobbles: int
    scrobbles_30d: int
    pool: dict[str, int] = field(default_factory=dict)
    swum_recently: int = 0
    wishlist: int = 0
    last_sync_hours: float | None = None
    agent_installed: bool = False
    orphans: int = 0  # staged files still on the headphones that belong to no pending session
    orphan_session: int | None = None


def swim_split(manifest: dict, swim_minutes: float) -> tuple[int, float]:
    """(number of tracks inside the swim window, seconds they cover)."""
    t, n = 0.0, 0
    for tr in sorted(manifest["tracks"], key=lambda x: x["position"]):
        if t >= swim_minutes * 60:
            break
        t += tr["duration"]
        n += 1
    return n, t


def wishlist_lines(weights: dict[str, float] | None = None) -> list[tuple[str, str, str]]:
    """(label, bucket, url) from the file `sync` writes. With `weights`, interleaved by bucket share
    (the file is sorted by score, which would put one bucket first)."""
    f = config_dir() / "wishlist.txt"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            out.append((parts[0], parts[1].strip("[]"), parts[2]))
    if weights:
        from types import SimpleNamespace

        from .acquire import balanced

        items = [SimpleNamespace(sources={b}, row=(label, b, url)) for label, b, url in out]
        out = [i.row for i in balanced(items, weights)]
    return out


def dashboard(cfg: Config, db, now: float | None = None, agent_installed: bool = False) -> Dashboard:
    now = now if now is not None else time.time()
    row = history.pending_session(db)
    staged = None
    if row:
        m = json.loads(row["manifest"])
        n_swim, _ = swim_split(m, m.get("minutes", cfg.minutes))
        staged = {
            "id": row["id"], "tracks": len(m["tracks"]), "minutes": sum(t["duration"] for t in m["tracks"]) / 60,
            "age_hours": (now - row["planned_at"]) / 3600, "swim_tracks": n_swim,
            "swim_minutes": m.get("minutes", cfg.minutes),
            "buckets": dict(Counter(t.get("source", "") for t in m["tracks"])),
        }
    free = None
    if cfg.device_path.is_dir():
        free = shutil.disk_usage(cfg.device_path).free / 1e9
    last_sync = history.get_meta(db, "last_sync")
    from .flow import orphans

    n_orphans, orphan_id = orphans(cfg, db)
    return Dashboard(
        device_connected=cfg.device_path.is_dir(), device_free_gb=free, staged=staged,
        scrobbles=db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"],
        scrobbles_30d=db.execute("SELECT COUNT(*) c FROM scrobbles WHERE ts >= ?", (now - 30 * DAY,)).fetchone()["c"],
        pool={r["source"]: r["c"] for r in db.execute(
            "SELECT source, COUNT(*) c FROM pool WHERE retired=0 GROUP BY source ORDER BY c DESC")},
        swum_recently=len(history.recent_plays(db, cfg.no_repeat_days)),
        wishlist=len(wishlist_lines()),
        last_sync_hours=(now - float(last_sync)) / 3600 if last_sync else None,
        agent_installed=agent_installed, orphans=n_orphans, orphan_session=orphan_id,
    )


def top_artists(db, days: int = 30, n: int = 8, now: float | None = None) -> list[tuple[str, int]]:
    now = now if now is not None else time.time()
    return [(r["artist"], r["c"]) for r in db.execute(
        "SELECT artist, COUNT(*) c FROM scrobbles WHERE ts >= ? GROUP BY artist ORDER BY c DESC LIMIT ?",
        (now - days * DAY, n))]


def top_tracks(db, days: int = 30, n: int = 8, now: float | None = None) -> list[tuple[str, str, int]]:
    now = now if now is not None else time.time()
    from .buckets import looks_like_song

    rows = db.execute(
        "SELECT artist, title, COUNT(*) c FROM scrobbles WHERE ts >= ? GROUP BY artist, title ORDER BY c DESC LIMIT ?",
        (now - days * DAY, n * 4))
    return [(r["artist"], r["title"], r["c"]) for r in rows if looks_like_song(r["title"])][:n]  # no podcast episodes


def swim_stats(db) -> dict:
    sessions = {r["status"]: r["c"] for r in db.execute("SELECT status, COUNT(*) c FROM sessions GROUP BY status")}
    scrobbled = db.execute("SELECT SUM(COALESCE(swum_minutes, minutes)) m, MAX(scrobbled_at) last FROM sessions "
                           "WHERE status='scrobbled'").fetchone()
    most = [(r["artist"], r["title"], r["c"]) for r in db.execute(
        "SELECT artist, title, COUNT(*) c FROM plays GROUP BY artist, title ORDER BY c DESC, MAX(ts) DESC LIMIT 5")]
    return {"sessions": sessions, "swim_minutes": scrobbled["m"] or 0.0, "last_swim": scrobbled["last"], "most_swum": most}


@dataclass
class Recap:
    days: int
    swims: int = 0
    minutes: float = 0.0
    distance_m: float = 0.0
    tracks: int = 0
    discoveries: list[tuple[str, str]] = field(default_factory=list)  # new-to-you tracks you heard and kept
    skipped: int = 0
    top_artists: list[tuple[str, int]] = field(default_factory=list)
    # from Garmin (every swim the watch recorded, with or without the headphones)
    paces: list[float] = field(default_factory=list)  # s/100m of the last swims, oldest first
    weekly_km: list[float] = field(default_factory=list)  # the last 4 weeks, oldest first
    avg_hr: float | None = None
    hr_zones: list[float] = field(default_factory=list)  # share of time in zones 1-5
    fastest: list[tuple[str, str, float]] = field(default_factory=list)  # power songs
    slowest: list[tuple[str, str, float]] = field(default_factory=list)


    def lines(self) -> list[str]:
        return self._music_lines() + self._garmin_lines()

    def _garmin_lines(self) -> list[str]:
        out = []
        if self.paces:
            trend = " → ".join(fmt_min(round(p)) for p in self.paces)
            out.append(f"Pace per 100 m, last {len(self.paces)} swims: {trend} (best {fmt_min(round(min(self.paces)))})")
        if any(self.weekly_km):
            out.append("Distance per week, last 4: " + " · ".join(f"{k:.1f}" for k in self.weekly_km) + " km")
        if self.avg_hr:
            zones = " ".join(f"{z:.0%}" for z in self.hr_zones)
            out.append(f"Heart rate: avg {self.avg_hr:.0f} bpm" + (f" · time in zones 1-5: {zones}" if zones else ""))
        if self.fastest:
            out.append("Power songs (you swim faster): " + " · ".join(f"{a} - {t} {s:+.0%}" for a, t, s in self.fastest))
        if self.slowest:
            out.append("Slow you down: " + " · ".join(f"{a} - {t} {s:+.0%}" for a, t, s in self.slowest))
        return out

    def _music_lines(self) -> list[str]:
        if not self.swims:
            return [f"No swims scrobbled in the last {self.days} days."]
        head = [f"{self.swims} swims", f"{self.minutes / 60:.1f} h"]
        if self.distance_m:
            head.append(f"{self.distance_m / 1000:.1f} km")
        head.append(f"{self.tracks} tracks heard")
        if self.skipped:
            head.append(f"{self.skipped} skipped")
        out = [f"Last {self.days} days: " + ", ".join(head)]
        if self.top_artists:
            out.append("Most swum artists: " + " · ".join(f"{a} ×{n}" for a, n in self.top_artists))
        if self.discoveries:
            out.append(f"{len(self.discoveries)} discoveries: " + " · ".join(f"{a} - {t}" for a, t in self.discoveries[:10]))
        return out


def recap(db, days: int = 7, now: float | None = None) -> Recap:
    """What the last `days` of swimming looked like."""
    now = now if now is not None else time.time()
    r = Recap(days)
    window = "SELECT id FROM sessions WHERE status='scrobbled' AND scrobbled_at >= :since"
    since = {"since": now - days * DAY}
    source = {}  # (session, artist, title) -> bucket
    for s in db.execute(f"SELECT id, manifest, minutes, swum_minutes, distance_m FROM sessions WHERE id IN ({window})",
                        since):
        r.swims += 1
        r.minutes += s["swum_minutes"] if s["swum_minutes"] is not None else s["minutes"] or 0
        r.distance_m += s["distance_m"] or 0
        source.update({(s["id"], t["artist"], t["title"]): t.get("source") for t in json.loads(s["manifest"])["tracks"]})
    artists: Counter = Counter()
    for p in db.execute(f"SELECT session_id, artist, title FROM plays WHERE session_id IN ({window}) ORDER BY ts", since):
        r.tracks += 1
        artists[p["artist"]] += 1
        if source.get((p["session_id"], p["artist"], p["title"])) in NEW_TO_YOU:
            r.discoveries.append((p["artist"], p["title"]))
    r.skipped = db.execute(f"SELECT COUNT(*) c FROM skips WHERE session_id IN ({window})", since).fetchone()["c"]
    r.top_artists = artists.most_common(5)
    garmin_recap(db, r, now)
    return r


def garmin_recap(db, r: Recap, now: float, trend: int = 6) -> None:
    """Pace trend, weekly distance, heart rate and power songs, from the swims Garmin sync remembered."""
    rows = db.execute("SELECT start, distance_m, elapsed_s, moving_s, avg_hr, hr_zones FROM swims "
                      "WHERE start <= ? ORDER BY start", (now,)).fetchall()
    r.paces = [(s["moving_s"] or s["elapsed_s"]) / (s["distance_m"] / 100) for s in rows if s["distance_m"]][-trend:]
    r.weekly_km = [sum(s["distance_m"] or 0 for s in rows if now - (w + 1) * 7 * DAY < s["start"] <= now - w * 7 * DAY)
                   / 1000 for w in range(3, -1, -1)]
    recent = [s for s in rows if s["start"] > now - r.days * DAY]
    timed = [(s["avg_hr"], s["moving_s"] or s["elapsed_s"]) for s in recent if s["avg_hr"]]
    if timed:
        r.avg_hr = sum(h * t for h, t in timed) / sum(t for _, t in timed)
    rows = [json.loads(s["hr_zones"]) for s in recent if s["hr_zones"] not in (None, "[]")]
    zones = [sum(z) for z in zip(*rows, strict=False)]  # a swim with fewer zones just adds to the first ones
    if sum(zones):
        r.hr_zones = [z / sum(zones) for z in zones]
    r.fastest, r.slowest = power.leaderboard(db)


def listening_mix(db, days: int = 30, now: float | None = None) -> dict[str, float]:
    """Of the tracks you played in the window: how many were new to you, how many returned after a year?"""
    now = now if now is not None else time.time()
    start = now - days * DAY
    rows = db.execute(
        """SELECT w.artist, w.title, MIN(a.ts) first_ever, MAX(CASE WHEN a.ts < ? THEN a.ts END) last_before
           FROM (SELECT DISTINCT artist, title FROM scrobbles WHERE ts >= ?) w
           JOIN scrobbles a ON a.artist = w.artist AND a.title = w.title
           GROUP BY w.artist, w.title""", (start, start)).fetchall()
    if not rows:
        return {}
    n = len(rows)
    new = sum(1 for r in rows if r["first_ever"] >= start)
    back = sum(1 for r in rows if r["last_before"] and start - r["last_before"] > 365 * DAY)
    return {"tracks": float(n), "new to you": new / n, "back after 1y+": back / n, "repeat": 1 - (new + back) / n}
