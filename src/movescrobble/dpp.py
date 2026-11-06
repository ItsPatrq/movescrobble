"""Pick a diverse, relevant set with a determinantal point process (greedy MAP).

Kernel L = diag(q) S diag(q): q is each track's quality (taste x energy), S its similarity to the
others (audio features plus a same-artist term). The log-determinant gain of adding a track is its
quality minus how much it overlaps what is already chosen, so near-duplicates and repeated artists
are discouraged without hard caps. Greedy MAP with the incremental Cholesky update is Chen et al.,
"Fast Greedy MAP Inference for Determinantal Point Process to Improve Recommendation Diversity"
(NeurIPS 2018). Two additions for this use case:

* Gumbel noise on every step's gain, so the result is a different good set each run, not the one MAP set;
* bucket steering: a bonus for buckets that are below their share of the time budget, so the mix
  follows the quotas without being forced to.
"""
from __future__ import annotations

import numpy as np


def greedy_dpp(
    q: np.ndarray,
    V: np.ndarray,
    durations: np.ndarray,
    budget: float,
    rng: np.random.Generator,
    *,
    buckets: np.ndarray | None = None,
    quota_secs: dict[int, float] | None = None,
    artists: np.ndarray | None = None,
    max_per_artist: int | None = None,
    noise: float = 0.35,
    steer: float = 1.0,
    relax_cap: bool = False,
) -> list[int]:
    """Indices of the chosen tracks, in selection order. V holds unit-length feature rows."""
    n = len(q)
    d2 = q.astype(np.float64) ** 2  # diagonal of L (S_ii = 1)
    c = np.zeros((n, n))
    free = np.ones(n, dtype=bool)
    used: dict[int, float] = {}
    count: dict[int, int] = {}
    chosen: list[int] = []
    remaining = float(budget)
    quota_secs = quota_secs or {}

    for t in range(n):
        fits = free & (durations <= remaining + 1e-6)
        if max_per_artist and artists is not None:
            under = fits & np.array([count.get(int(a), 0) < max_per_artist for a in artists])
            fits = under if under.any() or not relax_cap else fits  # soft cap: only break it to fill the time
        ok = fits & (d2 > 1e-9)
        filling = not ok.any()
        if filling:
            # every remaining track duplicates what is chosen (the determinant collapsed): keep going by
            # quality and distance from the nearest chosen track, so the playlist still reaches its length
            ok = fits
            if not ok.any():
                break
            nearest = (V @ V[chosen].T).max(axis=1) if chosen else np.zeros(n)
            gain = np.log(np.maximum(q, 1e-9)) + np.log(1.05 - nearest)
        else:
            gain = np.log(np.maximum(d2, 1e-12))
        if buckets is not None and quota_secs:
            deficit = np.array([
                max(quota_secs.get(b, 0.0) - used.get(b, 0.0), 0.0) / max(quota_secs.get(b, 1.0), 1.0)
                for b in range(int(buckets.max()) + 1)
            ])
            gain = gain + steer * np.log(deficit[buckets] + 0.05)
        score = np.where(ok, gain + noise * rng.gumbel(size=n), -np.inf)
        j = int(np.argmax(score))
        if not filling:
            e = (q * q[j] * (V @ V[j]) - c[:, :t] @ c[j, :t]) / np.sqrt(d2[j])
            c[:, t] = e
            d2 = d2 - e * e
        d2[j] = 0.0
        free[j] = False
        chosen.append(j)
        remaining -= float(durations[j])
        if buckets is not None:
            used[int(buckets[j])] = used.get(int(buckets[j]), 0.0) + float(durations[j])
        if artists is not None:
            count[int(artists[j])] = count.get(int(artists[j]), 0) + 1
    return chosen
