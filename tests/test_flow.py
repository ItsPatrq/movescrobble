import plistlib
from datetime import datetime
from pathlib import Path

import pytest

from swimscrobble import flow, history, launchd
from swimscrobble.config import Config
from swimscrobble.energy import Features
from swimscrobble.library import Track
from swimscrobble.pocketcasts import PodcastReport


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWIMSCROBBLE_HOME", str(tmp_path / "home"))
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


def test_reconnect_awaits_confirmation_without_auto_submit(env):
    cfg, db, lib, vol = env
    run(cfg, db, lib)
    (vol / "SWIM PRO").rename(vol / "gone"); assert run(cfg, db, lib)[0] == ["device not connected"]
    (vol / "gone").rename(vol / "SWIM PRO")
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 11, 0))
    assert log == ["awaiting confirmation"]
    assert history.pending_session(db) is not None  # not overwritten by a new plan
    csv = next((Path(flow.config_dir()) / "pending").glob("*.csv")).read_text()
    assert csv.count("\n") == 13  # header + only the 12 tracks that fit the 60 min swim, not all 24 staged


def test_auto_submit_scrobbles_then_stages_next(env):
    cfg, db, lib, vol = env
    cfg.auto_submit = True
    run(cfg, db, lib)
    flow._set_handled(False)
    sent = []
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 10, 0),
                    lastfm_submit=lambda s: (sent.append(s) or len(s)))
    assert len(sent) == 1 and len(sent[0]) == 12
    # session ended at plug-in time: last scrobble starts before 10:00, first is 60 min earlier
    assert sent[0][0].timestamp == datetime(2026, 10, 25, 9, 0) and sent[0][-1].timestamp < datetime(2026, 10, 25, 10)
    assert log[0] == "scrobbled 12 tracks" and "staged session" in log[-1]
    second = {p.name for p in (vol / "SWIM PRO" / "Music").iterdir()}
    assert len(second) == 24  # old staged files cleared, new full buffer in (recent tracks top it up)
    assert history.play_counts(db)


def test_failed_submit_keeps_session_pending(env):
    cfg, db, lib, _ = env
    cfg.auto_submit = True
    run(cfg, db, lib)
    flow._set_handled(False)
    def boom(s): raise RuntimeError("network")
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 10), lastfm_submit=boom)
    assert log[0].startswith("failed") and history.pending_session(db) is not None


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


def test_podcasts_sync_even_when_the_previous_session_blocks_staging(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib)
    flow._set_handled(False)
    rep = PodcastReport(downloaded=["a", "b"], on_device=2)
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 11, 0), podcast_sync=lambda: rep)
    assert log == ["podcasts: 2 podcasts (2 new)", "awaiting confirmation"]


def test_launchd_plist_and_install(tmp_path):
    data = plistlib.loads(launchd.build_plist("/py", "/Volumes", tmp_path))
    assert data["WatchPaths"] == ["/Volumes"] and data["ProgramArguments"][-1] == "on-connect"
    calls = []
    p = launchd.install(501, tmp_path / "a.plist", lambda cmd, **kw: calls.append(cmd), python="/py")
    assert p.exists() and calls[-1][:3] == ["launchctl", "bootstrap", "gui/501"]
    launchd.uninstall(501, p, lambda cmd, **kw: calls.append(cmd))
    assert not p.exists()


def test_pool_sync_runs_and_failure_does_not_block(env):
    cfg, db, lib, _ = env
    log, _ = run(cfg, db, lib, pool_sync=lambda: "+3 -0")
    assert "pool sync: +3 -0" in log and any("staged session" in x for x in log)
    flow._set_handled(False)
    def bad(): raise RuntimeError("lastfm down")
    cfg.auto_submit = True  # settle the first session so the second connection reaches the pool step
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 12), pool_sync=bad, lastfm_submit=len)
    assert any("pool sync failed" in x for x in log)


def test_plugging_in_again_straight_away_does_not_scrobble_a_swim_that_never_happened(env):
    cfg, db, lib, vol = env
    cfg.auto_submit = True
    run(cfg, db, lib, now=datetime(2026, 10, 25, 9, 0))
    flow._set_handled(False)  # unplug / replug
    sent = []
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 9, 5), lastfm_submit=lambda s: sent.append(s))
    assert log == ["too soon"] and not sent and history.pending_session(db) is not None


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


def test_garmin_swim_scrobbles_its_real_start_and_length_without_asking(env):
    from swimscrobble.garmin import Swim

    cfg, db, lib, _ = env  # auto_submit is off: the Garmin swim alone is enough
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    flow._set_handled(False)
    sent, windows = [], []
    def find(since, until):
        windows.append((since, until))
        return Swim(datetime(2026, 10, 25, 8, 15), 30.0, "lap_swimming", 1500)
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 10, 0), find_swim=find,
                    lastfm_submit=lambda s: (sent.append(s) or len(s)))
    assert windows == [(datetime(2026, 10, 25, 7, 0), datetime(2026, 10, 25, 10, 0))]
    # the music started warmup_minutes (8) before the watch and ran cooldown_minutes (11) after: 49 min from 8:07
    assert len(sent[0]) == 10 and sent[0][0].timestamp == datetime(2026, 10, 25, 8, 7)
    assert log[0].startswith("Garmin lap swimming") and "scrobbled 10 tracks" in log[0] and "staged session" in log[-1]
    assert db.execute("SELECT swum_minutes FROM sessions WHERE status='scrobbled'").fetchone()[0] == 30  # the swim


def test_no_garmin_swim_or_garmin_down_falls_back_to_asking(env):
    cfg, db, lib, _ = env
    run(cfg, db, lib, now=datetime(2026, 10, 25, 7, 0))
    flow._set_handled(False)
    log, _ = run(cfg, db, lib, now=datetime(2026, 10, 25, 10, 0), find_swim=lambda a, b: None)
    assert log == ["awaiting confirmation"]
    flow._set_handled(False)
    def down(a, b): raise RuntimeError("401")
    log, msgs = run(cfg, db, lib, now=datetime(2026, 10, 25, 10, 5), find_swim=down)
    assert log == ["awaiting confirmation"] and any("Garmin lookup failed" in m for m in msgs)


def test_one_notification_sums_up_the_connection(env):
    cfg, db, lib, _ = env
    log, msgs = run(cfg, db, lib, podcast_sync=lambda: PodcastReport(downloaded=["x"], on_device=3),
                    pool_sync=lambda: (_ for _ in ()).throw(RuntimeError("lastfm down")))
    assert len(msgs) == 1
    assert msgs[0].startswith("swimscrobble: 3 podcasts (1 new) · pool sync FAILED: lastfm down · staged 24 tracks")


def test_on_connect_reports_each_step_for_the_spinner(env):
    cfg, db, lib, vol = env
    steps = []
    run(cfg, db, lib, podcast_sync=lambda: "0 podcasts", pool_sync=lambda: "+0 -0", progress=steps.append)
    assert steps[:5] == ["syncing podcasts", "settling the last swim", "syncing Last.fm history and the pool",
                         "scanning the library", "removing the previous playlist"]
    n = sum(s.startswith("copying") for s in steps)
    assert n and steps[-1] == f"copying to the headphones {n}/{n}" and f"levelling tracks {n}/{n}" in steps
