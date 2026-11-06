import numpy as np

from movescrobble import evaluate, history

DAY = 86400


def seed_history(db, days=1500, seed=0):
    """Synthetic listener: loves a rotating set of tracks, some of which return after a long break."""
    rng = np.random.default_rng(seed)
    end = 2000 * DAY
    rows = []
    for track in range(300):
        start = rng.integers(0, days) * DAY
        for _ in range(rng.integers(3, 25)):
            ts = int(end - days * DAY + start + rng.exponential(25) * DAY)
            if ts < end:
                rows.append((ts, f"A{track % 40}", f"T{track}"))
    db.executemany("INSERT OR IGNORE INTO scrobbles VALUES(?,?,?)", rows)
    db.commit()


def test_recency_model_beats_random_on_a_bursty_listener(tmp_path):
    db = history.connect(tmp_path / "h.db")
    seed_history(db)
    res = evaluate.evaluate_models(db, {"decay": evaluate.REPEAT_MODELS["decay 30d half-life (used)"],
                                        "random": evaluate.REPEAT_MODELS["random"]})
    assert res["decay"][2] >= 5 and res["decay"][1] > 2 * res["random"][1]


def test_gem_evaluation_runs_and_returns_per_model_scores(tmp_path):
    db = history.connect(tmp_path / "h.db")
    seed_history(db, seed=3)
    res = evaluate.evaluate_models(db, evaluate.GEM_MODELS, gems=True)
    assert set(res) <= set(evaluate.GEM_MODELS) and all(0 <= v[0] <= 1 and 0 <= v[1] <= 1 for v in res.values())


def test_artist_coverage_shares_are_nested(tmp_path):
    db = history.connect(tmp_path / "h.db")
    seed_history(db)
    c = evaluate.new_track_artist_coverage(db)
    a, b, c3 = c.values()
    assert 0 <= a <= b <= c3 <= 1


def test_ndcg_perfect_and_worst_ranking():
    hits = [True] * 5 + [False] * 95
    assert evaluate._ndcg(list(range(100, 0, -1)), hits, 10) == (1.0, 1.0)
    assert evaluate._ndcg(list(range(100)), hits, 10)[0] == 0.0


def test_hard_windows_priority_order():
    now = 1000 * DAY
    recent = np.array([now - 5 * DAY])
    mid = np.array([now - 60 * DAY])
    old = np.array([now - 400 * DAY] * 9)
    f = evaluate.hard_windows
    assert f(recent, now) > f(mid, now) > f(old, now)


def test_overlap_between_playlists():
    p1 = [{"file": "a"}, {"file": "b"}]
    p2 = [{"file": "b"}, {"file": "c"}]
    assert evaluate.overlap_between([p1, p2]) == (1 / 3, 3)
    assert evaluate.overlap_between([p1, p1]) == (1.0, 2)


def test_playlist_metrics_on_a_hand_built_playlist():
    tracks = [
        {"file": f"f{i}", "artist": f"A{i % 3}", "energy": 0.6 + 0.05 * i, "duration": 300,
         "source": "obsession" if i < 2 else "rec"}
        for i in range(12)
    ]
    audio = {t["file"]: np.array([i, 0.0]) for i, t in enumerate(tracks)}
    bpm = {t["file"]: 120.0 for t in tracks}
    m = evaluate.playlist_metrics(tracks, audio, bpm, swim_seconds=3600)
    assert m["artists"] == 3 and m["max_per_artist"] == 4 and m["same_artist_adjacent"] == 0
    assert m["mean_tempo_gap"] == 0 and m["longest_bucket_run"] == 10 and m["arc_gain"] > 0
