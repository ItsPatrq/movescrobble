"""Order the chosen tracks as a workout: warm-up, driving middle, a gentle taper.

Simulated annealing over the order, minimising
* energy and tempo jumps between neighbours (tempo compared modulo octaves: 70 and 140 BPM match),
* the same artist back to back and long runs of one bucket,
* distance from an energy arc over the swim window (the first `swim_seconds`; anything after that, the
  buffer, just holds the plateau).
Research on exercise music (Karageorghis) suggests lower tempo to warm up, 120-140+ BPM for the main
work, easing off at the end; since every track here is already high energy, the arc is shallow.
"""
from __future__ import annotations

import math

import numpy as np

from .workouts import WORKOUTS

W_ENERGY, W_TEMPO, W_BRIGHT, W_ARTIST, W_RUN, W_ARC = 1.5, 1.2, 0.3, 2.0, 0.6, 2.0


def tempo_gap(a: float, b: float) -> float:
    """0 = same tempo or an exact double/half; 0.5 = worst case. Unknown tempo costs nothing."""
    if a <= 0 or b <= 0:
        return 0.0
    d = abs(math.log2(a / b))
    return min(d % 1.0, 1.0 - d % 1.0)


def order(
    energy: np.ndarray,
    bpm: np.ndarray,
    bright: np.ndarray,
    artist: np.ndarray,
    bucket: np.ndarray,
    seconds: np.ndarray,
    swim_seconds: float,
    rng: np.random.Generator,
    iters: int = 12000,
    workout: str = "steady",
) -> list[int]:
    n = len(energy)
    if n < 3:
        return list(range(n))
    lo, mid, hi = (float(np.percentile(energy, p)) for p in (25, 60, 90))
    shape = WORKOUTS[workout].target
    w_energy = W_ENERGY * WORKOUTS[workout].smoothness
    e, b, br, ar, bk, sec = (x.tolist() for x in (energy, bpm, bright, artist, bucket, seconds))
    mean_sec = float(np.mean(seconds))

    def cost(perm: list[int]) -> float:
        total, t = 0.0, 0.0
        for pos, i in enumerate(perm):
            mid_t = t + sec[i] / 2
            target = shape(mid_t / swim_seconds, lo, mid, hi) if swim_seconds else mid
            total += W_ARC * (e[i] - target) ** 2 * sec[i] / mean_sec
            t += sec[i]
            if pos:
                j = perm[pos - 1]
                total += w_energy * abs(e[i] - e[j]) + W_TEMPO * tempo_gap(b[i], b[j])
                total += W_BRIGHT * abs(br[i] - br[j]) + (W_ARTIST if ar[i] == ar[j] else 0.0)
                if pos > 1 and bk[i] == bk[j] == bk[perm[pos - 2]]:
                    total += W_RUN
        return total

    perm = rng.permutation(n).tolist()
    cur = cost(perm)
    best, best_cost = perm[:], cur
    for k in range(iters):
        temp = 1.0 * (0.005 / 1.0) ** (k / iters)
        new = perm[:]
        a, c = int(rng.integers(n)), int(rng.integers(n))
        if a == c:
            continue
        if rng.random() < 0.5:
            new[a], new[c] = new[c], new[a]
        else:
            new.insert(c, new.pop(a))
        nc = cost(new)
        if nc < cur or rng.random() < math.exp((cur - nc) / temp):
            perm, cur = new, nc
            if cur < best_cost:
                best, best_cost = perm[:], cur
    return best
