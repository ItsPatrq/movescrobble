import numpy as np

from movescrobble.dpp import greedy_dpp
from movescrobble.sequence import order, tempo_gap
from movescrobble.workouts import arc_target


def unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def test_dpp_prefers_the_diverse_set_over_near_duplicates():
    # 3 near-identical tracks (0,1,2) and 3 distinct ones; equal quality; room for 3
    V = unit([[1, 0, 0], [0.99, 0.1, 0], [0.98, 0, 0.1], [0, 1, 0], [0, 0, 1], [0.5, 0.5, 0.7]])
    q = np.ones(6)
    picks = set()
    for seed in range(20):
        sel = greedy_dpp(q, V, np.full(6, 200.0), 600, np.random.default_rng(seed), noise=0.1)
        assert len(sel) == 3
        picks.update(sel)
        assert len({0, 1, 2} & set(sel)) <= 1  # never two of the clones together
    assert picks >= {3, 4}


def test_dpp_respects_the_time_budget_and_never_repeats():
    V = unit(np.random.default_rng(0).normal(size=(30, 6)))
    dur = np.random.default_rng(1).uniform(120, 400, 30)
    sel = greedy_dpp(np.ones(30), V, dur, 1800, np.random.default_rng(2))
    assert len(set(sel)) == len(sel) and dur[sel].sum() <= 1800 and dur[sel].sum() > 1500


def test_dpp_quality_wins_when_diversity_is_equal():
    V = unit(np.eye(8))
    q = np.array([1, 1, 1, 1, 0.1, 0.1, 0.1, 0.1])
    sel = greedy_dpp(q, V, np.full(8, 100.0), 400, np.random.default_rng(0), noise=0.05)
    assert set(sel) == {0, 1, 2, 3}


def test_dpp_steers_towards_bucket_quotas():
    V = unit(np.eye(20))
    q = np.ones(20)
    buckets = np.array([0] * 15 + [1] * 5)  # bucket 1 is scarce but owed 50% of the time
    sel = greedy_dpp(q, V, np.full(20, 100.0), 800, np.random.default_rng(3), buckets=buckets,
                     quota_secs={0: 400.0, 1: 400.0}, noise=0.05)
    assert sum(1 for i in sel if buckets[i] == 1) >= 4


def test_dpp_artist_cap_is_hard():
    V = unit(np.eye(10))
    artists = np.array([0] * 8 + [1, 2])
    sel = greedy_dpp(np.ones(10), V, np.full(10, 100.0), 1000, np.random.default_rng(0), artists=artists, max_per_artist=2)
    assert sum(1 for i in sel if artists[i] == 0) == 2


def test_tempo_gap_is_octave_aware():
    assert tempo_gap(120, 120) == 0 and abs(tempo_gap(70, 140)) < 1e-9 and tempo_gap(0, 120) == 0
    assert tempo_gap(120, 170) > tempo_gap(120, 130)
    assert tempo_gap(120, 120 * 2**0.5) > 0.45  # a half-octave apart is the worst match


def test_arc_target_shape():
    lo, mid, hi = 0.6, 0.75, 0.9
    assert arc_target(0.0, lo, mid, hi) == lo and abs(arc_target(0.12, lo, mid, hi) - mid) < 1e-9
    assert all(mid - 1e-9 <= arc_target(p / 100, lo, mid, hi) <= hi + 1e-9 for p in range(13, 89))
    assert arc_target(0.99, lo, mid, hi) < mid and arc_target(1.5, lo, mid, hi) == mid


def test_order_improves_on_a_random_order():
    rng = np.random.default_rng(0)
    n = 24
    energy = rng.uniform(0.5, 0.95, n)
    seconds = np.full(n, 200.0)
    kw = dict(bpm=rng.uniform(90, 160, n), bright=rng.uniform(0.2, 0.8, n), artist=np.arange(n) % 8,
              bucket=np.arange(n) % 3, seconds=seconds, swim_seconds=3000.0)
    perm = order(energy, rng=np.random.default_rng(1), **kw)
    assert sorted(perm) == list(range(n))
    jump = lambda p: np.mean(np.abs(np.diff(energy[p])))  # noqa: E731
    assert jump(perm) < 0.7 * jump(list(range(n)))
    assert energy[perm[:2]].mean() < energy[perm[8:14]].mean()  # warm-up below the middle


def test_order_handles_tiny_inputs():
    one = np.array([0.8])
    assert order(one, one, one, np.array([0]), np.array([0]), np.array([200.0]), 3000.0, np.random.default_rng(0)) == [0]


def test_dpp_keeps_filling_when_every_track_is_a_duplicate():
    V = unit(np.tile([[1.0, 0.0, 0.0]], (12, 1)))  # identical sound, so the determinant collapses after one pick
    sel = greedy_dpp(np.ones(12), V, np.full(12, 100.0), 800, np.random.default_rng(0))
    assert len(sel) == 8 and len(set(sel)) == 8


def test_dpp_fill_mode_still_honours_the_artist_cap():
    V = unit(np.tile([[1.0, 0.0]], (10, 1)))
    artists = np.array([0] * 5 + [1] * 5)
    sel = greedy_dpp(np.ones(10), V, np.full(10, 100.0), 1000, np.random.default_rng(0), artists=artists, max_per_artist=3)
    assert len(sel) == 6 and sum(1 for i in sel if artists[i] == 0) == 3


def test_dpp_soft_cap_is_relaxed_only_when_nothing_else_fits():
    V = unit(np.eye(10))
    artists = np.array([0] * 8 + [1, 2])
    sel = greedy_dpp(np.ones(10), V, np.full(10, 100.0), 700, np.random.default_rng(0), artists=artists,
                     max_per_artist=2, relax_cap=True)
    assert len(sel) == 7 and sum(1 for i in sel if artists[i] == 0) == 5  # 2 + both others, then the cap gives way
    order_ = [artists[i] for i in sel]
    assert order_[:4].count(0) <= 2  # the cap held while there was an alternative
