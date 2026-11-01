from pathlib import Path

from swimscrobble import acquire, history
from swimscrobble.config import Config
from swimscrobble.library import Track, scan
from swimscrobble.libsync import sync_library
from swimscrobble.link import LocalIndex
from swimscrobble.ytm import Entry

NOW = 100 * 86400.0


def T(artist, title, dur=200, name=None):
    return Track(Path(name or f"{artist}-{title}.mp3"), artist, title, "", dur)


def test_index_tolerates_noise_and_credits():
    idx = LocalIndex([T("Depeche Mode, War Child Records", "Universal Soldier"), T("Closterkeller", "Białe wino")])
    assert idx.find("Depeche Mode", "Universal Soldier (Remastered)")
    assert idx.find("Closterkeller", "Biale wino [MIXED]")
    assert idx.find("Someone Else", "Universal Soldier") is None
    assert idx.find("", "Universal Soldier")  # missing artist tag in playlist data


def test_index_prefers_closest_duration_and_rejects_other_cut():
    idx = LocalIndex([T("A", "S", 200, "studio.mp3"), T("A", "S", 400, "live.mp3")])
    assert idx.find("A", "S", 205).path.name == "studio.mp3"
    assert idx.find("A", "S", 395).path.name == "live.mp3"
    assert LocalIndex([T("A", "S", 200)]).find("A", "S", 900) is None


def test_scan_is_recursive_and_filters(tmp_path):
    import shutil
    import subprocess
    if shutil.which("ffmpeg") is None:
        return
    for rel, dur in [("YouTubeMusic/Alb/01 - a.mp3", 70), ("Logic/loop.mp3", 70), ("x/short.mp3", 5), (".hidden/h.mp3", 70)]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=duration={dur}", str(f)], check=True)
    (tmp_path / "x" / "notes.txt").write_text("hi")
    found = [t.path.relative_to(tmp_path).as_posix() for t in scan(tmp_path, exclude=("Logic",), min_seconds=60)]
    assert found == ["YouTubeMusic/Alb/01 - a.mp3"]


def make_cfg(tmp_path):
    return Config(music_dir=str(tmp_path), ytm_playlist="https://music.youtube.com/playlist?list=X", lastfm_user="me")


def test_sync_reports_missing_and_downloads_then_rescans(tmp_path):
    db = history.connect(tmp_path / "h.db")
    cfg = make_cfg(tmp_path)
    lib = [T("Depeche Mode", "Free")]
    entries = [Entry("1", "Depeche Mode", "Free", 200, 1), Entry("2", "Depeche Mode", "Newborn", 200, 2),
               Entry("3", "Depeche Mode", "Stripped", 200, 3)]
    got = []

    def download(missing):
        got.append([e.index for e in missing])
        lib.extend([T("Depeche Mode", "Newborn"), T("Depeche Mode", "Stripped")])

    rep = sync_library(cfg, db, fetch_playlist=lambda url: entries, library_loader=lambda: list(lib), download=download)
    assert got == [[2, 3]] and rep.downloaded and rep.playlist_missing == []
    assert len(rep.pool.added) == 3

    lib2 = [T("Depeche Mode", "Free")]
    rep = sync_library(cfg, db, fetch_playlist=lambda url: entries, library_loader=lambda: list(lib2))
    assert [e.index for e in rep.playlist_missing] == [2, 3] and not rep.downloaded  # report only


def test_download_failure_leaves_entries_missing(tmp_path):
    db = history.connect(tmp_path / "h.db")
    rep = sync_library(make_cfg(tmp_path), db, fetch_playlist=lambda u: [Entry("1", "A", "Gone", 100, 1)],
                       library_loader=lambda: [], download=lambda m: None)
    assert [e.index for e in rep.playlist_missing] == [1] and rep.downloaded


def test_pool_is_the_playlist_and_history_decides_buckets(tmp_path):
    from swimscrobble.taste import Taste

    db = history.connect(tmp_path / "h.db")
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)", [(int(NOW) - 86400, "Closterkeller", "Biale wino")] * 1)
    db.commit()
    lib = [T("Closterkeller", "Białe wino"), T("NIN", "Closer"), T("Not In Playlist", "Hidden")]
    entries = [Entry("1", "Closterkeller", "Białe wino", 200, 1), Entry("2", "NIN", "Closer", 200, 2)]
    seeds = []
    taste = lambda user, tracks, artists, lib: (seeds.append((list(tracks), list(artists), lib)) or Taste())  # noqa: E731
    cfg = make_cfg(tmp_path)
    cfg.ytm_playlists = ["https://music.youtube.com/playlist?list=Y"]
    cfg.pool = "playlist"
    syncs = []
    rep = sync_library(cfg, db, fetch_playlist=lambda u: [e for e in entries], taste_fetch=taste,
                       history_sync=lambda: syncs.append(1), library_loader=lambda: lib, now=NOW)
    pool = {(r["artist"], r["title"]): r["source"] for r in db.execute("SELECT artist, title, source FROM pool")}
    assert pool == {("closterkeller", "białe wino"): "mid", ("nin", "closer"): "rec"}  # keyed by LOCAL tags; 1 play = mid
    assert ("not in playlist", "hidden") not in pool  # outside the playlist: not eligible
    assert syncs == [1] and seeds == [([("Closterkeller", "Biale wino")], ["Closterkeller"],
                                       ["Closterkeller", "NIN", "Not In Playlist"])]  # history + library artists
    assert rep.playlist_total == 4  # two playlists configured (ytm_playlist + ytm_playlists), 2 entries each

    cfg.pool = "library"
    sync_library(cfg, db, fetch_playlist=lambda u: entries, library_loader=lambda: lib, now=NOW)
    assert ("not in playlist", "hidden") in {(r["artist"], r["title"]) for r in db.execute("SELECT artist, title FROM pool")}


def test_download_command_is_the_ymp3_alias_limited_to_missing_items(tmp_path):
    cfg = Config(music_dir=str(tmp_path / "dl"), cookies_file="~/cookies.txt")
    cmd = acquire.playlist_download_command(cfg, "https://pl", [7, 2, 12])
    assert cmd[0] == "yt-dlp" and cmd[1:3] == ["--cookies", str(Path("~/cookies.txt").expanduser())]
    assert "--embed-thumbnail" in cmd and "playlist_index:%(track_number)s" in cmd
    assert cmd[-3:] == ["--playlist-items", "2,7,12", "https://pl"][-3:] or cmd[-1] == "https://pl"
    i = cmd.index("--playlist-items")
    assert cmd[i + 1] == "2,7,12" and cmd[-1] == "https://pl"
    assert any(str(tmp_path / "dl") in part for part in cmd)  # --output points into music_dir


def test_download_missing_asks_and_reports(tmp_path):
    cfg = Config(music_dir=str(tmp_path))
    ran = []
    class R:
        returncode = 0
    run = lambda cmd: (ran.append(cmd) or R())  # noqa: E731
    es = [Entry("1", "A", "T", 1, 4)]
    assert acquire.download_missing(cfg, "u", es, run, ask=lambda _: "n") is False and not ran
    assert acquire.download_missing(cfg, "u", es, run, ask=lambda _: "y") and ran[0][-3:-1] == ["--playlist-items", "4"]
    assert acquire.download_missing(cfg, "u", [], run) is True


# --- wishlist: download what you play / Last.fm suggests even if it is not in the playlist ---------

def fake_search(results):
    return lambda query: results.get(query, [])


def song(video_id, artist, title, dur=200):
    return {"videoId": video_id, "title": title, "artists": [{"name": a} for a in artist.split(", ")], "duration_seconds": dur}


def test_find_song_rejects_covers_and_lookalikes():
    res = [song("c1", "Cover Band", "Closer"), song("l1", "NIN", "Closer (Live)"), song("ok", "Nine Inch Nails", "Closer")]
    e = acquire.find_song("Nine Inch Nails", "Closer", fake_search({"Nine Inch Nails Closer": res}))
    assert e.video_id == "ok"
    assert acquire.find_song("Nine Inch Nails", "Closer", fake_search({"Nine Inch Nails Closer": res[:2]})) is None


def wish(artist, title, score=1.0):
    from swimscrobble.generator import Candidate
    return Candidate(artist, title, score, {"rec"})


def test_fetch_wishes_downloads_with_watch_url_into_the_library_and_remembers(tmp_path):
    db = history.connect(tmp_path / "h.db")
    cfg = Config(music_dir=str(tmp_path / "lib"))
    ran = []

    class R:
        returncode = 0

    run = lambda cmd: (ran.append(cmd) or R())  # noqa: E731
    search = fake_search({"A One": [song("v1", "A", "One")], "B Two": []})
    wishes = [wish("A", "One"), wish("B", "Two"), wish("C", "Three")]
    n = acquire.fetch_wishes(cfg, db, wishes, 2, search, run, now=1000)
    assert n == 1 and len(ran) == 1  # "Two" and "Three" are not on YouTube Music, and did not eat the quota of 2
    assert ran[0][-1] == "https://music.youtube.com/watch?v=v1"
    out = ran[0][ran[0].index("--output") + 1]
    assert out.startswith(str(tmp_path / "lib")) and "%(album|Singles)s" in out  # same single library folder
    # next sync: "One" is done for good, the misses wait 30 days
    assert acquire.fetch_wishes(cfg, db, wishes, 5, search, run, now=2000) == 0 and len(ran) == 1
    later = fake_search({"B Two": [song("v2", "B", "Two")]})
    assert acquire.fetch_wishes(cfg, db, wishes, 5, later, run, now=1000 + 31 * 86400) == 1 and ran[1][-1].endswith("v2")


def test_fetch_wishes_stops_at_the_quota(tmp_path):
    db = history.connect(tmp_path / "h.db")
    cfg = Config(music_dir=str(tmp_path / "lib"))

    class R:
        returncode = 0

    ran = []
    ws = [wish(f"A{i}", f"S{i}") for i in range(10)]
    search = lambda q: [song("v" + q.split()[0], q.split()[0], q.split()[1])]  # noqa: E731
    assert acquire.fetch_wishes(cfg, db, ws, 3, search, lambda c: (ran.append(c) or R())) == 3 and len(ran) == 3


def test_fetch_wishes_reports_each_download_and_keeps_yt_dlp_quiet(tmp_path, monkeypatch):
    db = history.connect(tmp_path / "h.db")
    cfg = Config(music_dir=str(tmp_path / "lib"))
    calls, steps = [], []
    monkeypatch.setattr(acquire.subprocess, "run", lambda cmd, **kw: calls.append(kw) or type("R", (), {"returncode": 0}))
    search = fake_search({"A One": [song("v1", "A", "One")]})
    assert acquire.fetch_wishes(cfg, db, [wish("A", "One")], 2, search, progress=steps.append) == 1
    assert steps == ["downloading 1/2: A - One"] and calls[0]["capture_output"]  # nothing printed over the spinner


def test_notify_passes_the_text_as_an_argument(monkeypatch):
    from swimscrobble import cli

    ran = []
    monkeypatch.setattr(cli.os, "uname", lambda: type("U", (), {"sysname": "Darwin"}))
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    cli.notify('10 podcasts · "quoted" swim')
    assert ran[0][-1] == '10 podcasts · "quoted" swim'  # not spliced into the AppleScript source


def test_fetch_wishes_respects_ask_and_failures(tmp_path):
    db = history.connect(tmp_path / "h.db")
    cfg = Config(music_dir=str(tmp_path / "lib"))
    search = fake_search({"A One": [song("v1", "A", "One")]})

    class Bad:
        returncode = 1

    ran = []
    assert acquire.fetch_wishes(cfg, db, [wish("A", "One")], 5, search, lambda c: ran.append(c), ask=lambda _: "n") == 0
    assert ran == []
    assert acquire.fetch_wishes(cfg, db, [wish("A", "One")], 5, search, lambda c: Bad(), now=1) == 0
    assert db.execute("SELECT status FROM wish_attempts").fetchone()["status"] == "failed"


def test_sync_fetches_wishes_into_the_library_and_they_join_the_pool(tmp_path):
    from swimscrobble.taste import Taste

    db = history.connect(tmp_path / "h.db")
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)", [(int(NOW) - 86400 * d, "Loved Band", "Big Hit") for d in (1, 2, 3)])
    db.commit()
    cfg = make_cfg(tmp_path)
    cfg.ytm_playlist = "https://music.youtube.com/playlist?list=P"
    cfg.pool = "playlist"  # even with a playlist-only pool, fetched wishlist files must be usable
    playlist_track = T("Closterkeller", "Sansara", name=str(tmp_path / "pl.mp3"))
    lib = [playlist_track]
    entries = [Entry("1", "Closterkeller", "Sansara", 200, 1)]
    fetched = []

    def fetch(wishes):
        fetched.append([w.title for w in wishes])
        db.execute("INSERT INTO wish_attempts VALUES('loved band|big hit', 'ok', 1)")
        lib.append(T("Loved Band", "Big Hit", name=str(tmp_path / "Singles" / "Loved Band - Big Hit.mp3")))
        return 1

    rep = sync_library(cfg, db, fetch_playlist=lambda u: entries, taste_fetch=lambda *a: Taste(),
                       library_loader=lambda: list(lib), fetch_wishes=fetch, now=NOW)
    assert fetched == [["Big Hit"]]  # played last month on Last.fm, no file, not in the playlist -> fetched
    pool = {(r["artist"], r["title"]): r["source"] for r in db.execute("SELECT artist, title, source FROM pool")}
    assert pool[("loved band", "big hit")] == "mid" and ("closterkeller", "sansara") in pool
    assert rep.pool.missing == []  # nothing left on the wishlist after the fetch


def test_balanced_wishes_follow_the_bucket_shares():
    from swimscrobble.generator import Candidate
    ws = [Candidate("A", f"o{i}", 1 - i / 100, {"obsession"}) for i in range(30)]
    ws += [Candidate("B", f"r{i}", 0.5 - i / 100, {"rec"}) for i in range(30)]
    ws += [Candidate("C", f"l{i}", 0.4 - i / 100, {"lost"}) for i in range(30)]
    out = acquire.balanced(ws, {"obsession": 0.5, "rec": 0.3, "lost": 0.2})
    first = [next(iter(c.sources)) for c in out[:10]]
    assert first.count("obsession") == 5 and first.count("rec") == 3 and first.count("lost") == 2
    assert [c.title for c in out if "obsession" in c.sources][:3] == ["o0", "o1", "o2"]  # best first
    assert len(out) == 90 and acquire.balanced(ws, None) == ws


def test_preview_command_cuts_a_section_without_tags_or_cover(tmp_path):
    cfg = make_cfg(tmp_path)
    cmd = acquire.preview_command(cfg, "vid", "/tmp/p/remote.%(ext)s", 70, 100)
    assert cmd[-1] == "https://music.youtube.com/watch?v=vid" and cmd[-4:-2] == ["--download-sections", "*70-100"]
    assert "--embed-thumbnail" not in cmd and "--parse-metadata" not in cmd and "playlist_index:%(track_number)s" not in cmd
    assert cmd[cmd.index("--output") + 1] == "/tmp/p/remote.%(ext)s" and "--extract-audio" in cmd


def test_player_streams_a_clip_and_stops_the_previous_one(tmp_path):
    from types import SimpleNamespace

    from swimscrobble.preview import Player

    started, stopped = [], []

    class Proc:
        def __init__(self, cmd, **kw):
            started.append(cmd)

        def poll(self):
            return None

        def terminate(self):
            stopped.append(1)

    def run(cmd):
        (tmp_path / "remote.mp3").write_bytes(b"clip")
    found = lambda a, t: SimpleNamespace(video_id="v", duration=200) if a != "Nobody" else None  # noqa: E731
    p = Player(make_cfg(tmp_path), folder=tmp_path, popen=Proc, run=run, find_song=found)
    assert p.play_song("Band", "Song") and started[-1][-1].endswith("remote.mp3")
    assert p.play_song("Band", "Other") and stopped == [1]  # one clip at a time
    assert not p.play_song("Nobody", "Song")
    p.stop()
    assert p.proc is None
