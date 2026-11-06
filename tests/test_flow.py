import json
import plistlib
from datetime import datetime

import pytest

from movescrobble import flow, history, launchd
from movescrobble.config import Config
from movescrobble.energy import Features
from movescrobble.garmin import Swim
from movescrobble.library import Track
from movescrobble.pocketcasts import PodcastReport


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path / "home"))
    music = tmp_path / "music"
    music.mkdir()
    lib = []
    for i in range(30):
        f = music / f"{i:02d} - song{i}.mp3"
        f.write_bytes(b"x" * 10)
        lib.append(Track(f, f"Artist{i % 5}", f"Song{i}", "Alb", 300))
    vol = tmp_path / "Volumes"
    (vol / "SWIM PRO" / "Music").mkdir(parents=True)
    cfg = Config(music_dir=str(music), volumes_dir=str(vol), minutes=60, loudness_target=0)  # fake audio
    db = history.connect(tmp_path / "h.db")
    return cfg, db, lib, vol


def run(cfg, db, lib, now=datetime(2026, 10, 25, 9, 0), **kw):
    msgs = []
    log = flow.on_connect(cfg, db, now, msgs.append, library_loader=lambda _: lib,
                          feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8), **kw)
    return log, msgs


def test_no_device(env):
    cfg, db, lib, vol = env
    (vol / "SWIM PRO" / "Music").rmdir(); (vol / "SWIM PRO").rmdir()
    assert run(cfg, db, lib)[0] == ["device not connected"]


def test_first_connect_stages_session(env):
    cfg, db, lib, vol = env
    log, msgs = run(cfg, db, lib)
    staged = sorted(p.name for p in (vol / "SWIM PRO" / "Music").iterdir())
    assert len(staged) == 24 and staged[0].startswith("001 - ")  # 60 min swim x2 buffer
    assert history.pending_session(db) is not None and "staged" in msgs[-1]


def test_same_connection_is_not_reprocessed(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib)
    assert run(cfg, db, lib)[0] == ["already handled this connection"]


def test_clear_staged_only_touches_prefixed_files(tmp_path):
    (tmp_path / "001 - a.mp3").write_bytes(b"x")
    (tmp_path / "01 - keep.mp3").write_bytes(b"x")
    m = {"tracks": [{"staged": "001 - a.mp3"}, {"staged": "01 - keep.mp3"}]}
    assert flow.clear_staged(tmp_path, m) == 1 and (tmp_path / "01 - keep.mp3").exists()


def test_podcast_failure_does_not_block_staging(env):
    cfg, db, lib, _ = env
    def bad(): raise RuntimeError("401")
    log, _ = run(cfg, db, lib, podcast_sync=bad)
    assert any("podcasts failed" in l for l in log) and any("staged session" in l for l in log)


def test_launchd_plist_and_install(tmp_path):
    data = plistlib.loads(launchd.build_plist("/py", "/Volumes", tmp_path))
    assert data["WatchPaths"] == ["/Volumes"] and data["ProgramArguments"][-1] == "on-connect"
    calls = []
    p = launchd.install(501, tmp_path / "a.plist", lambda cmd, **kw: calls.append(cmd), python="/py")
    assert p.exists() and calls[-1][:3] == ["launchctl", "bootstrap", "gui/501"]
    launchd.uninstall(501, p, lambda cmd, **kw: calls.append(cmd))
    assert not p.exists()


def test_reroll_replaces_the_files_and_discards_the_old_session_without_scrobbling(env):
    cfg, db, lib, vol = env
    run(cfg, db, lib)
    music = vol / "SWIM PRO" / "Music"
    before = {p.name for p in music.iterdir()}
    first_id = history.pending_session(db)["id"]
    m = flow.reroll(cfg, db, seed=99, library_loader=lambda _: lib, feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8))
    after = {p.name for p in music.iterdir()}
    assert len(after) == len(m["tracks"]) and after != before  # old staged files removed, new ones in
    assert db.execute("SELECT status FROM sessions WHERE id=?", (first_id,)).fetchone()["status"] == "discarded"
    assert history.pending_session(db)["id"] == m["id"]
    assert db.execute("SELECT COUNT(*) c FROM plays").fetchone()["c"] == 0  # nothing scrobbled


def test_reroll_needs_the_device(env):
    cfg, db, lib, vol = env
    (vol / "SWIM PRO" / "Music").rmdir()
    (vol / "SWIM PRO").rmdir()
    import pytest
    with pytest.raises(RuntimeError):
        flow.reroll(cfg, db, library_loader=lambda _: lib)


def test_discard_pending(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib)
    sid = history.discard_pending(db)
    assert sid and history.pending_session(db) is None and history.discard_pending(db) is None


def test_one_notification_sums_up_the_connection(env):
    cfg, db, lib, _ = env
    log, msgs = run(cfg, db, lib, podcast_sync=lambda: PodcastReport(downloaded=["x"], on_device=3),
                    pool_sync=lambda: (_ for _ in ()).throw(RuntimeError("lastfm down")))
    assert len(msgs) == 1
    assert msgs[0].startswith("movescrobble: 3 podcasts (1 new) · pool sync FAILED: lastfm down · staged 24 tracks")


def swim_at(hour=8, minute=15):
    return Swim(datetime(2026, 10, 25, hour, minute), 30.0, "lap_swimming", 1500)


def settled_session(cfg, db, lib, **kw):
    """Stage at 7:00, then Garmin shows a swim."""
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    return flow.settle_swim(db, cfg, datetime(2026, 10, 25, 10), lambda a, b: swim_at(), **kw)


def test_plugging_in_never_scrobbles_and_keeps_an_unsettled_playlist(env):
    cfg, db, lib, vol = env
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    first = history.pending_session(db)["id"]
    before = sorted(p.name for p in (vol / "SWIM PRO" / "Music").iterdir())
    flow._set_handled(False)
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 20, 0))
    assert history.pending_session(db)["id"] == first
    assert sorted(p.name for p in (vol / "SWIM PRO" / "Music").iterdir()) == before
    assert db.execute("SELECT COUNT(*) c FROM plays").fetchone()["c"] == 0
    assert "keeping session" in log[0] and msgs == ["movescrobble: playlist unchanged"]


def test_plugging_in_after_a_settled_swim_stages_the_next_playlist(env):
    cfg, db, lib, _ = env
    assert "scrobbled 10 tracks" in settled_session(cfg, db, lib, lastfm_submit=len)
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 11, 0))
    assert any("staged session" in x for x in log) and history.pending_session(db) is not None


def test_a_swim_scrobbles_its_real_start_and_length_without_headphones(env):
    cfg, db, lib, vol = env
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    (vol / "SWIM PRO" / "Music").rename(vol / "gone")
    (vol / "SWIM PRO").rmdir()
    sent, windows = [], []

    def find(since, until):
        windows.append((since, until))
        return swim_at()

    out = flow.settle_swim(db, cfg, datetime(2026, 10, 25, 10), find, lastfm_submit=lambda x: (sent.append(x) or len(x)))
    assert windows == [(datetime(2026, 10, 25, 7, 0), datetime(2026, 10, 25, 10, 0))]
    # the music started warmup_minutes (8) before the watch and ran cooldown_minutes (11) after: 49 min from 8:07
    assert len(sent[0]) == 10 and sent[0][0].timestamp == datetime(2026, 10, 25, 8, 7)
    assert out.startswith("lap swimming") and "scrobbled 10 tracks" in out
    assert db.execute("SELECT swum_minutes FROM sessions WHERE status='scrobbled'").fetchone()[0] == 30


def test_without_a_swim_on_garmin_nothing_is_scrobbled(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    sent = []
    out = flow.settle_swim(db, cfg, datetime(2026, 10, 25, 10), lambda a, b: None, lastfm_submit=sent.append)
    assert out == "no swim on Garmin since the playlist was staged"
    assert not sent and history.pending_session(db) is not None


def test_garmin_down_scrobbles_nothing_and_says_so(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))

    def down(a, b):
        raise RuntimeError("401")

    warned, sent = [], []
    flow.settle_swim(db, cfg, datetime(2026, 10, 25, 10), down, warned.append, lastfm_submit=sent.append)
    assert not sent and history.pending_session(db) is not None and "Garmin lookup failed: 401" in warned[0]


def test_failed_submit_keeps_session_pending(env):
    cfg, db, lib, _ = env

    def boom(_s):
        raise RuntimeError("network")

    assert settled_session(cfg, db, lib, lastfm_submit=boom) == "scrobbling FAILED: network"
    assert history.pending_session(db) is not None


def test_nothing_staged_is_not_an_error(env):
    cfg, db, _, _ = env
    assert flow.settle_swim(db, cfg, datetime(2026, 10, 25, 10), lambda a, b: swim_at()) == (
        "no staged playlist waiting for a swim")


def test_podcasts_sync_even_when_the_playlist_stays(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib)
    flow._set_handled(False)
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 11, 0),
                    podcast_sync=lambda: PodcastReport(downloaded=["a", "b"], on_device=2))
    assert log[0] == "podcasts: 2 podcasts (2 new)" and "keeping session" in log[1]
    assert "2 podcasts (2 new)" in msgs[0]


def test_pool_sync_failure_does_not_block_staging(env):
    cfg, db, lib, _ = env

    def bad():
        raise RuntimeError("lastfm down")

    log, _ = run(cfg, db, lib, pool_sync=bad)
    assert any("pool sync failed" in x for x in log) and any("staged session" in x for x in log)


def test_on_connect_reports_each_step_for_the_spinner(env):
    cfg, db, lib, _ = env
    steps = []
    run(cfg, db, lib, podcast_sync=lambda: "0 podcasts", pool_sync=lambda: "+0 -0", progress=steps.append)
    assert steps[:4] == ["syncing podcasts", "syncing Last.fm history and the pool", "scanning the library",
                         "removing the previous playlist"]
    n = sum(x.startswith("copying") for x in steps)
    assert n and f"copying to the headphones {n}/{n}" in steps and f"levelling tracks {n}/{n}" in steps
    assert steps[-1] == "updating the YouTube Music playlist"


def test_youtube_gets_the_staged_playlist_and_cannot_stop_staging(env):
    cfg, db, lib, _ = env
    seen = []
    log, msgs = run(cfg, db, lib, mirror=lambda m: (seen.append(m) or "YouTube playlist FAILED: down"))
    assert seen[0]["id"] == history.pending_session(db)["id"]
    assert "YouTube playlist FAILED: down" in log and "YouTube playlist FAILED: down" in msgs[0]


def test_youtube_catches_up_when_the_playlist_stays(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib)
    flow._set_handled(False)
    seen = []
    run(cfg, db, lib, now=datetime(2026, 10, 25, 11), mirror=lambda m: (seen.append(m) or ""))
    assert seen[0]["tracks"] == json.loads(history.pending_session(db)["manifest"])["tracks"]
