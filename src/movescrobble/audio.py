"""ffmpeg re-encodes for the headphones: faster podcasts without long pauses, and music at one loudness.

The headphones have no speed control and no volume levelling, and bone conduction underwater makes quiet
masters hard to hear, so both are baked into the files that are copied over. Originals are never touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .config import config_dir

CACHE_DIR = "loudness-cache"
CACHE_DAYS = 60  # normalised copies unused for this long are deleted
TOLERANCE_LU = 1.0  # already this close to the target: copy the original, no re-encode
LOUDNORM = "loudnorm=I={i}:TP=-1.0:LRA=11"


def ffmpeg(*args: str) -> str:
    """Run ffmpeg; returns stderr (where loudnorm prints its measurement)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else r.returncode}")
    return r.stderr


def _into(dest: Path, *args: str) -> None:
    """Encode to `dest` via a temporary file, so a half-written file never looks complete."""
    tmp = dest.with_name(dest.name + ".part.mp3")
    try:
        ffmpeg(*args, str(tmp))
        tmp.rename(dest)
    finally:
        tmp.unlink(missing_ok=True)


# --- podcasts -------------------------------------------------------------------------------------

def podcast_filter(speed: float, trim_silence: bool) -> str | None:
    parts = []
    if trim_silence:  # pauses over 0.6 s shrink to 0.3 s; speech itself is untouched
        parts.append("silenceremove=stop_periods=-1:stop_duration=0.6:stop_threshold=-40dB:stop_silence=0.3")
    if speed and abs(speed - 1) > 0.01:
        parts.append(f"atempo={speed:.2f}")  # pitch-preserving
    return ",".join(parts) or None


def process_podcast(src: Path, dest: Path, speed: float, trim_silence: bool) -> None:
    flt = podcast_filter(speed, trim_silence)
    if not flt:
        shutil.copyfile(src, dest)
        return
    _into(dest, "-i", str(src), "-vn", "-af", flt, "-c:a", "libmp3lame", "-q:a", "4", "-map_metadata", "0")


# --- music loudness -------------------------------------------------------------------------------

def measure(src: Path) -> dict:
    """loudnorm's first pass: integrated loudness, true peak, range and threshold of the whole file.
    These describe the file alone (not the target), so one measurement serves every target."""
    err = ffmpeg("-i", str(src), "-vn", "-af", LOUDNORM.format(i=-14) + ":print_format=json", "-f", "null", "-")
    return json.loads(err[err.rindex("{"):err.rindex("}") + 1])


def _file_key(src: Path) -> str:
    st = src.stat()
    return hashlib.sha1(f"{src}|{st.st_mtime}|{st.st_size}".encode()).hexdigest()[:20]


def _touch(f: Path) -> Path:
    os.utime(f)  # keeps it out of prune()
    return f


def normalised(src: Path, target: float, cache: Path | None = None) -> Path:
    """A copy of `src` at `target` LUFS (two-pass, linear gain: no pumping), cached; `src` itself if already close.

    The measurement is cached per file and the levelled copy per file + target, so changing the target costs one
    encode per track, not a re-measure. Only MP3s are re-encoded (the staged name keeps its extension).
    """
    if src.suffix.lower() != ".mp3":
        return src
    cache = cache or config_dir() / CACHE_DIR
    cache.mkdir(parents=True, exist_ok=True)
    key = _file_key(src)
    out = cache / f"{key}_{target:g}.mp3"
    if out.exists():
        return _touch(out)
    measured = cache / f"{key}.json"
    if measured.exists():
        m = json.loads(_touch(measured).read_text())
    else:
        m = measure(src)
        measured.write_text(json.dumps(m))
    if abs(float(m["input_i"]) - target) <= TOLERANCE_LU and float(m["input_tp"]) <= -1.0:
        return src
    flt = (LOUDNORM.format(i=target) + f":measured_I={m['input_i']}:measured_TP={m['input_tp']}"
           f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:linear=true")
    _into(out, "-i", str(src), "-map", "0:a", "-af", flt, "-ar", "44100",  # loudnorm upsamples to 192 kHz otherwise
          "-c:a", "libmp3lame", "-q:a", "0", "-map_metadata", "0", "-id3v2_version", "3")
    return out


def leveller(target: float | None, cache: Path | None = None):
    """src -> the file to copy to the headphones: levelled to `target`, or `src` itself when off or unreadable."""
    if target is None:
        return lambda src: src

    def level(src: Path) -> Path:
        try:
            return normalised(src, target, cache)
        except Exception:  # odd/corrupt file: an un-levelled track beats a missing one
            return src
    return level


def prune(cache: Path | None = None, days: float = CACHE_DAYS) -> int:
    """Delete cache files unused for `days`; runs at most once a day."""
    cache = cache or config_dir() / CACHE_DIR
    stamp = cache / ".pruned"
    if not cache.is_dir() or (stamp.exists() and time.time() - stamp.stat().st_mtime < 86400):
        return 0
    cutoff, n = time.time() - days * 86400, 0
    for f in cache.iterdir():
        if f != stamp and f.stat().st_mtime < cutoff:
            f.unlink()
            n += 1
    stamp.touch()
    return n
