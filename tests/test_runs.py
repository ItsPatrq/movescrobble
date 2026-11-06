from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from movescrobble import garmin, history, runs

START = datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc)
T0 = int(START.timestamp())
CFG = SimpleNamespace(lastfm_user="me", history_days=365)


@pytest.fixture
def db(tmp_path):
    return history.connect(tmp_path / "h.db")


def activity(activity_id=1, seconds=1800.0, kind="running", start=START, offset=timedelta(hours=1)):
    return {"activityId": activity_id, "activityType": {"typeKey": kind}, "elapsedDuration": seconds,
            "startTimeLocal": (start + offset).strftime("%Y-%m-%d %H:%M:%S"),
            "startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S")}


def scrobble(db, offset_seconds, artist="A", title="T"):
    db.execute("INSERT INTO scrobbles(ts, artist, title) VALUES(?,?,?)", (T0 + offset_seconds, artist, title))
    db.commit()


def sync(db, activities, cfg=CFG, pull=lambda: None, **kw):
    written = []
    report = runs.sync(db, cfg, fetch=lambda _s, _e: activities, write=lambda i, b: written.append((i, b)),
                       pull_history=pull, **kw)
    return report, written


def test_tracks_played_during_the_run_go_on_the_activity_in_order(db):
    scrobble(db, 600, "Second", "B")
    scrobble(db, 0, "First", "A")
    scrobble(db, -1, "Before", "X")
    scrobble(db, 1800, "After", "Y")
    report, written = sync(db, [activity()])
    assert written == [(1, "♫ Ran to (movescrobble):\n1. First - A\n2. Second - B")]
    assert report == "1 runs in 30 days, soundtrack on 1"


def test_start_time_comes_from_gmt_so_a_run_abroad_still_matches(db):
    scrobble(db, 60)
    _, written = sync(db, [activity(offset=timedelta(hours=-7))])
    assert len(written) == 1


def test_history_is_refreshed_before_matching(db):
    _, written = sync(db, [activity()], pull=lambda: scrobble(db, 60))
    assert len(written) == 1


def test_a_run_without_music_is_left_alone(db):
    report, written = sync(db, [activity()])
    assert written == []
    assert report == "1 runs in 30 days"


def test_a_second_sync_does_not_write_again(db):
    scrobble(db, 60)
    sync(db, [activity()])
    _, written = sync(db, [activity()])
    assert written == []


def test_late_scrobbles_update_the_activity(db):
    """A phone that was offline during the run sends its scrobbles afterwards."""
    scrobble(db, 60, "One", "A")
    sync(db, [activity()])
    scrobble(db, 300, "Two", "B")
    _, written = sync(db, [activity()])
    assert written == [(1, "♫ Ran to (movescrobble):\n1. One - A\n2. Two - B")]


def test_a_failed_write_is_reported_and_retried_without_stopping_the_others(db):
    scrobble(db, 60)
    calls = []

    def flaky(activity_id, _body):
        calls.append(activity_id)
        if activity_id == 1:
            raise OSError("Garmin down")

    other = activity(2)
    report = runs.sync(db, CFG, fetch=lambda _s, _e: [activity(1), other], write=flaky, pull_history=lambda: None)
    assert calls == [1, 2]
    assert "soundtrack on 1" in report and "1 FAILED: Garmin down" in report
    _, written = sync(db, [activity(1), other])
    assert [i for i, _ in written] == [1]


@pytest.mark.parametrize("bad", [activity(kind="lap_swimming"), activity(seconds=0), {"activityId": 3}])
def test_other_activities_and_empty_recordings_are_ignored(db, bad):
    scrobble(db, 60)
    _, written = sync(db, [bad])
    assert written == []


def test_trail_and_treadmill_count_as_runs(db):
    scrobble(db, 60)
    _, written = sync(db, [activity(1, kind="trail_running"), activity(2, kind="treadmill_running")])
    assert [i for i, _ in written] == [1, 2]


def test_without_a_lastfm_user_nothing_is_read_or_written(db):
    def boom(_s, _e):
        raise AssertionError("should not ask Garmin")

    report = runs.sync(db, SimpleNamespace(lastfm_user=""), fetch=boom, write=boom, pull_history=lambda: None)
    assert report == "runs skipped (no lastfm_user)"


def test_your_own_description_survives_and_an_old_list_is_replaced():
    first = garmin.with_soundtrack("Felt great", "♫ Ran to (movescrobble):\n1. X")
    assert garmin.with_soundtrack(first, "♫ Ran to (movescrobble):\n1. Y") == (
        "Felt great\n\n♫ Ran to (movescrobble):\n1. Y")


def test_a_run_never_counts_as_the_swim_that_settles_a_session():
    """Only a swim proves the staged playlist was swum to; a run just reads what Last.fm already has."""
    run = {"activityId": 1, "activityType": {"typeKey": "running"}, "elapsedDuration": 1800.0,
           "startTimeLocal": "2026-11-04 23:00:00", "startTimeGMT": "2026-11-04 22:00:00"}
    assert garmin.swim_between(datetime(2026, 11, 4, 20), datetime(2026, 11, 5, 1), lambda _s, _e: [run]) is None


def test_a_run_sync_leaves_the_staged_session_pending(db):
    db.execute("INSERT INTO sessions(id, planned_at, status) VALUES(1, 0, 'planned')")
    db.commit()
    scrobble(db, 60)
    sync(db, [activity()])
    assert history.pending_session(db)["id"] == 1
    assert db.execute("SELECT COUNT(*) FROM plays").fetchone()[0] == 0
