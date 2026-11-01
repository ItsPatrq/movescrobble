import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from swimscrobble import flow, history, stats
from swimscrobble.config import Config
from swimscrobble.energy import Features
from swimscrobble.library import Track
from swimscrobble.shell import Actions, Shell


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWIMSCROBBLE_HOME", str(tmp_path / "home"))
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
    return cfg, db, lib, vol / "SWIM PRO" / "Music", music


class Fake:
    """Scripted stand-ins for everything that would touch the network or the OS."""

    def __init__(self, lib, music):
        self.lib, self.music = lib, music
        self.calls = []
        self.agent = False
        self.energy = 0.8
        self.yt = [{"videoId": "vid1", "title": "Fresh Song", "artists": [{"name": "New Artist"}],
                    "album": {"name": "New Album"}, "duration_seconds": 250}]

    def actions(self) -> Actions:
        def download(vid):
            self.calls.append(("download", vid))
            f = self.music / "New Artist - Fresh Song.mp3"
            f.write_bytes(b"newaudio")
            self.lib.append(Track(f, "New Artist", "Fresh Song", "New Album", 250))
            return True

        def submit(sc):
            self.calls.append(("submit", len(sc)))
            return f"Last.fm accepted {len(sc)}/{len(sc)}"

        return Actions(
            load_library=lambda: list(self.lib), features=lambda t: Features(120, -7, 150, 0.6, self.energy),
            sync=lambda dl: (self.calls.append(("sync", dl)) or "pool +1 -0 · playlist 3/3 found · 5 wishlist entries"),
            search_youtube=lambda q: (self.calls.append(("yt", q)) or self.yt), download_video=download,
            find_song=lambda a, t: SimpleNamespace(video_id="wish1") if a != "Nobody" else None,
            podcasts=lambda: "podcasts: 2 downloaded", on_connect=lambda: ["staged session X"], submit=submit,
            agent_installed=lambda: self.agent, agent_set=lambda on: setattr(self, "agent", on),
        )


def make(env, *inputs):
    cfg, db, lib, folder, music = env
    fake = Fake(lib, music)
    out = io.StringIO()
    answers = iter(inputs)

    def ask(prompt):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    shell = Shell(cfg, db, fake.actions(), Console(file=out, width=140, force_terminal=False, color_system=None), ask)
    return shell, fake, out


def text(out):
    return re.sub(r"\s+", " ", out.getvalue())


def stage(env):
    cfg, db, lib, folder, music = env
    flow.on_connect(cfg, db, library_loader=lambda _: lib, notify=lambda m: None,
                    feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8))


def test_dashboard_shows_device_stage_history_and_hint(env):
    cfg, db, *_ = env
    db.execute("INSERT INTO scrobbles VALUES(1900000000,'A','x')")
    db.commit()
    shell, _, out = make(env, "q")
    shell.run()
    t = text(out)
    assert "SWIM PRO connected" in t and "nothing waiting" in t and "1 scrobbles" in t and "press r" in t
    assert "Agent" in t and "not installed" in t


def test_quit_words_and_eof_exit_cleanly(env):
    for inputs in (("quit",), ("exit",), ()):
        shell, _, _ = make(env, *inputs)
        shell.run() if inputs else pytest.raises(SystemExit).__enter__() and None
    shell, _, _ = make(env)
    with pytest.raises(SystemExit):
        shell.run()


def test_reroll_stages_and_shows_the_playlist(env):
    cfg, db, lib, folder, _ = env
    shell, fake, out = make(env, "r", "n", "intervals", "42", "", "q")  # no sync first, intervals, seed 42, leave view
    shell.run()
    t = text(out)
    assert json.loads(history.pending_session(db)["manifest"])["workout"] == "intervals"
    assert history.pending_session(db) is not None and len(list(folder.iterdir())) >= 10
    assert "staged session" in t and "end of your swim" in t and ("sync", False) not in fake.calls


def test_reroll_replaces_existing_playlist_after_confirmation_and_can_be_declined(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    first = history.pending_session(db)["id"]
    shell, _, _ = make(env, "r", "n", "q")  # decline the replacement
    shell.run()
    assert history.pending_session(db)["id"] == first
    shell, _, _ = make(env, "r", "y", "n", "", "7", "", "q")
    shell.run()
    assert history.pending_session(db)["id"] != first


def test_reroll_with_sync_calls_the_sync_action(env):
    shell, fake, _ = make(env, "r", "y", "", "1", "", "q")
    shell.run()
    assert ("sync", False) in fake.calls


def test_reroll_without_device(env):
    cfg, db, lib, folder, _ = env
    folder.rmdir()
    folder.parent.rmdir()
    shell, _, out = make(env, "r", "q")
    shell.run()
    assert "not connected" in text(out) and history.pending_session(db) is None


def test_playlist_edit_commands_delete_and_move(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    before = json.loads(history.pending_session(db)["manifest"])["tracks"]
    shell, _, out = make(env, "p", "d 2,3", "m 5 1", "", "q")
    shell.run()
    after = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert len(after) == len(before) - 2 and len(list(folder.iterdir())) == len(after)
    assert after[0]["file"] == before[6]["file"]  # old position 7 -> 5 after deleting two, then moved to 1


def test_add_track_from_library_at_a_position(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    used = {t["file"] for t in json.loads(history.pending_session(db)["manifest"])["tracks"]}
    new = next(t for t in lib if str(t.path) not in used and len(t.title) == 6)  # 'SongNN': a unique search
    shell, _, out = make(env, "a", new.title.lower(), "1", "2", "q")
    shell.run()
    tracks = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert tracks[1]["file"] == str(new.path) and tracks[1]["source"] == "added"
    assert (folder / f"002 - {new.path.name}").exists() and "added" in text(out)


def test_add_track_default_position_is_the_end_of_the_swim(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    used = {t["file"] for t in json.loads(history.pending_session(db)["manifest"])["tracks"]}
    new = next(t for t in lib if str(t.path) not in used and len(t.title) == 6)
    shell, _, _ = make(env, "a", new.title.lower(), "1", "", "q")
    shell.run()
    tracks = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert tracks[11]["file"] == str(new.path)  # 12 tracks fill the 60-minute swim


def test_add_track_from_youtube_downloads_and_inserts(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    shell, fake, out = make(env, "a", "fresh song", "y", "1", "1", "q")
    shell.run()
    assert ("download", "vid1") in fake.calls
    tracks = json.loads(history.pending_session(db)["manifest"])["tracks"]
    assert tracks[0]["title"] == "Fresh Song"


def test_add_track_warns_about_slow_tracks_and_respects_no(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    used = {t["file"] for t in json.loads(history.pending_session(db)["manifest"])["tracks"]}
    new = next(t for t in lib if str(t.path) not in used)
    shell, fake, out = make(env, "a", new.title.lower(), "1", "1", "n", "q")
    fake.energy = 0.3
    n = len(json.loads(history.pending_session(db)["manifest"])["tracks"])
    shell.run()
    assert len(json.loads(history.pending_session(db)["manifest"])["tracks"]) == n  # declined


def test_add_track_without_a_staged_playlist(env):
    shell, _, out = make(env, "a", "q")
    shell.run()
    assert "No staged playlist" in text(out)


def test_stats_screen(env):
    cfg, db, lib, folder, _ = env
    import time
    now = int(time.time())
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)", [(now - 3600 * d, "Band", f"t{d % 3}") for d in range(1, 30)])
    db.commit()
    stage(env)
    shell, _, out = make(env, "s", "q")
    shell.run()
    t = text(out)
    assert "Top artists" in t and "Band" in t and "Library: 30 tracks" in t and "Swims:" in t and "new to you" in t


def test_wishlist_download(env, tmp_path):
    f = tmp_path / "home" / "wishlist.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("Alpha - One\t[rec]\thttps://x/1\nNobody - Ghost\t[fresh]\thttps://x/2\nGamma - Three\t[obsession]\thttps://x/3\n")
    shell, fake, out = make(env, "w", "1,2,3", "q")
    shell.run()
    t = text(out)
    assert t.count("downloaded") == 2 and "could not get" in t and fake.calls.count(("download", "wish1")) == 2


def test_wishlist_empty(env):
    shell, _, out = make(env, "w", "q")
    shell.run()
    assert "wishlist is empty" in text(out)


def test_sync_screen_passes_the_download_choice(env):
    shell, fake, out = make(env, "y", "n", "q")
    shell.run()
    assert ("sync", False) in fake.calls and "pool +1" in text(out)


def test_scrobble_flow_settles_and_offers_the_next_playlist(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    row = db.execute("SELECT id FROM sessions").fetchone()["id"]
    db.execute("UPDATE sessions SET planned_at = planned_at - 7200")  # staged two hours ago
    db.commit()
    shell, fake, out = make(env, "c", "50", "2", "y", "n", "q")  # swam 50 min, skipped #2, send, no restage
    shell.run()
    assert fake.calls[-1][0] == "submit" and 8 <= fake.calls[-1][1] <= 10
    assert db.execute("SELECT status FROM sessions WHERE id=?", (row,)).fetchone()["status"] == "scrobbled"
    assert "Last.fm accepted" in text(out) and history.pending_session(db) is None


def test_scrobble_failure_keeps_the_session(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    db.execute("UPDATE sessions SET planned_at = planned_at - 7200")
    db.commit()
    shell, fake, out = make(env, "c", "", "", "y", "q")
    shell.act.submit = lambda s: (_ for _ in ()).throw(RuntimeError("network down"))
    shell.run()
    assert history.pending_session(db) is not None and "network down" in text(out)


def test_scrobble_right_after_staging_asks_first(env):
    stage(env)
    shell, fake, _ = make(env, "c", "n", "q")
    shell.run()
    assert not [c for c in fake.calls if c[0] == "submit"]


def test_skip_discards_without_scrobbling(env):
    cfg, db, *_ = env
    stage(env)
    shell, fake, out = make(env, "k", "y", "n", "q")  # skip, and keep the files on the headphones
    shell.run()
    assert history.pending_session(db) is None and not fake.calls and "nothing was scrobbled" in text(out)
    assert len(list(env[3].iterdir())) >= 10  # files untouched


def test_skip_can_also_remove_the_files_from_the_headphones(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    shell, _, out = make(env, "k", "y", "y", "q")
    shell.run()
    assert list(folder.iterdir()) == [] and "removed" in text(out)


def test_dashboard_warns_about_leftover_files_and_they_can_be_adopted_or_removed(env):
    cfg, db, lib, folder, _ = env
    stage(env)
    n = len(list(folder.iterdir()))
    history.discard_pending(db)  # the database forgets, the headphones do not
    shell, _, out = make(env, "q")
    shell.run()
    assert "none tracked" in text(out) and f"{n} tracks of discarded session" in text(out)
    shell, _, out = make(env, "u", "u", "q")
    shell.run()
    assert history.pending_session(db) is not None and "current playlist again" in text(out)
    history.discard_pending(db)
    shell, _, out = make(env, "u", "x", "y", "q")
    shell.run()
    assert list(folder.iterdir()) == [] and history.pending_session(db) is None


def test_agent_toggle_podcasts_and_on_connect(env):
    shell, fake, out = make(env, "g", "y", "o", "e", "q")
    shell.run()
    assert fake.agent is True
    t = text(out)
    assert "podcasts: 2 downloaded" in t and "staged session X" in t


def test_unknown_keys_and_help_do_not_crash(env):
    shell, _, out = make(env, "zzz", "?", "q")
    shell.run()
    t = text(out)
    assert "unknown key" in t and "reroll" in t and "wishlist" in t


def test_an_action_error_does_not_kill_the_shell(env):
    shell, fake, out = make(env, "y", "n", "s", "q")
    shell.act.sync = lambda dl: (_ for _ in ()).throw(RuntimeError("boom"))
    shell.run()
    assert "error: boom" in text(out) and "Top artists" in text(out)


def test_status_helper_matches_dashboard(env):
    cfg, db, *_ = env
    stage(env)
    d = stats.dashboard(cfg, db)
    assert d.staged["tracks"] >= 10 and d.pool == {} and d.device_connected


def test_bare_command_prints_help_when_not_a_terminal(env, capsys, monkeypatch):
    from swimscrobble import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    cli.main([])
    assert "usage: swimscrobble" in capsys.readouterr().out


def test_paths_are_real(env):
    _, _, lib, _, _ = env
    assert all(Path(t.path).exists() for t in lib)


def test_scrobble_screen_offers_the_garmin_swim(env):
    from datetime import datetime

    from swimscrobble.garmin import Swim

    cfg, db, *_ = env
    stage(env)
    db.execute("UPDATE sessions SET planned_at = planned_at - 7200")
    db.commit()
    shell, fake, out = make(env, "c", "", "", "y", "n", "q")  # accept the Garmin length, no skips, send
    shell.act.find_swim = lambda since, until: Swim(datetime(2026, 10, 25, 8, 0), 20.0, "lap_swimming")
    shell.run()
    # 7 warm-up + 20 swum + 10 after, of 5-min tracks
    assert "Garmin: lap swimming" in text(out) and fake.calls[-1] == ("submit", 7)


def test_podcast_screen_marks_finished_episodes_then_syncs(env):
    shell, fake, out = make(env, "o", "2", "q")
    eps = [{"uuid": "e1", "podcast": "p", "title": "One", "filename": "01 - One.mp3"},
           {"uuid": "e2", "podcast": "p", "title": "Two", "filename": "02 - Two.mp3"}]
    marked = []
    shell.act.podcast_episodes = lambda: eps
    shell.act.mark_played = lambda chosen: (marked.extend(chosen) or "marked 1 played")
    shell.run()
    assert marked == [eps[1]] and "marked 1 played" in text(out) and "podcasts: 2 downloaded" in text(out)


def test_liked_discoveries_are_loved(env):
    from swimscrobble.planner import PlannedTrack

    cfg, db, lib, *_ = env
    shell, fake, out = make(env, "2")
    loved = []
    shell.act.love = lambda tracks: (loved.extend(tracks) or "loved 1 on Last.fm")
    heard = [PlannedTrack(1, lib[0], "obsession"), PlannedTrack(2, lib[1], "rec"), PlannedTrack(3, lib[2], "fresh")]
    shell.discoveries(heard)
    assert loved == [(lib[2].artist, lib[2].title)] and "loved 1" in text(out)  # #2 of the two new ones


def test_block_from_the_playlist_removes_it_and_keeps_the_artist_out_of_rerolls(env):
    cfg, db, *_ = env
    stage(env)
    _, m = make(env)[0].session()
    victim = sorted(m["tracks"], key=lambda t: t["position"])[0]
    shell, _, out = make(env, "p", "b 1", "2", "", "q")
    shell.run()
    assert f"blocked {victim['artist']} (all tracks)" in text(out)
    _, m = shell.session()
    assert victim["artist"] not in {t["artist"] for t in m["tracks"]}
    m = flow.reroll(cfg, db, seed=3, library_loader=lambda _: env[2], feature_of=lambda t: Features(120, -7, 150, 0.6, 0.8))
    assert victim["artist"] not in {t["artist"] for t in m["tracks"]}


def test_blocklist_screen_unblocks_and_blocks(env):
    from swimscrobble import blocklist

    cfg, db, *_ = env
    blocklist.block(db, "Artist1")
    blocklist.block(db, "Artist2", "Song2")
    shell, _, out = make(env, "b", "1", "The Weeknd", "q")
    shell.run()
    assert blocklist.entries(db) == [("Artist2", "Song2"), ("The Weeknd", "")]
    assert "unblocked Artist1 (all tracks)" in text(out)


def test_discover_in_line_mode_lists_previews_and_steers(env):
    from test_keyboard import fake_taste

    from swimscrobble.taste import Tuning

    cfg, db, *_ = env
    shell, fake, out = make(env, "d", "g1", "+Artist5", "", "q")
    played = []
    shell.act.recommend = fake_taste
    shell.act.preview_song = lambda a, t: played.append((a, t)) or True
    shell.run()
    t = text(out)
    assert "In your files" in t and "To get" in t and "New Artist - Fresh Song" in t
    assert played == [("New Artist", "Fresh Song")] and Tuning.load(db).more == ["Artist5"]
