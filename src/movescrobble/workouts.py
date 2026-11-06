"""Workouts: the energy curve a playlist follows over the swim. Pure Python (no numpy): the CLI imports it."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


def arc_target(p: float, lo: float, mid: float, hi: float) -> float:
    """Target energy at fraction p (0..1) of the swim; >1 holds the plateau."""
    if p >= 1.0:
        return mid
    if p < 0.12:
        return lo + (mid - lo) * p / 0.12
    if p < 0.9:
        # two gentle surges between the plateau and the peak, continuous with warm-up and taper
        return mid + (hi - mid) * (0.5 - 0.5 * math.cos(2 * math.pi * 2 * (p - 0.12) / 0.78))
    return mid - (mid - lo) * 0.5 * (p - 0.9) / 0.1


def intervals_target(p: float, lo: float, mid: float, hi: float) -> float:
    """Warm-up, then alternating hard / easy blocks (3 of each, ~2 tracks long on a 60 min swim), then a cool-down."""
    if p >= 1.0:
        return mid
    if p < 0.1:
        return lo + (mid - lo) * p / 0.1
    if p >= 0.92:
        return lo
    return hi if int((p - 0.1) / 0.82 * 6) % 2 == 0 else lo


def easy_target(p: float, lo: float, mid: float, hi: float) -> float:
    """Recovery swim: an even, low plateau, no surges."""
    return lo + (mid - lo) * 0.4 if p < 1.0 else lo


def pyramid_target(p: float, lo: float, mid: float, hi: float) -> float:
    """Build steadily to the peak at half-way, then come back down."""
    if p >= 1.0:
        return mid
    return lo + (hi - lo) * (1 - abs(2 * p - 1))


@dataclass(frozen=True)
class Workout:
    target: Callable[[float, float, float, float], float]  # (fraction of swim, lo, mid, hi) -> target energy
    help: str
    smoothness: float = 1.0  # scales the penalty on energy jumps between neighbours (intervals want the jumps)
    energy_pull: float = 1.5  # exponent on energy when choosing the set (an easy swim does not chase it)


DEFAULT = "steady"
WORKOUTS = {
    "steady": Workout(arc_target, "warm-up, two gentle surges, taper"),
    "intervals": Workout(intervals_target, "alternating hard / easy blocks", smoothness=0.25),
    "easy": Workout(easy_target, "even and low, for recovery", energy_pull=0.0),
    "pyramid": Workout(pyramid_target, "build to a peak half-way, then down"),
}
