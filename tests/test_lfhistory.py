import pytest

from movescrobble import history, lfhistory

NOW = 100 * 86400
DAY = 86400


def page(tracks, page_no, pages, nowplaying=False):
    items = [{"artist": {"#text": a}, "name": t, "date": {"uts": str(ts)}} for a, t, ts in tracks]
    if nowplaying:
        items.insert(0, {"artist": {"#text": "Live"}, "name": "Now", "@attr": {"nowplaying": "true"}})
    return {"recenttracks": {"track": items, "@attr": {"page": str(page_no), "totalPages": str(pages)}}}


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")


def test_pages_are_followed_and_nowplaying_skipped(tmp_path):
    db = history.connect(tmp_path / "h.db")
    pages = {
        1: page([("A", "x", 300), ("A", "x", 200)], 1, 2, nowplaying=True),
        2: page([("B", "y", 100)], 2, 2),
    }
    seen = []
    n = lfhistory.sync_history(db, "me", lambda p: (seen.append(p) or pages[p["page"]]), sleep=lambda s: None)
    assert n == 3 and [p["page"] for p in seen] == [1, 2] and "from" not in seen[0]
    assert db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"] == 3


def test_incremental_sync_only_asks_for_newer_and_is_idempotent(tmp_path):
    db = history.connect(tmp_path / "h.db")
    lfhistory.sync_history(db, "me", lambda p: page([("A", "x", 300)], 1, 1), sleep=lambda s: None)
    seen = []
    n = lfhistory.sync_history(db, "me", lambda p: (seen.append(p) or page([("A", "x", 300), ("A", "z", 400)], 1, 1)),
                               sleep=lambda s: None)
    assert seen[0]["from"] == 301 and n == 1  # the overlapping row is not double counted
    assert db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"] == 2


def test_single_track_response_not_wrapped_in_list(tmp_path):
    db = history.connect(tmp_path / "h.db")
    one = {"recenttracks": {"track": {"artist": {"#text": "A"}, "name": "x", "date": {"uts": "5"}},
                            "@attr": {"totalPages": "1"}}}
    assert lfhistory.sync_history(db, "me", lambda p: one, sleep=lambda s: None) == 1


def test_stats_windows(tmp_path):
    db = history.connect(tmp_path / "h.db")
    rows = [(NOW - 5 * DAY, "A", "x"), (NOW - 10 * DAY, "A", "x"), (NOW - 40 * DAY, "A", "x"),
            (NOW - 70 * DAY, "A", "x"), (NOW - 200 * DAY, "A", "x"), (NOW - 200 * DAY, "B", "old")]
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)", rows)
    st = lfhistory.stats(db, NOW)
    assert st[("A", "x")] == lfhistory.Stat(total=5, p30=2, p60=1, p90=1, last=NOW - 5 * DAY)
    assert st[("B", "old")].total == 1 and st[("B", "old")].p30 == 0


def test_interrupted_pull_resumes_instead_of_restarting(tmp_path):
    db = history.connect(tmp_path / "h.db")
    pages = {1: page([("A", "x", 900), ("A", "x", 800)], 1, 3), 2: page([("A", "x", 700), ("A", "x", 600)], 2, 3),
             3: page([("A", "x", 500)], 3, 3)}

    def dies_on_page_3(p):
        if p["page"] == 3:
            raise RuntimeError("network down")
        return pages[p["page"]]

    with pytest.raises(RuntimeError):
        lfhistory.sync_history(db, "me", dies_on_page_3, sleep=lambda s: None)
    assert db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"] == 4
    assert db.execute("SELECT value FROM meta WHERE key='lastfm_history_ts'").fetchone() is None  # not "done" yet

    seen = []
    # on resume Last.fm only returns what is at or before the oldest scrobble we already have
    resumed = page([("A", "x", 600), ("A", "x", 500)], 1, 1)
    n = lfhistory.sync_history(db, "me", lambda p: (seen.append(p) or resumed), sleep=lambda s: None)
    assert seen[0]["to"] == 600 and "from" not in seen[0] and n == 1
    assert db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"] == 5
    assert db.execute("SELECT value FROM meta WHERE key='lastfm_history_ts'").fetchone()["value"] == "900"
    assert db.execute("SELECT 1 FROM meta WHERE key='history_resume_to'").fetchone() is None


def test_first_pull_is_capped_by_first_since_but_later_pulls_are_incremental(tmp_path):
    db = history.connect(tmp_path / "h.db")
    seen = []
    lfhistory.sync_history(db, "me", lambda p: (seen.append(p) or page([("A", "x", 5000)], 1, 1)),
                           sleep=lambda s: None, first_since=1234)
    assert seen[0]["from"] == 1235  # asked only for the last year, not the whole decade
    lfhistory.sync_history(db, "me", lambda p: (seen.append(p) or page([], 1, 1)), sleep=lambda s: None, first_since=1234)
    assert seen[1]["from"] == 5001  # from the newest scrobble we hold, not first_since again
