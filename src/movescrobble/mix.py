"""Build a varied, energising session: choose with a DPP, then sequence as a workout.

1. Every pool track gets a quality q = taste relevance x energy (x a content bonus for tracks you
   have never played, based on how close they sound to what you are into), damped by how often you
   already swam to it.
2. A determinantal point process picks the set (see dpp.py): relevant, but spread out over artists and
   sound, with each bucket (recent obsession, mid, lost gems, fresh, discoveries) steered to its share.
3. An annealing sequencer orders it (see sequence.py): energy arc over the swim window, no jarring
   tempo/energy jumps, no artist back to back.
Different seeds give different sets, not just different shuffles.
"""
from __future__ import annotations

import numpy as np

from .dpp import greedy_dpp
from .energy import Features
from .generator import Candidate, Key
from .library import Track
from .planner import NEW_TO_YOU, PlannedTrack, dedupe, key_of
from .sequence import order
from .workouts import WORKOUTS

DEFAULT_QUOTAS = {"obsession": 0.40, "mid": 0.15, "fresh": 0.15, "lost": 0.10, "rec": 0.20}
BUCKET_ALIASES = {"playlist": "obsession", "top": "obsession", "loved": "obsession", "similar": "rec"}
UNKNOWN_TASTE_WEIGHT = 0.5  # only used when there is no taste pool at all
SKIP_DAMP, ARTIST_SKIP_DAMP = 0.5, 0.85  # quality factor per skip of the track / of any track by the artist
POWER_PULL, POWER_RANGE = 3.0, (0.75, 1.3)  # +5% swim speed to a track -> x1.15 quality, capped both ways
RETIRE_SKIPS = 3
ARTIST_WEIGHT = 1.0  # strength of the same-artist term in the similarity kernel (audio terms total 1.0)


def bucket_of(c: Candidate) -> str:
    for s in sorted(c.sources):
        b = BUCKET_ALIASES.get(s, s)
        if b in DEFAULT_QUOTAS:
            return b
    return "obsession"


def audio_matrix(features: list[Features]) -> np.ndarray:
    """z-scored audio descriptors, one row per track."""
    m = np.array([[f.energy, f.bpm / 200 if f.bpm > 0 else 0.6, (f.loudness_db + 30) / 30, f.pulse, f.bright, f.bass]
                  for f in features], dtype=np.float64)
    sd = m.std(axis=0)
    return (m - m.mean(axis=0)) / np.where(sd > 1e-9, sd, 1.0)


def kernel_vectors(audio: np.ndarray, artist_ids: np.ndarray) -> np.ndarray:
    """Unit rows whose dot products are the similarity S: audio closeness + same artist."""
    a = audio / max(np.sqrt((audio**2).sum(axis=1).mean()), 1e-9)  # rows of average norm 1
    onehot = np.zeros((len(artist_ids), int(artist_ids.max()) + 1))
    onehot[np.arange(len(artist_ids)), artist_ids] = np.sqrt(ARTIST_WEIGHT)
    v = np.hstack([a, onehot])
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)


def build_mix(
    library: list[Track],
    candidates: list[Candidate],
    feature_of,
    *,
    minutes: float = 120,
    swim_minutes: float = 60,
    seed: int | None = None,
    quotas: dict[str, float] | None = None,
    min_energy: float = 0.5,
    plays: dict[Key, int] | None = None,
    recent: set[Key] | None = None,
    max_per_artist: int = 4,
    variety: float = 2.5,
    workout: str = "steady",
    skips: dict[Key, int] | None = None,
    artist_skips: dict[str, int] | None = None,
    power: dict[Key, float] | None = None,
) -> list[PlannedTrack]:
    """`skips` / `artist_skips`: how often you skipped a track / an artist on a swim. Each skip makes it rarer;
    a track skipped RETIRE_SKIPS times is not picked again. `power`: how much faster you swim to a track
    (power.scores, from Garmin lengths); faster tracks are picked more often, slower ones less."""
    rng = np.random.default_rng(seed)
    plays, recent = plays or {}, recent or set()
    skips, artist_skips, power = skips or {}, artist_skips or {}, power or {}
    cmap = {(c.artist.casefold(), c.title.casefold()): c for c in candidates}
    quotas = dict(quotas or DEFAULT_QUOTAS)

    rows = []  # (track, bucket, relevance, features)
    stale = []  # recently swum: last resort, only to fill the buffer
    for t in dedupe(library):
        k = key_of(t)
        c = cmap.get(k)
        if cmap and c is None:
            continue  # a taste pool exists: only tracks it lists are eligible
        if skips.get(k, 0) >= RETIRE_SKIPS:
            continue
        f = feature_of(t)
        if f.energy < min_energy:
            continue
        item = (t, bucket_of(c) if c else "other", c.score if c else UNKNOWN_TASTE_WEIGHT, f)
        (stale if k in recent else rows).append(item)
    if not rows and not stale:
        return []
    if any(r[1] == "other" for r in rows):
        quotas = {"other": 1.0}

    def prepare(items):
        tracks = [r[0] for r in items]
        feats = [r[3] for r in items]
        audio = audio_matrix(feats)
        names = {a: i for i, a in enumerate(dict.fromkeys(t.artist.casefold() for t in tracks))}
        artist_ids = np.array([names[t.artist.casefold()] for t in tracks])
        rel = np.array([r[2] for r in items])
        energy = np.array([f.energy for f in feats])
        buckets = [r[1] for r in items]
        # content bonus: unplayed tracks that sound like what you are into rank higher
        hot = np.array([b in ("obsession", "mid") for b in buckets])
        if hot.sum() >= 3:
            taste = (audio[hot] * rel[hot, None]).sum(axis=0)
            taste /= max(np.linalg.norm(taste), 1e-9)
            unit = audio / np.maximum(np.linalg.norm(audio, axis=1, keepdims=True), 1e-9)
            sim = unit @ taste
            new = np.array([b in NEW_TO_YOU for b in buckets])
            rel = np.where(new, rel * (1.0 + 0.4 * sim), rel)
        swum = np.array([plays.get(key_of(t), 0) for t in tracks])
        skipped = np.array([SKIP_DAMP ** skips.get(key_of(t), 0) * ARTIST_SKIP_DAMP ** artist_skips.get(t.artist.casefold(), 0)
                            for t in tracks])
        faster = np.clip([1 + POWER_PULL * power.get(key_of(t), 0.0) for t in tracks], *POWER_RANGE)
        q = (np.clip(rel, 0.1, 1.5) ** 0.8 * energy ** WORKOUTS[workout].energy_pull / np.sqrt(1 + swum) * skipped
             * faster)
        return tracks, feats, audio, artist_ids, q, energy, buckets

    budget = minutes * 60
    pool_items = rows + stale if len(rows) < 2 else rows
    tracks, feats, audio, artist_ids, q, energy, buckets = prepare(pool_items)
    dur = np.array([t.duration for t in tracks])
    names = sorted(set(buckets))
    bidx = np.array([names.index(b) for b in buckets])
    live = {b: w for b, w in quotas.items() if b in names}
    total_w = sum(live.values()) or 1.0
    quota_secs = {names.index(b): budget * w / total_w for b, w in live.items()}
    V = kernel_vectors(audio, artist_ids)
    chosen_idx = greedy_dpp(q, V, dur, budget, rng, buckets=bidx, quota_secs=quota_secs, artists=artist_ids,
                            max_per_artist=max_per_artist, noise=variety, relax_cap=True,
                            steer=1.0 + 1.5 * variety)  # quotas must beat the noise

    # recently swum tracks only if the fresh ones cannot fill the playlist
    used = float(dur[chosen_idx].sum()) if chosen_idx else 0.0
    if stale and pool_items is rows and used < budget - 400:
        s_tracks, s_feats, s_audio, s_art, s_q, s_energy, s_buckets = prepare(stale)
        order_ = rng.permutation(len(stale))
        extra = []
        for i in order_:
            if used + s_tracks[i].duration <= budget:
                extra.append(i)
                used += s_tracks[i].duration
        tracks = tracks + [s_tracks[i] for i in extra]
        feats = feats + [s_feats[i] for i in extra]
        energy = np.concatenate([energy, s_energy[extra]]) if extra else energy
        buckets = buckets + [s_buckets[i] for i in extra]
        base = len(dur)
        dur = np.concatenate([dur, np.array([s_tracks[i].duration for i in extra])]) if extra else dur
        chosen_idx = chosen_idx + list(range(base, base + len(extra)))
        artist_ids = np.concatenate([artist_ids, 10_000 + np.array([hash(s_tracks[i].artist.casefold()) % 9999
                                                                     for i in extra], dtype=int)]) if extra else artist_ids

    sel = np.array(chosen_idx, dtype=int)
    bk_names = sorted(set(buckets))
    seq = order(
        energy[sel], np.array([feats[i].bpm for i in sel]), np.array([feats[i].bright for i in sel]),
        artist_ids[sel], np.array([bk_names.index(buckets[i]) for i in sel]), dur[sel],
        swim_minutes * 60, rng, workout=workout,
    )
    return [PlannedTrack(n + 1, tracks[sel[i]], buckets[sel[i]], round(float(energy[sel[i]]), 2))
            for n, i in enumerate(seq)]
