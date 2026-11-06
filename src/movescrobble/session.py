"""Session manifests: the contract between planning and scrobbling."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .library import Track
from .planner import PlannedTrack


def to_manifest(plan: list[PlannedTrack], seed: int | None, minutes: float) -> dict:
    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "minutes": minutes,
        "tracks": [
            {"position": p.position, "file": str(p.track.path), "staged": p.staged_name,
             "artist": p.track.artist, "title": p.track.title, "album": p.track.album,
             "duration": round(p.track.duration),
             "source": p.source, "energy": p.energy}
            for p in plan
        ],
    }


def from_manifest(manifest: dict) -> list[PlannedTrack]:
    return [
        PlannedTrack(
            t["position"],
            Track(Path(t["file"]), t["artist"], t["title"], t["album"], t["duration"]),
            t.get("source", ""),
            t.get("energy"),
        )
        for t in manifest["tracks"]
    ]


def played_part(
    manifest: dict, played_minutes: float | None = None, skipped: set[int] = frozenset()
) -> tuple[list[PlannedTrack], float]:
    """The tracks that count as heard, and how many seconds were listened.

    The staged list is longer than the swim (buffer), so by default only the first `minutes` of it
    counts; `played_minutes` overrides that (stopped early, or swam longer).
    """
    from .scrobble import trim_to_played

    plan = [p for p in from_manifest(manifest) if p.position not in skipped]
    total = sum(p.track.duration for p in plan)
    minutes = played_minutes if played_minutes is not None else manifest.get("minutes")
    seconds = min(total, minutes * 60) if minutes is not None else total
    return trim_to_played(plan, seconds), seconds
