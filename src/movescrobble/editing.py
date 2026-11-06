"""Edit the playlist that is staged on the headphones (add / remove / move tracks).

Filenames carry the play order (`001 - name.mp3`), so inserting or removing a track renames the ones
after it on the device. The session manifest in the database is updated to match, so scrobbling stays right.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import audio, history, ytmirror
from .config import Config
from .library import Track
from .planner import staged_name


def _session(db):
    row = history.pending_session(db)
    if row is None:
        raise ValueError("no staged session: reroll first")
    return row, json.loads(row["manifest"])


def _apply(cfg: Config, db, row, manifest: dict, tracks: list[dict], copy: dict[str, Path] | None = None) -> dict:
    """Renumber `tracks`, make the device folder match (rename survivors, copy new files, drop the rest)."""
    if not cfg.device_path.is_dir():
        raise RuntimeError(f"{cfg.device_name} is not connected")
    stage = cfg.stage_path
    stage.mkdir(parents=True, exist_ok=True)
    old = {t["staged"] for t in manifest["tracks"]}
    # phase 1: park every existing staged file under a temporary name (no clashes while renumbering)
    parked: dict[str, Path] = {}
    for i, name in enumerate(sorted(old)):
        f = stage / name
        if f.exists():
            tmp = stage / f".swimtmp-{i:03d}"
            f.rename(tmp)
            parked[name] = tmp
    # phase 2: final names
    for pos, t in enumerate(tracks, 1):
        final = staged_name(pos, t["file"])
        src = parked.pop(t["staged"], None)
        if src is not None:
            src.rename(stage / final)
        elif copy and t["file"] in copy:
            shutil.copy2(audio.leveller(cfg.loudness)(copy[t["file"]]), stage / final)
        t["position"], t["staged"] = pos, final
    for leftover in parked.values():  # removed tracks
        leftover.unlink(missing_ok=True)
    manifest["tracks"] = tracks
    db.execute("UPDATE sessions SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), row["id"]))
    db.commit()
    ytmirror.update(db, cfg, manifest)
    return manifest


def add_track(cfg: Config, db, track: Track, position: int, energy: float | None = None) -> dict:
    """Insert `track` at 1-based `position` (clamped) of the staged playlist."""
    row, manifest = _session(db)
    tracks = sorted(manifest["tracks"], key=lambda t: t["position"])
    if any(t["file"] == str(track.path) for t in tracks):
        raise ValueError("already in the playlist")
    entry = {
        "position": 0, "file": str(track.path), "staged": "", "artist": track.artist, "title": track.title,
        "album": track.album, "duration": round(track.duration), "source": "added", "energy": energy,
    }
    tracks.insert(max(0, min(position - 1, len(tracks))), entry)
    return _apply(cfg, db, row, manifest, tracks, copy={str(track.path): track.path})


def remove_tracks(cfg: Config, db, positions: set[int]) -> dict:
    row, manifest = _session(db)
    tracks = sorted(manifest["tracks"], key=lambda t: t["position"])
    keep = [t for t in tracks if t["position"] not in positions]
    if len(keep) == len(tracks):
        raise ValueError("no such position")
    return _apply(cfg, db, row, manifest, keep)


def move_track(cfg: Config, db, src: int, dst: int) -> dict:
    row, manifest = _session(db)
    tracks = sorted(manifest["tracks"], key=lambda t: t["position"])
    if not 1 <= src <= len(tracks):
        raise ValueError("no such position")
    t = tracks.pop(src - 1)
    tracks.insert(max(0, min(dst - 1, len(tracks))), t)
    return _apply(cfg, db, row, manifest, tracks)
