from collections import Counter
from pathlib import Path

import numpy as np

from movescrobble import energy, history
from movescrobble.energy import Features
from movescrobble.generator import Candidate
from movescrobble.library import Track
from movescrobble.mix import DEFAULT_QUOTAS, build_mix


def T(i, artist=None, dur=200):
    return Track(Path(f"t{i}.mp3"), artist or f"Artist{i}", f"Song{i}", "", dur)


def feats(table=None, bpm=120):
    table = table or {}
    return lambda t: Features(bpm, -7, 150, 0.6, table.get(t.title, 0.8), 0.5, 0.25)


def cand(i, bucket, score=0.6):
    return Candidate(f"Artist{i}", f"Song{i}", score, {bucket})


def pool(n_per=12):
    """5 buckets x n_per tracks, every track its own artist."""
    lib, cands = [], []
    for b_i, b in enumerate(DEFAULT_QUOTAS):
        for j in range(n_per):
            i = b_i * 100 + j
            lib.append(T(i))
            cands.append(cand(i, b))
    return lib, cands


def bucket_secs(plan):
    out = Counter()
    for p in plan:
        out[p.source] += p.track.duration
    return out


def test_slow_tracks_are_dropped():
    lib = [T(i) for i in range(30)]
    table = {f"Song{i}": 0.2 for i in range(10)}  # ballads
    plan = build_mix(lib, [], feats(table), minutes=60, seed=1, min_energy=0.45)
    assert plan and all(p.energy >= 0.45 for p in plan)
    assert not {f"Song{i}" for i in range(10)} & {p.track.title for p in plan}


def test_session_is_about_two_hours_and_follows_the_quotas():
    lib, cands = pool()
    plan = build_mix(lib, cands, feats(), minutes=120, seed=1)
    assert 6800 <= sum(p.track.duration for p in plan) <= 7200
    secs = bucket_secs(plan)
    assert set(secs) == set(DEFAULT_QUOTAS)
    total = sum(secs.values())
    for b, w in DEFAULT_QUOTAS.items():  # steered towards the shares, within a track or two
        assert abs(secs[b] / total - w) < 0.12, (b, secs[b] / total)


def test_quotas_are_configurable():
    lib, cands = pool(40)
    plan = build_mix(lib, cands, feats(), minutes=120, seed=1, quotas={"lost": 1.0})
    assert bucket_secs(plan)["lost"] >= 0.5 * sum(p.track.duration for p in plan)


def test_empty_bucket_hands_its_time_to_the_others():
    lib, cands = pool()
    cands = [c for c in cands if "lost" not in c.sources]  # no lost gems available
    plan = build_mix(lib, cands, feats(), minutes=120, seed=1)
    assert "lost" not in bucket_secs(plan) and sum(p.track.duration for p in plan) >= 6800


def test_artists_are_spread_out_and_capped():
    lib = [Track(Path(f"a{i}.mp3"), "Same Artist", f"S{i}", "", 200) for i in range(20)]
    lib += [T(100 + i) for i in range(30)]
    cands = [Candidate(t.artist, t.title, 0.6, {"obsession"}) for t in lib]
    plan = build_mix(lib, cands, feats(), minutes=60, seed=1)
    assert Counter(p.track.artist for p in plan)["Same Artist"] <= 2  # the DPP penalises repeats by itself
    only = [x for x in lib if x.artist == "Same Artist"]
    plan = build_mix(only, [Candidate(t.artist, t.title, 0.6, {"obsession"}) for t in only], feats(), minutes=60, seed=1)
    assert sum(p.track.duration for p in plan) >= 3400  # the cap is soft: relaxed when nothing else is left


def test_different_seeds_give_different_playlists_and_same_seed_repeats():
    lib, cands = pool(20)
    runs = [tuple(p.track.title for p in build_mix(lib, cands, feats(), minutes=120, seed=s)) for s in range(6)]
    assert len(set(runs)) == 6
    assert runs[0] == tuple(p.track.title for p in build_mix(lib, cands, feats(), minutes=120, seed=0))
    sets = [set(r) for r in runs]
    assert len(sets[0] & sets[1]) / len(sets[0] | sets[1]) < 0.7  # different songs, not the same ones reshuffled


def test_no_long_runs_of_one_bucket_and_no_adjacent_artist():
    lib, cands = pool(20)
    for seed in range(8):
        plan = build_mix(lib, cands, feats(), minutes=120, seed=seed)
        src = [p.source for p in plan]
        assert all(len(set(src[i:i + 4])) > 1 for i in range(len(src) - 3))  # never 4 of a kind in a row
        assert all(a.track.artist != b.track.artist for a, b in zip(plan, plan[1:], strict=False))


def test_energy_follows_a_warmup_arc_over_the_swim_window():
    lib = [T(i) for i in range(40)]
    table = {f"Song{i}": 0.55 + 0.4 * (i / 39) for i in range(40)}  # energies from 0.55 to 0.95
    cands = [cand(i, "obsession") for i in range(40)]
    gains = []
    for seed in range(6):
        plan = build_mix(lib, cands, feats(table), minutes=120, swim_minutes=60, seed=seed)
        t, e0, e1 = 0.0, [], []
        for p in plan:
            (e0 if t < 420 else e1 if 900 <= t < 3000 else []).append(p.energy)
            t += p.track.duration
        gains.append(np.mean(e1) - np.mean(e0))
    assert np.mean(gains) > 0.05  # warm-up is calmer than the middle of the swim


def test_neighbouring_tracks_have_similar_energy():
    lib = [T(i) for i in range(40)]
    table = {f"Song{i}": 0.5 + 0.45 * ((i * 7) % 40) / 39 for i in range(40)}  # scrambled energies
    cands = [cand(i, "obsession") for i in range(40)]
    plan = build_mix(lib, cands, feats(table), minutes=120, seed=2)
    mean_jump = np.mean([abs(a.energy - b.energy) for a, b in zip(plan, plan[1:], strict=False)])
    energies = [p.energy for p in plan]
    random_jump = np.mean(np.abs(np.subtract.outer(energies, energies))[np.triu_indices(len(plan), 1)])
    assert mean_jump < 0.6 * random_jump


def test_high_energy_favoured_in_selection():
    lib = [T(i, dur=600) for i in range(20)]
    table = {f"Song{i}": (0.95 if i < 6 else 0.5) for i in range(20)}
    hits = 0
    for seed in range(20):
        plan = build_mix(lib, [], feats(table), minutes=60, seed=seed, variety=0.35)
        hits += sum(1 for p in plan if int(p.track.title[4:]) < 6)
    assert hits / 20 > 3.0  # 6 of 20 picked; unweighted would average 1.8 of the 6 energetic


def test_recent_tracks_excluded_unless_needed_for_the_buffer():
    lib = [T(i) for i in range(30)]
    recent = {(f"artist{i}", f"song{i}") for i in range(10)}
    c = build_mix(lib, [], feats(), minutes=60, seed=5, recent=recent)
    assert not {p.track.title.casefold() for p in c} & {k[1] for k in recent}
    small = [T(i) for i in range(20)]  # 20 x 200s = 4000s, 10 of them recent
    plan = build_mix(small, [], feats(), minutes=120, seed=1, recent={(f"artist{i}", f"song{i}") for i in range(10)})
    assert sum(p.track.duration for p in plan) == 4000
    assert len([p for p in plan if int(p.track.title[4:]) >= 10]) == 10


def test_with_taste_pool_unlisted_tracks_are_not_eligible():
    lib = [T(i) for i in range(30)]
    cands = [cand(i, "obsession") for i in range(10)]
    analysed = []
    plan = build_mix(lib, cands, lambda t: (analysed.append(t) or Features(120, -7, 150, 0.6, 0.8)), minutes=600, seed=1)
    assert {p.track.title for p in plan} == {f"Song{i}" for i in range(10)}
    assert len(analysed) == 10  # no audio analysis wasted on tracks that can never be picked


def test_legacy_source_names_map_to_buckets():
    lib = [T(0), T(1)]
    cands = [Candidate("Artist0", "Song0", 0.6, {"playlist", "loved"}), Candidate("Artist1", "Song1", 0.6, {"similar"})]
    plan = build_mix(lib, cands, feats(), minutes=600, seed=1)
    assert {p.track.title: p.source for p in plan} == {"Song0": "obsession", "Song1": "rec"}


def test_unplayed_tracks_that_sound_like_your_taste_are_preferred():
    lib = [T(i) for i in range(40)]
    # obsession tracks are high-energy; unplayed ones either sound alike (high energy/bpm) or not
    def f(t):
        i = int(t.title[4:])
        return Features(150 if i < 25 or i % 2 == 0 else 80, -6, 160, 0.7, 0.9 if i < 25 or i % 2 == 0 else 0.55, 0.7, 0.3)
    cands = [cand(i, "obsession" if i < 25 else "fresh", 0.7) for i in range(40)]
    similar_share = []
    for seed in range(15):
        plan = build_mix(lib, cands, f, minutes=30, seed=seed, quotas={"fresh": 1.0}, variety=0.35)
        fresh = [int(p.track.title[4:]) for p in plan if p.source == "fresh"]
        similar_share.append(np.mean([i % 2 == 0 for i in fresh]))
    assert np.mean(similar_share) > 0.7


# ---- audio energy ----
def click_train(bpm, seconds=60, amp=0.8):
    x = np.zeros(int(energy.SR * seconds), dtype=np.float32)
    step = int(energy.SR * 60 / bpm)
    for i in range(0, len(x) - 200, step):
        x[i : i + 200] = amp * np.random.default_rng(i).uniform(-1, 1, 200)
    return x


def test_tempo_estimate_and_pulse_on_click_train():
    env = energy.onset_envelope(click_train(120))
    bpm, pulse = energy.estimate_bpm(env)
    assert abs(bpm - 120) < 3 and pulse > 0.5


def test_quiet_sparse_audio_scores_lower_than_loud_driving():
    rng = np.random.default_rng(0)
    t = np.arange(energy.SR * 60) / energy.SR
    pad = (0.02 * np.sin(2 * np.pi * 220 * t) + 0.002 * rng.standard_normal(len(t))).astype(np.float32)
    driving = np.clip(click_train(130, amp=0.9) + 0.2 * rng.standard_normal(len(t)).astype(np.float32), -1, 1)
    assert energy.analyse_samples(pad).energy < 0.35 < 0.5 < energy.analyse_samples(driving).energy


def test_tag_bpm_overrides_estimate():
    f = energy.analyse_samples(click_train(120), tag_bpm=90.0)
    assert f.bpm == 90.0


def test_feature_cache_hits_until_file_changes(tmp_path):
    db = history.connect(tmp_path / "h.db")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    track = Track(f, "A", "T", "", 100)
    calls = []

    def fake(path, dur, bpm):
        calls.append(1)
        return Features(100, -8, 150, 0.5, 0.6)

    energy.cached_features(db, track, fake)
    energy.cached_features(db, track, fake)
    assert len(calls) == 1
    import os

    os.utime(f, (1, 1))
    energy.cached_features(db, track, fake)
    assert len(calls) == 2
