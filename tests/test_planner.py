from datetime import datetime, timedelta
from pathlib import Path

from movescrobble.library import Track
from movescrobble.planner import plan_session, total_seconds
from movescrobble.scrobble import build_scrobbles


def make(n, dur=240):
    return [Track(Path(f"t{i}.mp3"), f"A{i % 3}", f"T{i}", "", dur) for i in range(n)]


def test_fits_budget():
    plan = plan_session(make(40), minutes=60, seed=1)
    assert total_seconds(plan) <= 3600
    assert len(plan) == 15


def test_seed_reproducible():
    a = [p.track.title for p in plan_session(make(40), seed=7)]
    b = [p.track.title for p in plan_session(make(40), seed=7)]
    assert a == b


def test_staged_names_sort_in_play_order():
    plan = plan_session(make(40), seed=3)
    names = [p.staged_name for p in plan]
    assert names == sorted(names)


def test_scrobble_timestamps_sequential():
    plan = plan_session(make(10), minutes=60, seed=1)
    s = build_scrobbles(plan, datetime(2026, 10, 25, 7, 0))
    assert s[1].timestamp - s[0].timestamp == timedelta(seconds=240)


def test_duplicates_removed():
    dup = make(5) + [Track(Path("copy.mp3"), "A0", "T0", "other album", 240)]
    plan = plan_session(dup, minutes=600, seed=1)
    keys = [(p.track.artist, p.track.title) for p in plan]
    assert len(keys) == len(set(keys)) == 5
