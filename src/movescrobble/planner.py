"""Build a randomised session that fits a time budget.

The headphones play files in alphabetical order, so the plan assigns each
chosen track a zero-padded position prefix. Alphabetical order == play order.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from .library import Track

STAGED_RE = re.compile(r"^\d{3} - ")


def staged_name(position: int, path: Path | str) -> str:
    """Device filename: the zero-padded prefix is the play order (matched by STAGED_RE)."""
    return f"{position:03d} - {Path(path).name}"


NEW_TO_YOU = frozenset({"fresh", "rec"})  # buckets of tracks you had not played before: the discoveries


def parse_positions(text: str | None) -> set[int]:
    """'3, 7,12' -> {3, 7, 12}; anything that is not a number is ignored."""
    return {int(x) for x in (text or "").replace(" ", "").split(",") if x.isdigit()}


def fmt_min(seconds: float) -> str:
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


@dataclass(frozen=True)
class PlannedTrack:
    position: int
    track: Track
    source: str = ""  # "fav" | "rec" when built by the mixer
    energy: float | None = None

    @property
    def staged_name(self) -> str:
        return staged_name(self.position, self.track.path)


def plan_session(
    tracks: list[Track],
    minutes: float = 60,
    seed: int | None = None,
    avoid_repeat_artist: bool = True,
    exclude: set[tuple[str, str]] | None = None,
    arc: bool = False,
    weights: dict[tuple[str, str], float] | None = None,
) -> list[PlannedTrack]:
    """Shuffle, then greedily take every track that still fits the budget."""
    rng = random.Random(seed)
    pool = dedupe(tracks)
    if exclude:
        # recently played; fall back to the full pool if exclusion would leave too little
        fresh = [t for t in pool if key_of(t) not in exclude]
        if sum(t.duration for t in fresh) >= minutes * 60:
            pool = fresh
    if weights:
        # Efraimidis-Spirakis weighted shuffle: higher weight -> likelier to land early
        floor = 0.05
        pool.sort(
            key=lambda t: rng.random() ** (1 / max(weights.get(key_of(t), floor), 1e-9)),
            reverse=True,
        )
    else:
        rng.shuffle(pool)
    budget = minutes * 60
    chosen: list[Track] = []
    total = 0.0
    for t in pool:
        if total + t.duration <= budget:
            chosen.append(t)
            total += t.duration
    if arc:
        chosen = bpm_arc(chosen)
    elif avoid_repeat_artist:
        chosen = _spread_artists(chosen)
    return [PlannedTrack(i + 1, t) for i, t in enumerate(chosen)]


def key_of(t: Track) -> tuple[str, str]:
    return (t.artist.casefold(), t.title.casefold())


def bpm_arc(tracks: list[Track]) -> list[Track]:
    """Warm-up -> peak -> cool-down: slowest tracks at the ends, fastest in the middle."""
    known = sorted(t.bpm for t in tracks if t.bpm)
    mid = known[len(known) // 2] if known else 0.0
    ranked = sorted(tracks, key=lambda t: t.bpm or mid)
    front: list[Track] = []
    back: list[Track] = []
    for i, t in enumerate(ranked):
        (front if i % 2 == 0 else back).append(t)
    return front + back[::-1]


def dedupe(tracks: list[Track]) -> list[Track]:
    """Drop repeats of the same artist+title (e.g. a song copied from two albums)."""
    seen: set[tuple[str, str]] = set()
    out = []
    for t in tracks:
        key = key_of(t)
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _spread_artists(tracks: list[Track]) -> list[Track]:
    """Best-effort: avoid the same artist back to back, keeping shuffle otherwise."""
    out: list[Track] = []
    rest = list(tracks)
    while rest:
        prev = out[-1].artist if out else None
        idx = next(
            (i for i, t in enumerate(rest) if not t.artist or t.artist != prev), 0
        )
        out.append(rest.pop(idx))
    return out


def total_seconds(plan: list[PlannedTrack]) -> float:
    return sum(p.track.duration for p in plan)
