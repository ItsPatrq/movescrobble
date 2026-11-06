from datetime import datetime
from pathlib import Path

import pytest

from swimscrobble import config, history, listenbrainz
from swimscrobble.library import Track
from swimscrobble.planner import PlannedTrack, bpm_arc, plan_session
from swimscrobble.scrobble import Scrobble, build_scrobbles, confirm, skip_positions, trim_to_played


def tr(i, dur=240, bpm=None, artist=None):
    return Track(Path(f"t{i}.mp3"), artist or f"A{i}", f"T{i}", "", dur, bpm)


def planned(n, dur=240):
    return [PlannedTrack(i + 1, tr(i, dur)) for i in range(n)]


def test_trim_cut_off_track_needs_half():
    p = planned(3)  # 240s each
    assert len(trim_to_played(p, 240 + 130)) == 2  # 130s of 2nd >= 120s (half)
    assert len(trim_to_played(p, 240 + 100)) == 1
    assert len(trim_to_played(p, 99999)) == 3


def test_trim_long_track_capped_at_four_minutes():
    p = [PlannedTrack(1, tr(1, dur=1200))]
    assert trim_to_played(p, 239) == []
    assert len(trim_to_played(p, 241)) == 1


def test_trim_ignores_tracks_under_30s():
    assert trim_to_played([PlannedTrack(1, tr(1, dur=20))], 100) == []


def test_skip_shifts_following_timestamps():
    p = skip_positions(planned(3), {2})
    s = build_scrobbles(p, datetime(2026, 10, 25, 7))
    assert [x.title for x in s] == ["T0", "T2"]
    assert (s[1].timestamp - s[0].timestamp).total_seconds() == 240


def test_confirm(capsys):
    s = [Scrobble(datetime(2026, 10, 25), "a", "t", "", 10)]
    assert confirm(s, lambda _: "y") and not confirm(s, lambda _: "")


def test_listenbrainz_payload_and_batches():
    s = [Scrobble(datetime(2026, 10, 24, 7), "a", f"t{i}", "al" if i == 0 else "", 100) for i in range(250)]
    calls = []
    n = listenbrainz.submit(s, "tok", lambda payload, tok: (calls.append((payload, tok)) or {"status": "ok"}))
    assert n == 250 and len(calls) == 3 and calls[0][1] == "tok"
    first = calls[0][0]["payload"][0]["track_metadata"]
    assert calls[0][0]["listen_type"] == "import" and first["release_name"] == "al"


def test_listenbrainz_error():
    s = [Scrobble(datetime(2026, 10, 24), "a", "t", "", 1)]
    with pytest.raises(RuntimeError):
        listenbrainz.submit(s, "tok", lambda *_: {"status": "error"})


def test_bpm_arc_peaks_in_middle():
    tracks = [tr(i, bpm=b) for i, b in enumerate([90, 100, 110, 120, 130, 140, 150])]
    out = [t.bpm for t in bpm_arc(tracks)]
    assert out.index(150) in (2, 3, 4) and out[0] < 110 and out[-1] < 120


def test_exclude_recent_when_enough_left():
    lib = [tr(i, 600) for i in range(30)]
    ex = {(f"a{i}", f"t{i}") for i in range(10)}
    plan = plan_session(lib, minutes=60, seed=1, exclude=ex)
    assert all((p.track.artist.casefold(), p.track.title.casefold()) not in ex for p in plan)


def test_config_defaults_and_unknown_key(tmp_path):
    assert config.load(tmp_path / "none.toml").minutes == 60
    f = tmp_path / "c.toml"
    f.write_text('minutes = 45\nmax_podcasts = 3\n')
    c = config.load(f)
    assert c.minutes == 45 and c.max_podcasts == 3
    f.write_text("bogus = 1\n")
    with pytest.raises(SystemExit):
        config.load(f)


def test_history_session_lifecycle(tmp_path):
    db = history.connect(tmp_path / "h.db")
    sid = history.add_session(db, {"tracks": []}, 60, 1, None)
    assert history.pending_session(db)["id"] == sid
    history.mark_scrobbled(db, sid, [("A", "T", 9e12)])
    assert history.pending_session(db) is None
    assert ("a", "t") in history.recent_plays(db, 10**6)
    with pytest.raises(ValueError):
        history.mark_scrobbled(db, sid, [])
    assert history.play_counts(db)[("a", "t")] == 1


def test_secret_reads_env_then_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SWIMSCROBBLE_HOME", str(tmp_path))
    monkeypatch.delenv("MY_SECRET", raising=False)
    assert config.secret("MY_SECRET") is None
    (tmp_path / "secrets.toml").write_text('MY_SECRET = "from-file"\n')
    assert config.secret("MY_SECRET") == "from-file"
    monkeypatch.setenv("MY_SECRET", "from-env")
    assert config.secret("MY_SECRET") == "from-env"
