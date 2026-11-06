"""Keep the swim pool in sync with the scored candidates and the local library."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .generator import Candidate, Key, key


@dataclass
class SyncReport:
    added: list[Key] = field(default_factory=list)
    retired: list[Key] = field(default_factory=list)
    missing: list[Candidate] = field(default_factory=list)  # wanted but not in library


def sync_pool(
    db,
    candidates: list[Candidate],
    library_keys: set[Key],
    swim_plays: dict[Key, int],
    max_size: int,
    max_plays: int = 5,
) -> SyncReport:
    """Top `max_size` candidates that exist locally stay in the pool; overplayed ones retire."""
    rep = SyncReport()
    existing = {
        (r["artist"], r["title"]): r["retired"]
        for r in db.execute("SELECT artist, title, retired FROM pool")
    }
    keep: list[Candidate] = []
    for c in candidates:
        k = key(c.artist, c.title)
        if k not in library_keys:
            rep.missing.append(c)
        elif swim_plays.get(k, 0) < max_plays:
            keep.append(c)
    keep = keep[:max_size]
    keep_keys = {key(c.artist, c.title) for c in keep}
    now = time.time()
    for c in keep:
        k = key(c.artist, c.title)
        if k not in existing or existing[k]:
            rep.added.append(k)
        db.execute(
            "INSERT INTO pool(artist,title,source,score,retired,added_at) VALUES(?,?,?,?,0,?) "
            "ON CONFLICT(artist,title) DO UPDATE SET score=excluded.score, source=excluded.source, retired=0",
            (k[0], k[1], ",".join(sorted(c.sources)), c.score, now),
        )
    for k, was_retired in existing.items():
        if k not in keep_keys and not was_retired:
            rep.retired.append(k)
            db.execute("UPDATE pool SET retired=1 WHERE artist=? AND title=?", k)
    db.commit()
    return rep


def active_keys(db) -> set[Key]:
    return {(r["artist"], r["title"]) for r in db.execute("SELECT artist,title FROM pool WHERE retired=0")}
