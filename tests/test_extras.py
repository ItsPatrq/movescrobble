import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from swimscrobble import audio, garmin, history, pocketcasts


def tone(path, seconds=6, volume_db=-30, gaps=False):
    """A sine mp3; with `gaps`, 1 s tone / 2 s silence repeated (a podcast with long pauses)."""
    src = f"sine=frequency=440:duration={seconds}"
    flt = f"volume={volume_db}dB" + (",volume='if(lt(mod(t,3),1),1,0)':eval=frame" if gaps else "")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-af", flt, "-c:a", "libmp3lame", str(path)],
                   check=True)
    return path


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


# --- Garmin ---------------------------------------------------------------------------------------

def act(kind, start, seconds, **kw):
    return {"activityType": {"typeKey": kind}, "startTimeLocal": start, "elapsedDuration": seconds, **kw}


def test_garmin_picks_the_first_swim_inside_the_window():
    acts = [
        act("running", "2026-10-25 07:00:00", 1800),
        act("lap_swimming", "2026-10-25 06:00:00", 3000),  # before the playlist was staged
        act("lap_swimming", "2026-10-25 09:10:00", 2700),
        act("lap_swimming", "2026-10-25 08:05:00", 3300.5, distance=2000.0),
    ]
    asked = []
    swim = garmin.swim_between(datetime(2026, 10, 25, 7, 30), datetime(2026, 10, 25, 10),
                               lambda s, e: (asked.append((s, e)) or acts))
    assert swim.start == datetime(2026, 10, 25, 8, 5) and round(swim.minutes) == 55 and swim.distance_m == 2000
    assert asked == [("2026-10-25", "2026-10-25")]
    assert "lap swimming" in swim.describe() and "2000 m" in swim.describe()


def test_garmin_no_swim_and_not_enabled():
    assert garmin.swim_between(datetime(2026, 10, 25), datetime(2026, 10, 26), lambda s, e: []) is None
    assert garmin.finder(type("C", (), {"garmin": False})()) is None


# --- loudness -------------------------------------------------------------------------------------

@pytest.mark.ffmpeg
def test_quiet_track_is_levelled_once_and_cached(tmp_path, monkeypatch):
    src = tone(tmp_path / "quiet.mp3", volume_db=-30)
    cache = tmp_path / "cache"
    out = audio.normalised(src, -14, cache)
    assert out != src and out.parent == cache
    assert abs(float(audio.measure(out)["input_i"]) + 14) < 1.5
    calls = []
    monkeypatch.setattr(audio, "ffmpeg", lambda *a: calls.append(a))
    assert audio.normalised(src, -14, cache) == out and not calls  # second time: straight from the cache


@pytest.mark.ffmpeg
def test_track_already_at_target_is_copied_untouched(tmp_path):
    src = tmp_path / "loud.mp3"
    probe = tone(tmp_path / "probe.mp3", volume_db=0)
    gain = -14 - float(audio.measure(probe)["input_i"])
    tone(src, volume_db=gain)  # lands on -14 LUFS
    assert audio.normalised(src, -14, tmp_path / "cache") == src
    assert audio.normalised(src, -14, tmp_path / "cache") == src  # remembered without measuring again


@pytest.mark.ffmpeg
def test_unreadable_file_is_staged_as_it_is(tmp_path):
    src = tmp_path / "broken.mp3"
    src.write_bytes(b"not audio")
    assert audio.leveller(-14, tmp_path / "cache")(src) == src  # unreadable: staged as it is
    assert audio.leveller(None)(src) == src


def test_old_cache_entries_are_pruned(tmp_path):
    import os

    (tmp_path / "old.mp3").write_bytes(b"x")
    (tmp_path / "new.mp3").write_bytes(b"x")
    os.utime(tmp_path / "old.mp3", (1, 1))
    assert audio.prune(tmp_path) == 1 and sorted(p.name for p in tmp_path.iterdir()) == [".pruned", "new.mp3"]
    os.utime(tmp_path / "new.mp3", (1, 1))
    assert audio.prune(tmp_path) == 0  # at most once a day


# --- podcasts -------------------------------------------------------------------------------------

def test_podcast_filter():
    assert audio.podcast_filter(1.0, False) is None
    assert audio.podcast_filter(1.3, False) == "atempo=1.30"
    trimmed = audio.podcast_filter(1.3, True)
    assert trimmed.startswith("silenceremove=") and trimmed.endswith("atempo=1.30")


@pytest.mark.ffmpeg
def test_podcast_is_sped_up_and_pauses_are_cut(tmp_path):
    src = tone(tmp_path / "ep.mp3", seconds=12, volume_db=-10, gaps=True)  # 4 s speech, 8 s silence
    audio.process_podcast(src, tmp_path / "fast.mp3", 1.5, True)
    assert duration(tmp_path / "fast.mp3") < 0.6 * duration(src)


def test_processing_download_falls_back_to_the_original(tmp_path, monkeypatch):
    monkeypatch.setattr(pocketcasts, "http_download", lambda url, dest: dest.write_bytes(b"raw audio"))
    def boom(*a): raise RuntimeError("ffmpeg failed")
    monkeypatch.setattr(audio, "process_podcast", boom)
    fetcher = pocketcasts.Fetcher.for_settings(1.3, True)
    assert (fetcher.ext, fetcher.variant) == (".mp3", "1.30x+trim")
    fetcher.fetch("https://x/a.mp3", tmp_path / "01 - a.mp3")
    assert [p.name for p in tmp_path.iterdir()] == ["01 - a.mp3"] and (tmp_path / "01 - a.mp3").read_bytes() == b"raw audio"


def test_processed_episodes_are_named_mp3_and_remember_their_podcast(tmp_path):
    db = history.connect(tmp_path / "h.db")
    eps = [{"uuid": "e1", "podcastUuid": "p1", "title": "One", "url": "https://x/1.m4a"}]
    pocketcasts.sync_queue(eps, tmp_path / "dev", db, fetcher=pocketcasts.Fetcher(lambda u, d: d.write_bytes(b"x"), ".mp3"))
    assert pocketcasts.on_device(db) == [{"uuid": "e1", "podcast": "p1", "title": "One", "filename": "01 - One.mp3"}]


def test_changing_the_processing_refetches_instead_of_renaming(tmp_path):
    db = history.connect(tmp_path / "h.db")
    eps = [{"uuid": "e1", "podcastUuid": "p1", "title": "One", "url": "https://x/1.m4a"}]
    got = []
    dl = lambda u, d: (got.append(d.name), d.write_bytes(b"x"))  # noqa: E731
    pocketcasts.sync_queue(eps, tmp_path, db, fetcher=dl)
    pocketcasts.sync_queue(eps, tmp_path, db, fetcher=pocketcasts.Fetcher(dl, ".mp3", "1.30x"))
    pocketcasts.sync_queue(eps, tmp_path, db, fetcher=pocketcasts.Fetcher(dl, ".mp3", "1.30x"))  # unchanged: nothing fetched
    pocketcasts.sync_queue(eps, tmp_path, db, fetcher=pocketcasts.Fetcher(dl, ".mp3", "1.50x"))  # same format, new speed
    assert got == ["01 - One.m4a", "01 - One.mp3", "01 - One.mp3"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["01 - One.mp3", "h.db"]


def test_mark_played_tells_pocket_casts():
    sent = []
    n = pocketcasts.mark_played([{"uuid": "e1", "podcast": "p1"}], "tok",
                                lambda path, body, tok: sent.append((path, body)) or {})
    assert n == 1 and sent == [("/sync/update_episode", {"uuid": "e1", "podcast": "p1", "status": 3})]


def test_old_database_gets_the_podcast_column(tmp_path):
    old = sqlite3.connect(tmp_path / "h.db")
    old.execute("CREATE TABLE podcasts(uuid TEXT PRIMARY KEY, title TEXT, filename TEXT, downloaded_at REAL)")
    old.execute("INSERT INTO podcasts VALUES('e1','One','01 - One.mp3',0)")
    old.commit()
    old.close()
    db = history.connect(tmp_path / "h.db")
    assert pocketcasts.on_device(db)[0]["podcast"] is None


# --- 5: space on the headphones -------------------------------------------------------------------

def _plan(tmp_path, n=10, mb=10):
    from swimscrobble.library import Track
    from swimscrobble.planner import PlannedTrack

    out = []
    for i in range(n):
        f = tmp_path / f"t{i}.mp3"
        f.write_bytes(b"x" * int(mb * 1e6))
        out.append(PlannedTrack(i + 1, Track(f, "A", f"T{i}", "", 300)))
    return out


def test_buffer_tracks_are_dropped_until_the_playlist_fits(tmp_path, monkeypatch):
    from swimscrobble import flow

    plan = _plan(tmp_path)  # 10 x 5 min, 10 MB each
    monkeypatch.setattr(flow.shutil, "disk_usage", lambda p: SimpleNamespace(free=280e6))
    cfg = SimpleNamespace(stage_path=tmp_path, minutes=30, space_reserve_mb=200)
    kept = flow.fit_to_space(plan, cfg)  # 80 MB usable: 7 tracks (11 MB each)
    assert len(kept) == 7
    with pytest.raises(RuntimeError, match="not enough space"):
        # the swim itself does not fit
        flow.fit_to_space(plan, SimpleNamespace(stage_path=tmp_path, minutes=45, space_reserve_mb=200))


def test_podcasts_leave_room_for_the_playlist(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    monkeypatch.setattr(pocketcasts.shutil, "disk_usage", lambda p: SimpleNamespace(free=500e6))
    eps = [{"uuid": f"e{i}", "title": f"Ep {i}", "url": f"https://x/{i}.mp3", "size": str(150_000_000)} for i in range(3)]
    rep = pocketcasts.sync_queue(eps, tmp_path / "dev", db, fetcher=lambda u, d: d.write_bytes(b"x"), reserve_mb=400)
    assert rep.no_space == ["01 - Ep 0.mp3", "02 - Ep 1.mp3", "03 - Ep 2.mp3"] and rep.on_device == 0
    rep = pocketcasts.sync_queue(eps, tmp_path / "dev", db, fetcher=lambda u, d: d.write_bytes(b"x"), reserve_mb=300)
    assert rep.downloaded == ["01 - Ep 0.mp3"] and len(rep.no_space) == 2  # 200 MB to spare: room for one


# --- 6: skips -------------------------------------------------------------------------------------

def test_skips_are_counted_and_a_thrice_skipped_track_is_retired(tmp_path):
    from swimscrobble.energy import Features
    from swimscrobble.library import Track
    from swimscrobble.mix import build_mix

    db = history.connect(tmp_path / "h.db")
    for sid in (1, 2, 3):
        db.execute("INSERT INTO sessions(id, status) VALUES(?, 'planned')", (sid,))
        history.mark_scrobbled(db, sid, [], skipped=[("Skip", "Me")] + ([("Skip", "Other")] if sid == 1 else []))
    tracks, artists = history.skip_counts(db)
    assert tracks[("skip", "me")] == 3 and artists["skip"] == 4
    lib = [Track(tmp_path / f"{i}.mp3", "Skip" if i == 0 else f"A{i}", "Me" if i == 0 else f"T{i}", "", 240)
           for i in range(12)]
    plan = build_mix(lib, [], lambda t: Features(125, -7, 150, 0.6, 0.8), minutes=60, swim_minutes=30, seed=1,
                     skips=tracks, artist_skips=artists)
    assert plan and all(p.track.title != "Me" for p in plan)


# --- 7: liked discoveries -------------------------------------------------------------------------

def test_love_and_playlist_id(monkeypatch):
    from swimscrobble import lastfm, ytm

    sent = []
    monkeypatch.setattr(lastfm, "credentials", lambda: ("k", "s"))
    lastfm.love("A", "T", "sk", lambda body: sent.append(body) or {})
    assert sent[0]["method"] == "track.love" and sent[0]["artist"] == "A" and sent[0]["sk"] == "sk"
    assert ytm.playlist_id("https://music.youtube.com/playlist?list=PL123&si=x") == "PL123"


# --- 8: workouts ----------------------------------------------------------------------------------

def test_workout_shapes():
    from swimscrobble.workouts import WORKOUTS

    lo, mid, hi = 0.5, 0.7, 0.9
    iv = [WORKOUTS["intervals"].target(p / 100, lo, mid, hi) for p in range(15, 90)]
    assert {round(x, 2) for x in iv} == {lo, hi} and sum(a != b for a, b in zip(iv, iv[1:], strict=False)) >= 4
    assert WORKOUTS["pyramid"].target(0.5, lo, mid, hi) == hi and WORKOUTS["pyramid"].target(0.0, lo, mid, hi) == lo
    assert max(WORKOUTS["easy"].target(p / 10, lo, mid, hi) for p in range(10)) < mid


def test_every_workout_orders_a_mix(tmp_path):
    import numpy as np

    from swimscrobble.sequence import order
    from swimscrobble.workouts import WORKOUTS

    rng = np.random.default_rng(0)
    e = rng.uniform(0.5, 1.0, 12)
    for w in WORKOUTS:
        perm = order(e, np.full(12, 128.0), np.zeros(12), np.arange(12), np.zeros(12, int), np.full(12, 240.0),
                     2400, np.random.default_rng(1), iters=500, workout=w)
        assert sorted(perm) == list(range(12))


# --- 9: recap -------------------------------------------------------------------------------------

def test_recap_counts_swims_minutes_distance_and_discoveries(tmp_path):
    from swimscrobble import stats

    db = history.connect(tmp_path / "h.db")
    m = {"tracks": [{"position": 1, "artist": "A", "title": "Old", "source": "obsession"},
                    {"position": 2, "artist": "B", "title": "New", "source": "rec"},
                    {"position": 3, "artist": "C", "title": "Nope", "source": "fresh"}]}
    sid = history.add_session(db, m, 60, None, "x")
    history.mark_scrobbled(db, sid, [("A", "Old", 1.0), ("B", "New", 2.0)], skipped=[("C", "Nope")],
                           minutes=52, distance_m=2100)
    r = stats.recap(db, 7)
    assert (r.swims, r.minutes, r.distance_m, r.tracks, r.skipped) == (1, 52, 2100, 2, 1)
    assert r.discoveries == [("B", "New")]
    assert stats.recap(db, 7, now=__import__("time").time() + 30 * 86400).swims == 0


def test_old_sessions_table_gets_the_swim_columns(tmp_path):
    old = sqlite3.connect(tmp_path / "h.db")
    old.execute("CREATE TABLE sessions(id INTEGER PRIMARY KEY, planned_at REAL, seed INTEGER, minutes REAL, "
                "status TEXT NOT NULL DEFAULT 'planned', staged_dir TEXT, manifest TEXT, scrobbled_at REAL)")
    old.commit()
    old.close()
    db = history.connect(tmp_path / "h.db")
    assert {"swum_minutes", "distance_m"} <= {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}


# --- 10: one notification per connection ----------------------------------------------------------

def test_podcast_and_staged_summaries():
    from swimscrobble import flow

    assert str(pocketcasts.PodcastReport(downloaded=["a"], failed=["b: 403"], on_device=6)) == "6 podcasts (1 new), 1 FAILED"
    m = {"tracks": [{"duration": 600, "source": "rec"}, {"duration": 600, "source": "obsession"}], "workout": "intervals"}
    assert flow.staged_summary(m) == "staged 2 tracks, 20 min, 1 new (intervals)"


def test_busy_spinner_nests_and_ignores_updates_when_idle():
    import io

    from rich.console import Console

    from swimscrobble.ui import Busy

    out = io.StringIO()
    busy = Busy(Console(file=out, force_terminal=True, width=80))
    busy.update("nothing running")  # no spinner: must not raise or print
    with busy("outer"):
        with busy("inner"):
            busy.update("step 1/2")
        assert busy._status is not None
    assert busy._status is None and "nothing running" not in out.getvalue()
    plain = io.StringIO()
    with Busy(Console(file=plain, force_terminal=False))("syncing"):
        pass
    assert plain.getvalue() == "syncing ...\n"


def test_podcast_sync_reports_fetch_progress(tmp_path):
    db = history.connect(tmp_path / "h.db")
    eps = [{"uuid": f"u{i}", "title": f"Ep {i}", "url": f"https://x/{i}.mp3"} for i in range(2)]
    steps = []
    pocketcasts.sync_queue(eps, tmp_path / "dev", db, fetcher=lambda url, dest: dest.write_bytes(b"x"),
                           progress=steps.append)
    assert steps[0] == "podcasts: fetching 0/2" and "podcasts: fetched 2/2" in steps
    assert steps[-1] == "podcasts: copying to the headphones 2/2"


def test_blocklist_matches_artists_collaborations_and_single_tracks(tmp_path):
    from swimscrobble import blocklist

    db = history.connect(tmp_path / "h.db")
    blocklist.block(db, "The Weeknd")
    blocklist.block(db, "Daft Punk", "One More Time")
    b = blocklist.Blocklist.load(db)
    assert b.blocks("The Weeknd", "Blinding Lights")
    assert b.blocks("the weeknd", "Save Your Tears (Remix)")
    assert b.blocks("Daft Punk, The Weeknd", "Starboy")
    assert b.blocks("Ariana Grande", "Save Your Tears (with The Weeknd)")
    assert b.blocks("Daft Punk", "One More Time (Radio Edit)")
    assert not b.blocks("Daft Punk", "Get Lucky")
    assert not b.blocks("Weekend Players", "21st Century")  # whole words only
    assert blocklist.unblock(db, "the weeknd") and not blocklist.Blocklist.load(db).blocks("The Weeknd", "Starboy")


# --- Garmin: power songs, soundtrack, recap ---------------------------------------------------------

from datetime import timedelta  # noqa: E402

from swimscrobble import power  # noqa: E402


def lengths_doc(start_gmt, speeds, seconds=40):
    """splits JSON with one 25 m length per entry of `speeds` (1.0 = normal), back to back from `start_gmt`."""
    t, out = datetime.fromisoformat(start_gmt), []
    for i, v in enumerate(speeds):
        out.append({"startTimeGMT": t.isoformat(), "distance": 25.0, "duration": seconds / v, "averageHR": 120 + i,
                    "averageSWOLF": 50})
        t += timedelta(seconds=seconds / v)
    out.append({"startTimeGMT": t.isoformat(), "distance": 0.0, "duration": 30})  # a rest: no distance
    return {"lapDTOs": [{"lengthDTOs": out}]}


def test_garmin_parse_keeps_ids_offset_pace_and_zones():
    s = garmin.parse(act("lap_swimming", "2026-10-25 21:28:49", 1888, distance=1200.0, activityId=7,
                         startTimeGMT="2026-10-25 20:28:49", movingDuration=1500, averageHR=130,
                         hrTimeInZone_1=60, hrTimeInZone_3=600))
    assert s.activity_id == 7 and s.utc_offset == timedelta(hours=1) and round(s.pace) == 125
    assert s.hr_zones == (60, 0.0, 600, 0.0, 0.0) and "2:05/100m" in s.describe()
    ls = garmin.parse_lengths(lengths_doc("2026-10-25T20:28:49", [1, 1]), s.utc_offset)
    assert len(ls) == 2 and ls[0].start == datetime(2026, 10, 25, 21, 28, 49)


def test_lengths_are_split_between_tracks_and_compared_with_the_whole_swim():
    t0 = datetime(2026, 10, 25, 9)
    heard = [power.Heard("A", "fast", t0, 200), power.Heard("B", "slow", t0 + timedelta(seconds=200), 200),
             power.Heard("C", "wall", t0 + timedelta(seconds=400), 200)]
    ls = garmin.parse_lengths(lengths_doc("2026-10-25T09:00:00", [1.25] * 6 + [0.8] * 4), timedelta(0))
    e = {x.title: x for x in power.analyse(heard, ls, uncertainty_minutes=0)}
    assert set(e) == {"fast", "slow"}  # nothing swum while C played
    assert e["fast"].rel_speed > 0.1 > -0.1 > e["slow"].rel_speed and e["fast"].lengths == 6
    assert e["fast"].hr and e["fast"].pace < e["slow"].pace


def _scrobbled_session(db, start):
    manifest = {"tracks": [{"position": 1, "artist": "A", "title": "fast", "duration": 400},
                           {"position": 2, "artist": "B", "title": "slow", "duration": 400}]}
    sid = history.add_session(db, manifest, 60, None, "x", at=(start - timedelta(hours=3)).timestamp())
    history.mark_scrobbled(db, sid, [("A", "fast", start.timestamp()),
                                     ("B", "slow", (start + timedelta(seconds=400)).timestamp())])
    return sid


def test_garmin_sync_times_the_session_writes_the_soundtrack_once_and_feeds_the_mix(tmp_path):
    db = history.connect(tmp_path / "h.db")
    start = datetime.now().replace(microsecond=0) - timedelta(days=1)
    sid = _scrobbled_session(db, start)
    gmt = start - timedelta(hours=1)
    acts = [act("lap_swimming", start.isoformat(" "), 600, distance=250.0, activityId=11, movingDuration=450,
                startTimeGMT=gmt.isoformat(" "), averageHR=125),
            act("lap_swimming", (start - timedelta(days=3)).isoformat(" "), 1800, distance=1000.0, activityId=10)]
    written = []
    cfg = SimpleNamespace(garmin_soundtrack=True)
    run = lambda: power.sync(db, cfg, fetch_swims=lambda a, b: acts,  # noqa: E731
                             fetch_lengths=lambda s: garmin.parse_lengths(
                                 lengths_doc(gmt.isoformat(), [1.25] * 10 + [0.8] * 8), s.utc_offset),
                             write_soundtrack=lambda aid, text: written.append((aid, text)))
    msg = run()
    assert "2 swims" in msg and "timed 2 tracks in 1 new swim" in msg and "fastest to A - fast" in msg
    assert written[0][0] == 11 and "1. A - fast (" in written[0][1] and "2. B - slow" in written[0][1]
    assert db.execute("SELECT session_id FROM swims WHERE activity_id=11").fetchone()[0] == sid
    run()
    assert len(written) == 1  # already analysed: not timed or written again
    s = power.scores(db)
    assert s[("a", "fast")] > 0 > s[("b", "slow")]
    fastest, slowest = power.leaderboard(db)
    assert fastest[0][:2] == ("A", "fast") and slowest[0][:2] == ("B", "slow")


def test_soundtrack_keeps_your_own_description_and_replaces_an_old_one():
    first = garmin.with_soundtrack("", "♫ Swam to (swimscrobble):\n1. X")
    assert first == "♫ Swam to (swimscrobble):\n1. X"
    mine = garmin.with_soundtrack("Felt great\n\n" + first, "♫ Swam to (swimscrobble):\n1. Y")
    assert mine == "Felt great\n\n♫ Swam to (swimscrobble):\n1. Y"


def test_mix_prefers_power_songs(tmp_path):
    from swimscrobble.energy import Features
    from swimscrobble.library import Track
    from swimscrobble.mix import build_mix

    lib = [Track(tmp_path / f"{i}.mp3", f"Artist{i}", f"T{i}", "", 240) for i in range(40)]
    feat = lambda t: Features(120, -7, 150, 0.6, 0.8)  # noqa: E731
    boosted = {("artist0", "t0"): 0.1, ("artist1", "t1"): 0.1, ("artist2", "t2"): 0.1}
    hits = {k: 0 for k in (False, True)}
    for seed in range(30):
        for on in (False, True):
            plan = build_mix(lib, [], feat, minutes=40, swim_minutes=40, seed=seed, power=boosted if on else None)
            hits[on] += sum(p.track.title in ("T0", "T1", "T2") for p in plan)
    assert hits[True] > hits[False]


def test_recap_shows_pace_trend_weekly_distance_heart_rate_and_power_songs(tmp_path):
    db = history.connect(tmp_path / "h.db")
    now = datetime(2026, 10, 26, 12)
    for i, (days_ago, pace) in enumerate([(20, 140), (9, 132), (2, 125)]):
        s = garmin.Swim(now - timedelta(days=days_ago), 40, "lap_swimming", 1000.0, i + 1, timedelta(0),
                        pace * 10 / 60, 130, 150, 50, (0, 600, 1200, 0, 0))
        power.store_swim(db, s)
    db.executemany("INSERT INTO swim_tracks VALUES(1,?,?,?,0,0,0,0,?)", [("A", "fast", 20, 0.1), ("B", "slow", 20, -0.08)])
    from swimscrobble import stats

    text = "\n".join(stats.recap(db, 7, now.timestamp()).lines())
    assert "2:20 → 2:12 → 2:05 (best 2:05)" in text
    assert "Distance per week, last 4: 0.0 · 1.0 · 1.0 · 1.0 km" in text
    assert "avg 130 bpm" in text and "0% 33% 67% 0% 0%" in text
    assert "Power songs (you swim faster): A - fast +8%" in text and "Slow you down: B - slow -6%" in text


def test_missed_turns_count_double_and_stops_are_dropped():
    t = datetime(2026, 10, 25, 9)
    L = lambda s, strokes, stroke="FREESTYLE": garmin.Length(t, s, 25.0, strokes=strokes, stroke=stroke)  # noqa: E731
    out = power.repaired([L(36, 12), L(35, 12), L(71, 27), L(70, 12), L(37, 13)])
    assert [(n.seconds, n.distance_m) for n in out] == [(36, 25), (35, 25), (71, 50), (37, 25)]


def test_breaststroke_is_compared_with_breaststroke():
    t0 = datetime(2026, 10, 25, 9)
    heard = [power.Heard("A", "free", t0, 300), power.Heard("B", "breast", t0 + timedelta(seconds=300), 300)]
    ls, t = [], t0
    for stroke, secs in [("FREESTYLE", 35)] * 8 + [("BREASTSTROKE", 45)] * 6:
        ls.append(garmin.Length(t, secs, 25.0, strokes=12, stroke=stroke))
        t += timedelta(seconds=secs)
    e = {x.title: x for x in power.analyse(heard, ls, uncertainty_minutes=0)}
    assert abs(e["free"].rel_speed) < 0.01 and abs(e["breast"].rel_speed) < 0.01  # each normal for its stroke


def test_timing_uncertainty_spreads_lengths_near_a_track_change():
    t0 = datetime(2026, 10, 25, 9)
    heard = [power.Heard("A", "one", t0, 600), power.Heard("B", "two", t0 + timedelta(seconds=600), 600)]
    ls = [garmin.Length(t0 + timedelta(seconds=40 * i), 40, 25.0) for i in range(30)]
    sharp = power.analyse(heard, ls, uncertainty_minutes=0)
    spread = power.analyse(heard, ls)
    assert [e.lengths for e in sharp] == [15, 15]
    assert sum(e.lengths for e in spread) < 30  # some lengths may fall outside the playlist (before it started)
    assert all(e.lengths != 15 for e in spread)


def _episodes(n):
    return [{"uuid": f"u{i}", "title": f"Ep {i}", "url": f"https://x/{i}.mp3"} for i in range(n)]


def test_podcast_scratch_files_are_deleted_as_they_are_copied(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    scratch, real_copy, copied = tmp_path / "scratch", shutil.copyfile, []
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def copy(src, dst):
        assert not {p.name for p in Path(src).parent.iterdir()} & set(copied)  # earlier episodes are gone already
        copied.append(Path(src).name)
        return real_copy(src, dst)
    monkeypatch.setattr(pocketcasts.shutil, "copyfile", copy)
    rep = pocketcasts.sync_queue(_episodes(3), tmp_path / "dev", db, fetcher=lambda u, d: d.write_bytes(b"x"))
    assert len(rep.downloaded) == len(copied) == 3 and rep.on_device == 3
    assert list(scratch.iterdir()) == []


def test_podcast_scratch_is_removed_when_the_device_goes_away(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def gone(_src, _dst):
        raise OSError("device unplugged")
    monkeypatch.setattr(pocketcasts.shutil, "copyfile", gone)
    with pytest.raises(OSError):
        pocketcasts.sync_queue(_episodes(2), tmp_path / "dev", db, fetcher=lambda u, d: d.write_bytes(b"x"))
    assert list(scratch.iterdir()) == []


def test_podcast_sync_sweeps_scratch_left_by_a_killed_run_but_not_a_running_one(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    stale, running, other = (scratch / f"{n}x" for n in (pocketcasts.SCRATCH_PREFIX, pocketcasts.SCRATCH_PREFIX + "2", "other"))
    for d in (stale, running, other):
        d.mkdir(parents=True)
        (d / "ep.mp3").write_bytes(b"x")
    long_ago = time.time() - pocketcasts.STALE_SCRATCH_S - 60
    os.utime(stale, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    pocketcasts.sync_queue([], tmp_path / "dev", db)
    assert not stale.exists() and running.exists() and other.exists()


def test_http_download_leaves_no_partial_file_on_failure(tmp_path, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("connection reset")

    class Response(io.BytesIO):
        read = broken
    monkeypatch.setattr(pocketcasts, "open_url", lambda *_a, **_k: Response())
    with pytest.raises(OSError):
        pocketcasts.http_download("https://x/1.mp3", tmp_path / "1.mp3")
    assert list(tmp_path.iterdir()) == []


def _podcasts_cli(tmp_path, monkeypatch):
    from swimscrobble import cli, config
    seen, db = [], history.connect(tmp_path / "h.db")
    monkeypatch.setattr(config, "load", lambda: config.Config(volumes_dir=str(tmp_path)))
    monkeypatch.setattr(history, "connect", lambda: db)
    monkeypatch.setattr(pocketcasts, "sync_latest", lambda dest, *_a, **_k: seen.append(dest) or "ok")
    return cli, seen


def test_podcasts_command_targets_the_headphones_not_a_local_folder(tmp_path, monkeypatch):
    from swimscrobble.config import Config
    cli, seen = _podcasts_cli(tmp_path, monkeypatch)
    cfg = Config(volumes_dir=str(tmp_path))
    cfg.device_path.mkdir(parents=True)
    cli.cmd_podcasts(SimpleNamespace(dest=None, done=None))
    assert seen == [cfg.device_path / cfg.device_podcast_subdir]


def test_podcasts_command_refuses_without_the_headphones(tmp_path, monkeypatch):
    cli, seen = _podcasts_cli(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="not connected"):
        cli.cmd_podcasts(SimpleNamespace(dest=None, done=None))
    assert seen == []


def test_podcasts_command_honours_an_explicit_dest(tmp_path, monkeypatch):
    cli, seen = _podcasts_cli(tmp_path, monkeypatch)
    cli.cmd_podcasts(SimpleNamespace(dest=str(tmp_path / "keep"), done=None))
    assert seen == [tmp_path / "keep"]


def test_interrupted_copy_leaves_no_episode_that_looks_complete_and_is_refetched(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    dev, real_copy = tmp_path / "dev", shutil.copyfile
    fetch = lambda u, d: d.write_bytes(b"whole episode")  # noqa: E731

    def cut_short(_src, dst):
        Path(dst).write_bytes(b"who")
        raise OSError("device unplugged")
    monkeypatch.setattr(pocketcasts.shutil, "copyfile", cut_short)
    with pytest.raises(OSError):
        pocketcasts.sync_queue(_episodes(1), dev, db, fetcher=fetch)
    assert list(dev.iterdir()) == []

    monkeypatch.setattr(pocketcasts.shutil, "copyfile", real_copy)
    rep = pocketcasts.sync_queue(_episodes(1), dev, db, fetcher=fetch)
    assert rep.downloaded == ["01 - Ep 0.mp3"] and (dev / "01 - Ep 0.mp3").read_bytes() == b"whole episode"


def test_podcast_sync_clears_partial_copies_left_by_a_killed_run(tmp_path):
    db = history.connect(tmp_path / "h.db")
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "01 - Ep 0.mp3.part").write_bytes(b"who")
    pocketcasts.sync_queue(_episodes(1), dev, db, fetcher=lambda u, d: d.write_bytes(b"x"))
    assert [p.name for p in dev.iterdir()] == ["01 - Ep 0.mp3"]
