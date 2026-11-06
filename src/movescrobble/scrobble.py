"""Turn a played session into Last.fm-style scrobble records."""
from __future__ import annotations

import csv
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .planner import PlannedTrack


@dataclass(frozen=True)
class Scrobble:
    timestamp: datetime
    artist: str
    title: str
    album: str
    duration: int


def build_scrobbles(plan: list[PlannedTrack], start: datetime) -> list[Scrobble]:
    """Back-to-back playback from `start`; a scrobble's timestamp is when the track began."""
    out = []
    t = start
    for p in plan:
        out.append(
            Scrobble(t, p.track.artist, p.track.title, p.track.album, round(p.track.duration))
        )
        t += timedelta(seconds=p.track.duration)
    return out


def to_csv(scrobbles: list[Scrobble]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "artist", "track", "album", "duration"])
    for s in scrobbles:
        w.writerow([s.timestamp.isoformat(timespec="seconds"), s.artist, s.title, s.album, s.duration])
    return buf.getvalue()


MIN_TRACK_SECONDS = 30
SCROBBLE_CAP_SECONDS = 240  # Last.fm: half the track or four minutes, whichever is sooner


def trim_to_played(plan: list[PlannedTrack], played_seconds: float) -> list[PlannedTrack]:
    """Keep tracks played far enough to count; a cut-off last track needs half (or 4 min)."""
    out, elapsed = [], 0.0
    for p in plan:
        d = p.track.duration
        if elapsed >= played_seconds:
            break
        heard = min(d, played_seconds - elapsed)
        if d >= MIN_TRACK_SECONDS and heard >= min(d / 2, SCROBBLE_CAP_SECONDS):
            out.append(p)
        elapsed += d
    return out


def skip_positions(plan: list[PlannedTrack], skipped: set[int]) -> list[PlannedTrack]:
    return [p for p in plan if p.position not in skipped]


def confirm(scrobbles: list[Scrobble], ask: Callable[[str], str] = input) -> bool:
    for s in scrobbles:
        print(f"{s.timestamp:%Y-%m-%d %H:%M:%S}  {s.artist} - {s.title}")
    return ask(f"Submit {len(scrobbles)} scrobbles? [y/N] ").strip().lower() == "y"
