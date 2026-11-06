"""Offline evaluation on your own Last.fm history (nothing is sent anywhere).

Time-split protocol: pick a cutoff date T, rank candidates using only plays before T, and check which
of them were really played in the next 30 days. Repeating this over many cutoffs and averaging gives a
stable number, because listening is bursty. Used to choose the affinity model in affinity.py.
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

DAY = 86400
K = 100


def _load(db):
    tr: dict[tuple[str, str], list[int]] = defaultdict(list)
    ar: dict[str, list[int]] = defaultdict(list)
    for r in db.execute("SELECT ts, artist, title FROM scrobbles ORDER BY ts"):
        tr[(r["artist"], r["title"])].append(r["ts"])
        ar[r["artist"]].append(r["ts"])
    return ({k: np.array(v, dtype=np.float64) for k, v in tr.items()},
            {k: np.array(v, dtype=np.float64) for k, v in ar.items()})


def _past(arr: np.ndarray, t: float) -> np.ndarray:
    return arr[: np.searchsorted(arr, t)]


def _played_after(arr: np.ndarray, t: float, days: int = 30) -> bool:
    return np.searchsorted(arr, t + days * DAY) > np.searchsorted(arr, t)


def decay(ts: np.ndarray, t: float, half_life: float) -> float:
    return float(np.exp(-((t - ts) / DAY) * math.log(2) / half_life).sum()) if len(ts) else 0.0


def actr(ts: np.ndarray, t: float, d: float) -> float:
    """ACT-R base-level activation (Reiter-Haas et al. use it for music re-listening)."""
    return math.log(float((((t - ts) / DAY + 0.1) ** -d).sum())) if len(ts) else -9.0


def hard_windows(ts: np.ndarray, t: float) -> float:
    """The first version of this tool: bucket priority by hard 30 / 90 day windows."""
    p30 = int((ts >= t - 30 * DAY).sum())
    p90 = int(((ts >= t - 90 * DAY) & (ts < t - 30 * DAY)).sum())
    if p30:
        return 3 + math.log1p(p30)
    return 2 + math.log1p(p90) if p90 else 1 + math.log1p(len(ts)) / 6


REPEAT_MODELS = {
    "decay 30d half-life (used)": lambda ts, t: decay(ts, t, 30),
    "decay 14d half-life": lambda ts, t: decay(ts, t, 14),
    "decay 60d half-life": lambda ts, t: decay(ts, t, 60),
    "ACT-R d=0.5": lambda ts, t: actr(ts, t, 0.5),
    "ACT-R d=0.8": lambda ts, t: actr(ts, t, 0.8),
    "hard 30/90-day windows (v1)": hard_windows,
    "random": lambda ts, t: ((len(ts) * 7919 + int(ts[-1]) * 31) % 10007) / 10007.0,
}
GEM_MODELS = {
    "decay 180d half-life (used)": lambda ts, t: decay(ts, t, 180),
    "decay 365d half-life": lambda ts, t: decay(ts, t, 365),
    "loyalty: distinct play-weeks": lambda ts, t: len(set((ts // (7 * DAY)).astype(int))),
    "total plays (v1)": lambda ts, t: len(ts),
    "random": lambda ts, t: ((len(ts) * 7919 + int(ts[-1]) * 31) % 10007) / 10007.0,
}


def _ndcg(scores: list[float], hits: list[bool], k: int) -> tuple[float, float]:
    order = np.argsort(-np.array(scores), kind="stable")[:k]
    h = np.array(hits, dtype=float)[order]
    n_hits = int(sum(hits))
    if not n_hits:
        return 0.0, 0.0
    ideal = (1 / np.log2(np.arange(min(k, n_hits)) + 2)).sum()
    return float(h.sum() / min(k, n_hits)), float((h / np.log2(np.arange(len(h)) + 2)).sum() / ideal)


def cutoffs(last_ts: float, first_ts: float, step_days: int = 18, span_days: int = 800) -> list[float]:
    return [last_ts - d * DAY for d in range(30, span_days, step_days) if last_ts - d * DAY > first_ts + 400 * DAY]


def evaluate_models(db, models: dict, *, gems: bool = False, k: int = K) -> dict[str, tuple[float, float, int]]:
    """Mean (recall-or-precision@k, NDCG@k, n_cutoffs) per model over many cutoffs.

    gems=False: candidates are tracks played in the 2 years before T (repeat listening).
    gems=True:  candidates are tracks with 4+ plays that were silent for 90+ days (comebacks).
    """
    tr, _ = _load(db)
    if not tr:
        return {}
    last = max(v[-1] for v in tr.values())
    first = min(v[0] for v in tr.values())
    out: dict[str, list[tuple[float, float]]] = {m: [] for m in models}
    for t in cutoffs(last, first):
        cand, hits = [], []
        for arr in tr.values():
            p = _past(arr, t)
            if not len(p):
                continue
            if gems:
                ok = len(p) >= 4 and 90 * DAY <= t - p[-1] <= 1500 * DAY
            else:
                ok = t - p[-1] <= 730 * DAY
            if ok:
                cand.append(p)
                hits.append(_played_after(arr, t))
        if len(cand) < 100 or sum(hits) < 5:
            continue
        for name, f in models.items():
            out[name].append(_ndcg([f(p, t) for p in cand], hits, 50 if gems else k))
    return {m: (float(np.mean([x[0] for x in v])), float(np.mean([x[1] for x in v])), len(v)) for m, v in out.items() if v}


def new_track_artist_coverage(db) -> dict[str, float]:
    """Of the tracks first played in a 30-day window, how many are by an artist you already played?"""
    tr, ar = _load(db)
    last = max(v[-1] for v in tr.values())
    first = min(v[0] for v in tr.values())
    rows = []
    for t in cutoffs(last, first):
        novel = [k for k, v in tr.items() if t <= v[0] < t + 30 * DAY]
        if not novel:
            continue
        pa = [_past(ar[k[0]], t) for k in novel]
        rows.append((
            np.mean([len(p) > 0 and p[-1] >= t - 30 * DAY for p in pa]),
            np.mean([len(p) > 0 and p[-1] >= t - 365 * DAY for p in pa]),
            np.mean([len(p) > 0 for p in pa]),
        ))
    a = np.array(rows)
    return {"artist played in previous 30 days": float(a[:, 0].mean()),
            "artist played in previous 365 days": float(a[:, 1].mean()),
            "artist played at any time before": float(a[:, 2].mean())}


def playlist_metrics(tracks: list[dict], audio: dict[str, np.ndarray], bpm: dict[str, float], swim_seconds: float = 3600) -> dict:
    """Properties of one generated playlist. `tracks` are manifest entries; `audio` maps file -> z-scored vector."""
    from collections import Counter

    from .sequence import tempo_gap

    files = [t["file"] for t in tracks]
    e = np.array([t.get("energy") or 0.0 for t in tracks])
    dur = np.array([t["duration"] for t in tracks], dtype=float)
    start = np.cumsum(dur) - dur
    arts = [t["artist"].casefold() for t in tracks]
    Z = np.array([audio[f] for f in files])
    n = len(tracks)
    warm, main = e[start < 420], e[(start >= 900) & (start < swim_seconds - 600)]
    run = best = 1
    for a, b in zip(tracks, tracks[1:], strict=False):
        run = run + 1 if a.get("source") == b.get("source") else 1
        best = max(best, run)
    return {
        "artists": len(set(arts)), "max_per_artist": max(Counter(arts).values()),
        "energy": float(e.mean()), "energy_in_swim": float(e[start < swim_seconds].mean()),
        "arc_gain": float(main.mean() - warm.mean()) if len(warm) and len(main) else 0.0,
        "mean_energy_jump": float(np.abs(np.diff(e)).mean()),
        "mean_tempo_gap": float(np.mean([tempo_gap(bpm[a], bpm[b]) for a, b in zip(files, files[1:], strict=False)])),
        "same_artist_adjacent": sum(1 for a, b in zip(arts, arts[1:], strict=False) if a == b),
        "longest_bucket_run": best,
        "audio_diversity": float(np.linalg.norm(Z[:, None] - Z[None], axis=2)[np.triu_indices(n, 1)].mean()),
    }


def overlap_between(playlists: list[list[dict]]) -> tuple[float, int]:
    """(mean Jaccard overlap of track sets between playlists, distinct tracks used): predictability."""
    import itertools

    sets = [{t["file"] for t in p} for p in playlists]
    pairs = list(itertools.combinations(sets, 2))
    return (float(np.mean([len(a & b) / len(a | b) for a, b in pairs])) if pairs else 1.0,
            len(set().union(*sets)) if sets else 0)
