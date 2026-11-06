import json
import time
from pathlib import Path

import pytest

from movescrobble import editing, flow, history, stats
from movescrobble.config import Config
from movescrobble.energy import Features
from movescrobble.library import Track


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path / "home"))
    music = tmp_path / "music"
    music.mkdir()
    lib = []
    for i in range(30):
        f = music / f"{i:02d} - song{i}.mp3"
        f.write_bytes(f"audio{i}".encode())
        lib.append(Track(f, f"Artist{i % 6}", f"Song{i}", "Alb", 300))
    vol = tmp_path / "Volumes"
    (vol / "SWIM PRO" / "Music").mkdir(parents=True)
    cfg = Config(music_dir=str(music), volumes_dir=str(vol), minutes=60, loudness_target=0)  # fake audio
    db = history.connect(tmp_path / "h.db")
    flow.on_connect(cfg, db, library_loader=lambda _: lib, notify=lambda m: None,
                    feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8))
    return cfg, db, lib, vol / "SWIM PRO" / "Music"


def staged(folder):
    return sorted(p.name for p in folder.iterdir())


def manifest(db):
    return json.loads(history.pending_session(db)["manifest"])


def test_add_track_inserts_renumbers_and_copies(env):
    cfg, db, lib, folder = env
    m0 = manifest(db)
    used = {t["file"] for t in m0["tracks"]}
    new = next(t for t in lib if str(t.path) not in used)
    m = editing.add_track(cfg, db, new, 3)
    names = staged(folder)
    assert len(names) == len(m0["tracks"]) + 1 and names[2] == f"003 - {new.path.name}"
    assert [t["position"] for t in m["tracks"]] == list(range(1, len(names) + 1))
    assert (folder / names[2]).read_bytes() == new.path.read_bytes()
    # the old third track is now fourth, with the same content
    old3 = m0["tracks"][2]
    assert (folder / f"004 - {Path(old3['file']).name}").read_bytes() == Path(old3["file"]).read_bytes()
    assert manifest(db)["tracks"][2]["source"] == "added"  # the database copy matches the device
    assert all(not n.startswith(".swimtmp") for n in names)


def test_add_track_rejects_duplicates_and_needs_a_session_and_device(env):
    cfg, db, lib, folder = env
    first = Path(manifest(db)["tracks"][0]["file"])
    dup = next(t for t in lib if t.path == first)
    with pytest.raises(ValueError, match="already"):
        editing.add_track(cfg, db, dup, 1)
    history.discard_pending(db)
    with pytest.raises(ValueError, match="no staged session"):
        editing.add_track(cfg, db, lib[0], 1)


def test_position_is_clamped(env):
    cfg, db, lib, folder = env
    used = {t["file"] for t in manifest(db)["tracks"]}
    new = [t for t in lib if str(t.path) not in used]
    n = len(manifest(db)["tracks"])
    editing.add_track(cfg, db, new[0], 999)
    editing.add_track(cfg, db, new[1], -5)
    names = staged(folder)
    assert names[0].startswith("001 - ") and Path(manifest(db)["tracks"][0]["file"]) == new[1].path
    assert Path(manifest(db)["tracks"][-1]["file"]) == new[0].path and len(names) == n + 2


def test_remove_tracks_renumbers_and_deletes_files(env):
    cfg, db, lib, folder = env
    m0 = manifest(db)
    n = len(m0["tracks"])
    m = editing.remove_tracks(cfg, db, {2, 5})
    assert len(staged(folder)) == n - 2 and [t["position"] for t in m["tracks"]] == list(range(1, n - 1))
    gone = {Path(m0["tracks"][1]["file"]).name, Path(m0["tracks"][4]["file"]).name}
    assert not any(Path(x).name in gone for x in [n_.split(" - ", 1)[1] for n_ in staged(folder)])
    with pytest.raises(ValueError):
        editing.remove_tracks(cfg, db, {999})


def test_move_track(env):
    cfg, db, lib, folder = env
    m0 = manifest(db)
    last = m0["tracks"][-1]
    m = editing.move_track(cfg, db, len(m0["tracks"]), 1)
    assert m["tracks"][0]["file"] == last["file"] and staged(folder)[0] == f"001 - {Path(last['file']).name}"
    assert len(staged(folder)) == len(m0["tracks"])


def test_scrobbling_uses_the_edited_playlist(env):
    cfg, db, lib, folder = env
    used = {t["file"] for t in manifest(db)["tracks"]}
    new = next(t for t in lib if str(t.path) not in used)
    editing.add_track(cfg, db, new, 1)  # plays first
    from movescrobble.session import played_part
    plan, _ = played_part(manifest(db))
    assert plan[0].track.title == new.title


def test_dashboard_and_stats(env):
    cfg, db, lib, folder = env
    now = time.time()
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)",
                   [(int(now) - 86400 * d, "A", "x") for d in (1, 2, 3, 40)] + [(int(now) - 86400 * 400, "B", "old")])
    db.execute("INSERT OR REPLACE INTO meta VALUES('last_sync', ?)", (str(now - 7200),))
    db.commit()
    d = stats.dashboard(cfg, db, now, agent_installed=True)
    assert d.device_connected and d.staged["tracks"] > 10 and d.staged["swim_tracks"] == 12
    assert d.scrobbles == 5 and d.scrobbles_30d == 3 and abs(d.last_sync_hours - 2) < 0.01 and d.agent_installed
    assert stats.top_artists(db, now=now) == [("A", 3)] and stats.top_tracks(db, now=now) == [("A", "x", 3)]
    mix = stats.listening_mix(db, now=now)
    assert mix["tracks"] == 1 and mix["new to you"] == 0.0 and mix["repeat"] == 1.0  # A/x was also played 40 days ago
    db.execute("INSERT INTO scrobbles VALUES(?,?,?)", (int(now) - 86400, "C", "brand new"))
    db.execute("INSERT INTO scrobbles VALUES(?,?,?)", (int(now) - 86400 * 500, "D", "comeback"))
    db.execute("INSERT INTO scrobbles VALUES(?,?,?)", (int(now) - 86400 * 2, "D", "comeback"))
    mix = stats.listening_mix(db, now=now)
    assert mix["tracks"] == 3 and abs(mix["new to you"] - 1 / 3) < 1e-9 and abs(mix["back after 1y+"] - 1 / 3) < 1e-9


def test_swim_split(env):
    cfg, db, lib, folder = env
    assert stats.swim_split(manifest(db), 60) == (12, 3600.0)  # 12 x 300 s = 60 min
    assert stats.swim_split(manifest(db), 1)[0] == 1


def test_swim_stats_after_a_scrobbled_session(env):
    cfg, db, lib, folder = env
    sid = history.pending_session(db)["id"]
    history.mark_scrobbled(db, sid, [("A", "x", 1.0), ("A", "x", 2.0), ("B", "y", 3.0)])
    s = stats.swim_stats(db)
    assert s["sessions"] == {"scrobbled": 1} and s["swim_minutes"] == 60 and s["most_swum"][0] == ("A", "x", 2)


def test_top_tracks_skip_podcast_episodes(env):
    cfg, db, *_ = env
    now = time.time()
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)",
                   [(int(now) - 100 * i, "Pod", "Weekly Show | #12") for i in range(1, 6)]
                   + [(int(now) - 100 * i, "Band", "Real Song") for i in range(10, 13)])
    db.commit()
    assert stats.top_tracks(db, now=now) == [("Band", "Real Song", 3)]


def test_wishlist_is_interleaved_by_bucket_share(tmp_path, monkeypatch):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path))
    rows = [f"L{i} - x\t[lost]\tu" for i in range(10)] + [f"O{i} - x\t[obsession]\tu" for i in range(10)] \
        + [f"R{i} - x\t[rec]\tu" for i in range(10)]
    (tmp_path / "wishlist.txt").write_text("\n".join(rows) + "\n")
    plain = [b for _, b, _ in stats.wishlist_lines()]
    assert plain[:10] == ["lost"] * 10  # file order: sorted by score, one bucket first
    mixed = [b for _, b, _ in stats.wishlist_lines({"obsession": 0.5, "rec": 0.3, "lost": 0.2})][:10]
    assert mixed.count("obsession") == 5 and mixed.count("rec") == 3 and mixed.count("lost") == 2
