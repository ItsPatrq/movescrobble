"""Your full Last.fm listening history, mirrored locally and updated incrementally.

The web profile shows per-track plays and last-played dates; the API exposes the same data through
`user.getRecentTracks` (every scrobble, 200 per page, filterable by time). Pulling it once and
topping it up later gives exact answers ("never played", "last heard 140 days ago") instead of the
approximate top-1000 lists.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import history
from .taste import Transport, _get, http_get

DAY = 86400


@dataclass(frozen=True)
class Stat:
    total: int
    p30: int  # plays in the last 30 days
    p60: int  # 30-60 days ago
    p90: int  # 60-90 days ago
    last: int  # unix time of the most recent play


def _meta(db, key: str) -> int | None:
    value = history.get_meta(db, key)
    return int(value) if value is not None else None


def sync_history(
    db,
    user: str,
    transport: Transport = http_get,
    sleep: Callable[[float], None] = time.sleep,
    page_size: int = 200,
    log: Callable[[str], None] | None = None,
    first_since: int = 0,
) -> int:
    """Fetch scrobbles newer than the last completed sync (on the very first run, newer than
    `first_since`, so a decade of history is not downloaded when a year is plenty).

    Pages arrive newest first, so an interrupted pull records how far back it got (`resume_to`) and
    the next run continues from there instead of starting over. Rows are idempotent.
    """
    since = _meta(db, "lastfm_history_ts") or first_since
    resume_to = _meta(db, "history_resume_to")  # oldest scrobble of the interrupted pull
    newest = _meta(db, "history_pull_newest") or since
    added, page = 0, 1
    while True:
        params = {"user": user, "limit": page_size, "page": page, "extended": 0}
        if since:
            params["from"] = since + 1
        if resume_to:
            params["to"] = resume_to
        data = _get("user.getRecentTracks", transport, **params)["recenttracks"]
        tracks = data.get("track", [])
        if isinstance(tracks, dict):  # a single result is not wrapped in a list
            tracks = [tracks]
        rows = []
        for t in tracks:
            if t.get("@attr", {}).get("nowplaying") or "date" not in t:
                continue
            rows.append((int(t["date"]["uts"]), t["artist"]["#text"], t["name"]))
        before = db.total_changes
        db.executemany("INSERT OR IGNORE INTO scrobbles(ts, artist, title) VALUES(?,?,?)", rows)
        added += db.total_changes - before
        if rows:
            newest = max(newest, max(r[0] for r in rows))
            resume_to = min(r[0] for r in rows)
            history.set_meta(db, "history_resume_to", resume_to)
            history.set_meta(db, "history_pull_newest", newest)
        db.commit()
        pages = int(data.get("@attr", {}).get("totalPages", 1))
        if log and page % 25 == 0:
            log(f"  page {page}/{pages}, {added} new scrobbles so far")
        if page >= pages:
            break
        page += 1
        sleep(0.25)  # stay well under Last.fm's rate limit
    history.set_meta(db, "lastfm_history_ts", newest)
    history.set_meta(db, "history_resume_to", None)
    history.set_meta(db, "history_pull_newest", None)
    db.commit()
    return added


def stats(db, now: float | None = None) -> dict[tuple[str, str], Stat]:
    """Per (artist, title) play counts by window, as spelled on Last.fm."""
    now = int(now if now is not None else time.time())
    t30, t60, t90 = now - 30 * DAY, now - 60 * DAY, now - 90 * DAY
    rows = db.execute(
        """SELECT artist, title, COUNT(*) total,
                  SUM(ts >= ?) p30, SUM(ts >= ? AND ts < ?) p60, SUM(ts >= ? AND ts < ?) p90, MAX(ts) last
           FROM scrobbles GROUP BY artist, title""",
        (t30, t60, t30, t90, t60),
    ).fetchall()
    return {(r["artist"], r["title"]): Stat(r["total"], r["p30"], r["p60"], r["p90"], r["last"]) for r in rows}
