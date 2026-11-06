"""Estimate how energising a track is from the audio itself (numpy + ffmpeg, cached).

energy = mean of tempo, loudness, onset density and pulse clarity, each mapped to 0..1 with ranges
calibrated on a real library (modern masters sit around -5..-9 dB RMS, so loudness only separates
the quiet tail). A BPM tag, when present, overrides the estimated tempo. Ballads land below ~0.45.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SR = 11025
HOP = 256
WIN = 1024
CLIP_SECONDS = 90  # analysed excerpt, taken from the middle to skip intros/outros


@dataclass(frozen=True)
class Features:
    bpm: float
    loudness_db: float
    onset: float
    pulse: float
    energy: float
    bright: float = 0.5  # spectral centroid, 0 (dark) .. 1 (bright)
    bass: float = 0.25  # share of spectral energy below 200 Hz


def decode(path: Path, duration: float, run: Callable = subprocess.run) -> np.ndarray:
    start = max(0.0, duration / 2 - CLIP_SECONDS / 2)
    proc = run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.1f}", "-t", str(CLIP_SECONDS), "-i", str(path),
         "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(proc.stdout, dtype=np.float32)


def spectrogram(x: np.ndarray) -> np.ndarray:
    n = 1 + (len(x) - WIN) // HOP
    if n < 8:
        return np.zeros((0, WIN // 2 + 1))
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    return np.abs(np.fft.rfft(x[idx] * np.hanning(WIN), axis=1))


def onset_from_mag(mag: np.ndarray) -> np.ndarray:
    """Positive spectral flux of the log-magnitude spectrogram."""
    if len(mag) < 8:
        return np.zeros(1)
    return np.maximum(np.diff(np.log1p(100 * mag), axis=0), 0).sum(axis=1)


def onset_envelope(x: np.ndarray) -> np.ndarray:
    return onset_from_mag(spectrogram(x))


def timbre(mag: np.ndarray) -> tuple[float, float]:
    """(brightness 0..1, bass share) averaged over the clip."""
    if len(mag) == 0:
        return 0.5, 0.25
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    total = mag.sum() or 1.0
    centroid = float((mag * freqs).sum() / total)
    return _ramp(centroid, 500, 3500), float(mag[:, freqs < 200].sum() / total)


def estimate_bpm(env: np.ndarray, lo: float = 70, hi: float = 180) -> tuple[float, float]:
    """Return (bpm, pulse clarity): the strongest autocorrelation peak in the tempo range."""
    if len(env) < 64 or env.std() == 0:
        return 0.0, 0.0
    e = env - env.mean()
    ac = np.correlate(e, e, "full")[len(e) - 1 :]
    ac = ac / ac[0]
    fps = SR / HOP
    lags = np.arange(int(fps * 60 / hi), int(fps * 60 / lo) + 1)
    lags = lags[lags < len(ac) - 1]
    if len(lags) == 0:
        return 0.0, 0.0
    # mild log-normal prior around 120 BPM to avoid half/double-time picks
    prior = np.exp(-0.5 * (np.log2(60 * fps / lags / 120) / 0.6) ** 2)
    i = int(np.argmax(ac[lags] * prior))
    lag = float(lags[i])
    a, b, c = ac[lags[i] - 1], ac[lags[i]], ac[lags[i] + 1]
    denom = a - 2 * b + c
    if denom != 0:  # parabolic interpolation for sub-frame resolution
        lag += 0.5 * (a - c) / denom
    return 60 * fps / lag, float(ac[lags[i]])


def _ramp(v: float, lo: float, hi: float) -> float:
    return float(min(1.0, max(0.0, (v - lo) / (hi - lo))))


def tempo_score(bpm: float) -> float:
    return 0.0 if bpm <= 0 else _ramp(bpm, 85, 135)


def analyse_samples(x: np.ndarray, tag_bpm: float | None = None) -> Features:
    if len(x) < WIN * 8:
        return Features(0.0, -60.0, 0.0, 0.0, 0.0)
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    loud = 20 * np.log10(max(rms, 1e-6))
    mag = spectrogram(x)
    env = onset_from_mag(mag)
    bright, bass = timbre(mag)
    est_bpm, pulse = estimate_bpm(env)
    bpm = tag_bpm or est_bpm
    onset = _ramp(float(env.mean()), 130, 180)
    energy = (
        tempo_score(bpm) + _ramp(loud, -20, -6) + onset + _ramp(pulse, 0.2, 0.7)
    ) / 4
    return Features(round(bpm, 1), round(loud, 1), round(float(env.mean()), 1), round(pulse, 2), round(energy, 3),
                    round(bright, 3), round(bass, 3))


def analyse_file(path: Path, duration: float, tag_bpm: float | None = None, run: Callable = subprocess.run) -> Features:
    return analyse_samples(decode(path, duration, run), tag_bpm)


def cached_features(db, track, analyse: Callable[..., Features] = analyse_file) -> Features:
    """Per-file cache keyed on path + mtime; re-analyses only changed files."""
    path = Path(track.path)
    mtime = path.stat().st_mtime
    row = db.execute("SELECT * FROM features_v3 WHERE path=? AND mtime=?", (str(path), mtime)).fetchone()
    if row:
        return Features(row["bpm"], row["loudness"], row["onset"], row["pulse"], row["energy"], row["bright"], row["bass"])
    f = analyse(path, track.duration, track.bpm)
    db.execute("INSERT OR REPLACE INTO features_v3 VALUES(?,?,?,?,?,?,?,?,?)",
        (str(path), mtime, f.bpm, f.loudness_db, f.onset, f.pulse, f.energy, f.bright, f.bass),
    )
    db.commit()
    return f
