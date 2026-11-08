from pathlib import Path

from movescrobble import acquire, generator, history, matcher, pocketcasts, poolsync, ytm
from movescrobble.library import Track
from movescrobble.planner import plan_session


def T(a, t, d=200):
    return Track(Path(f"{a}-{t}.mp3"), a, t, "", d)


def test_ytm_parse_topic_and_plain_upload():
    data = {"entries": [
        {"id": "a1", "title": "Sansara", "channel": "Closterkeller - Topic", "duration": 311},
        {"id": "b2", "title": "Nine Inch Nails - Closer", "channel": "SomeUploader", "duration": 372},
        None, {"title": "no id"},
    ]}
    e = ytm.parse(data)
    assert [(x.artist, x.title) for x in e] == [("Closterkeller", "Sansara"), ("Nine Inch Nails", "Closer")]


def test_ytm_fetch_uses_runner():
    class P: stdout = '{"entries":[{"id":"x","title":"T","channel":"A - Topic"}]}'
    seen = {}
    def run(cmd, **kw): seen["cmd"] = cmd; return P()
    assert ytm.fetch("https://u", run)[0].video_id == "x" and "--flat-playlist" in seen["cmd"]


def test_matcher_statuses():
    lib = [T("Closterkeller", "Sansara", 311), T("Depeche Mode", "Martyr", 200)]
    entries = [
        ytm.Entry("1", "Closterkeller", "Sansara (Remastered)", 311),
        ytm.Entry("2", "Depeche Mode", "Martyr [MIXED]", 200),
        ytm.Entry("3", "Totally Else", "Unknown Song", 100),
    ]
    r = matcher.match(entries, lib)
    assert [x.status for x in r] == [matcher.MATCH, matcher.MATCH, matcher.MISSING]
    assert r[0].track.title == "Sansara" and r[2].track is None


def test_norm_strips_noise():
    assert matcher.norm("Sansara (feat. X) [MIXED]") == "sansara"
    assert matcher.norm("Mój sen srebrny") == matcher.norm("Moj sen srebrny")


def test_acquire_gate_and_failures(tmp_path):
    es = [ytm.Entry("a", "A", "T", None), ytm.Entry("b", "B", "U", None)]
    calls = []
    class R:
        def __init__(s, c): s.returncode = c
    def run(cmd): calls.append(cmd); return R(1 if cmd[-1].endswith("=b") else 0)
    assert acquire.acquire(es, tmp_path / "d", run, ask=lambda _: "n") == es and not calls
    failed = acquire.acquire(es, tmp_path / "d", run, ask=lambda _: "y")
    assert failed == [es[1]] and len(calls) == 2 and "mp3" in calls[0]


def test_weighted_plan_prefers_heavy():
    lib = [T(f"A{i}", f"T{i}", 600) for i in range(30)]
    heavy = {(f"a{i}", f"t{i}"): 1000.0 for i in range(6)}
    wins = 0
    for seed in range(20):
        plan = plan_session(lib, minutes=60, seed=seed, weights=heavy)
        wins += sum(1 for p in plan if (p.track.artist.casefold(), p.track.title.casefold()) in heavy)
    assert wins / 20 > 4  # of 6 heavy tracks, most make a 6-track cut


def test_poolsync(tmp_path):
    db = history.connect(tmp_path / "h.db")
    cands = [generator.Candidate("A", "one", 3, {"playlist"}), generator.Candidate("B", "two", 2, {"top"}),
             generator.Candidate("C", "gone", 1, {"top"}), generator.Candidate("D", "over", 1, {"top"})]
    lib = {("a", "one"), ("b", "two"), ("d", "over")}
    rep = poolsync.sync_pool(db, cands, lib, {("d", "over"): 9}, max_size=1)
    assert rep.added == [("a", "one")] and [c.title for c in rep.missing] == ["gone"]
    assert poolsync.active_keys(db) == {("a", "one")}
    rep2 = poolsync.sync_pool(db, [cands[1]], lib, {}, max_size=5)
    assert rep2.retired == [("a", "one")] and poolsync.active_keys(db) == {("b", "two")}


def test_pocketcasts_queue_sync(tmp_path):
    db = history.connect(tmp_path / "h.db")
    eps = [{"uuid": f"u{i}", "title": f"Ep: {i}", "url": f"https://x/{i}.mp3?t=1", "podcast": "p"} for i in range(3)]
    got = []
    def dl(url, dest): got.append(url); dest.write_bytes(b"x")
    dest = tmp_path / "dev"
    r = pocketcasts.sync_queue(eps, dest, db, max_episodes=2, fetcher=dl)
    assert sorted(p.name for p in dest.iterdir()) == ["01 - Ep 0.mp3", "02 - Ep 1.mp3"] and len(got) == 2
    # queue changes: first episode finished, order shifts
    r = pocketcasts.sync_queue(eps[1:], dest, db, max_episodes=2, fetcher=dl)
    assert sorted(p.name for p in dest.iterdir()) == ["01 - Ep 1.mp3", "02 - Ep 2.mp3"]
    assert r.removed == ["01 - Ep 0.mp3"] and r.renamed == ["01 - Ep 1.mp3"] and len(got) == 3


def test_pocketcasts_failed_download_skips_only_that_episode(tmp_path):
    db = history.connect(tmp_path / "h.db")
    eps = [{"uuid": f"u{i}", "title": f"Ep {i}", "url": f"https://x/{i}.mp3"} for i in range(3)]
    def dl(url, dest):
        if url.endswith("1.mp3"):
            raise RuntimeError("HTTP Error 403: Forbidden")
        dest.write_bytes(b"x")
    dest = tmp_path / "dev"
    r = pocketcasts.sync_queue(eps, dest, db, fetcher=dl)
    assert sorted(p.name for p in dest.iterdir()) == ["01 - Ep 0.mp3", "03 - Ep 2.mp3"]
    assert len(r.failed) == 1 and "403" in r.failed[0]
    assert "u1" not in {row["uuid"] for row in db.execute("SELECT uuid FROM podcasts")}  # retried next time


def test_pocketcasts_latest_unplayed_per_subscription():
    subs = [
        {"uuid": "old", "lastEpisodeUuid": "e-old", 
         "lastEpisodePublished": "2026-10-01T00:00:00Z", "lastEpisodePlayingStatus": 1},
        {"uuid": "new", "lastEpisodeUuid": "e-new", 
         "lastEpisodePublished": "2026-10-20T00:00:00Z", "lastEpisodePlayingStatus": 2},
        {"uuid": "done", "lastEpisodeUuid": "e-done", 
         "lastEpisodePublished": "2026-10-24T00:00:00Z", "lastEpisodePlayingStatus": 3},
        {"uuid": "arch", "lastEpisodeUuid": "e-arch", 
         "lastEpisodePublished": "2026-10-23T00:00:00Z", "lastEpisodeArchived": True},
        {"uuid": "mid", "lastEpisodeUuid": "e-mid", 
         "lastEpisodePublished": "2026-10-10T00:00:00Z", "lastEpisodePlayingStatus": 1},
    ]
    calls = []
    def t(path, body, tok):
        calls.append(path)
        if path == "/user/podcast/list":
            return {"podcasts": subs}
        return {"uuid": body["uuid"], "title": body["uuid"], "url": f"https://x/{body['uuid']}.mp3"}
    assert [e["uuid"] for e in pocketcasts.latest_unplayed("tok", 10, t)] == ["e-new", "e-mid", "e-old"]
    calls.clear()
    assert [e["uuid"] for e in pocketcasts.latest_unplayed("tok", 2, t)] == ["e-new", "e-mid"]
    assert calls.count("/user/episode") == 2  # never fetches past the cap


def test_pocketcasts_login_saves_token_private(monkeypatch, tmp_path):
    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path))
    tok = pocketcasts.login("e", "p", lambda path, body, t: {"token": "abc"})
    assert tok == "abc" and pocketcasts.saved_token() == "abc"
    assert (tmp_path / "pocketcasts.json").stat().st_mode & 0o777 == 0o600


def test_tokens_are_never_readable_by_others_even_for_a_moment(monkeypatch, tmp_path):
    import os
    import stat
    from pathlib import Path

    from movescrobble import config

    monkeypatch.setenv("MOVESCROBBLE_HOME", str(tmp_path))
    old = os.umask(0)  # the loosest a shell can be: a file made then chmod-ed is world-readable until the chmod
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)  # what is on disk before any chmod
    try:
        config.write_private_json("lastfm.json", {"session_key": "s"})
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "lastfm.json").stat().st_mode) == 0o600
