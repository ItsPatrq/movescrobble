import math

from movescrobble import affinity, history

DAY = 86400
NOW = 1000 * DAY


def put(db, rows):
    db.executemany("INSERT INTO scrobbles VALUES(?,?,?)", rows)
    db.commit()


def test_decay_halves_every_half_life_and_ranks_recent_above_old(tmp_path):
    db = history.connect(tmp_path / "h.db")
    put(db, [(NOW - 0 * DAY - 1, "A", "fresh"), (NOW - 30 * DAY, "A", "month"), (NOW - 60 * DAY, "A", "two")])
    tracks, _ = affinity.compute(db, NOW)
    assert math.isclose(tracks[("A", "fresh")].now, 1.0, rel_tol=1e-3)
    assert math.isclose(tracks[("A", "month")].now, 0.5, rel_tol=1e-3)
    assert math.isclose(tracks[("A", "two")].now, 0.25, rel_tol=1e-3)
    assert math.isclose(tracks[("A", "month")].long, 2 ** (-30 / 180), rel_tol=1e-3)


def test_totals_loyalty_and_last_use_the_whole_history(tmp_path):
    db = history.connect(tmp_path / "h.db")
    ancient = [(NOW - (2000 + 7 * i) * DAY, "B", "old") for i in range(5)]  # beyond the decay lookback
    put(db, ancient)
    tracks, artists = affinity.compute(db, NOW)
    t = tracks[("B", "old")]
    assert t.total == 5 and t.weeks == 5 and t.last == NOW - 2000 * DAY and t.now == 0.0 and t.long == 0.0
    assert artists["B"].total == 5 and artists["B"].now == 0.0


def test_artist_affinity_aggregates_all_tracks(tmp_path):
    db = history.connect(tmp_path / "h.db")
    put(db, [(NOW - 1, "C", "x"), (NOW - 2, "C", "y"), (NOW - 45 * DAY, "C", "z")])
    _, artists = affinity.compute(db, NOW)
    assert math.isclose(artists["C"].now, 2.5, rel_tol=1e-3)


def test_future_plays_are_ignored(tmp_path):
    db = history.connect(tmp_path / "h.db")
    put(db, [(NOW + 5 * DAY, "D", "later")])
    tracks, _ = affinity.compute(db, NOW)
    assert ("D", "later") not in tracks


def test_squash_is_monotone_and_capped():
    assert affinity.squash(0, 10) == 0 and affinity.squash(10, 10) == 1 and affinity.squash(500, 10) == 1
    assert affinity.squash(2, 10) < affinity.squash(5, 10)
