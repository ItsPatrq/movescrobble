"""Runs on a Garmin watch: what Last.fm says you played during each one, written onto the activity.

Unlike a swim, nothing is planned here: the music comes from your phone, which scrobbles it, so the scrobbles inside
the run's time window are the soundtrack. A phone that was offline scrobbles late, so recent runs are checked again on
every sync and an activity is rewritten only when its list changed.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

from . import garmin, history, lfhistory
from .garmin import Run


def tracks_during(db, run: Run) -> list[tuple[str, str]]:
    rows = db.execute("SELECT artist, title FROM scrobbles WHERE ts >= ? AND ts < ? ORDER BY ts",
                      (run.start_ts, run.start_ts + run.seconds))
    return [(r["artist"], r["title"]) for r in rows]


def soundtrack(tracks: list[tuple[str, str]]) -> str:
    lines = [f"{garmin.RUN_SOUNDTRACK_MARK} (movescrobble):"]
    lines += [f"{i}. {artist} - {title}" for i, (artist, title) in enumerate(tracks, 1)]
    return "\n".join(lines)


def sync(db, cfg, days: int = 30, fetch: Callable[[str, str], list[dict]] | None = None,
         write: Callable[[int, str], None] | None = None, pull_history: Callable[[], object] | None = None) -> str:
    """Bring the last `days` of runs up to date. Safe to run any number of times."""
    if not cfg.lastfm_user:
        return "runs skipped (no lastfm_user)"
    fetch = fetch or garmin.fetch_runs
    write = write or garmin.write_soundtrack
    if pull_history:
        pull_history()
    else:  # the plays of the run itself are usually newer than the last pull
        lfhistory.sync_history(db, cfg.lastfm_user, first_since=int(time.time()) - cfg.history_days * 86400)
    now = datetime.now()
    found = [r for a in fetch((now - timedelta(days=days)).date().isoformat(), now.date().isoformat())
             if (r := garmin.parse_run(a))]
    written, failures, failure = 0, 0, ""
    for run in found:
        tracks = tracks_during(db, run)
        if not tracks:
            continue
        body = soundtrack(tracks)
        if history.run_soundtrack(db, run.activity_id) == body:
            continue
        try:
            write(run.activity_id, body)
        except Exception as e:  # not remembered as written, so the next sync tries again
            failures, failure = failures + 1, str(e)
            continue
        history.set_run_soundtrack(db, run.activity_id, body)
        written += 1
    parts = [f"{len(found)} runs in {days} days"]
    if written:
        parts.append(f"soundtrack on {written}")
    if failures:
        parts.append(f"{failures} FAILED: {failure}")
    return ", ".join(parts)
