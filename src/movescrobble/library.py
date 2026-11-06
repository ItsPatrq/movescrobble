"""Scan a music folder into Track records."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"}


@dataclass(frozen=True)
class Track:
    path: Path
    artist: str
    title: str
    album: str
    duration: float  # seconds
    bpm: float | None = None


def _first(tags, key: str, default: str = "") -> str:
    value = tags.get(key) if tags else None
    if not value:
        return default
    return str(value[0]).strip() or default


def _bpm(tags) -> float | None:
    try:
        return float(_first(tags, "bpm")) or None
    except ValueError:
        return None


def read_track(path: Path) -> Track | None:
    audio = MutagenFile(path, easy=True)
    if audio is None or audio.info is None:
        return None
    return Track(
        path=path,
        artist=_first(audio.tags, "artist"),
        title=_first(audio.tags, "title", path.stem),
        album=_first(audio.tags, "album"),
        duration=float(audio.info.length),
        bpm=_bpm(audio.tags),
    )


def scan(
    folder: Path, recursive: bool = True, exclude: tuple[str, ...] = (), min_seconds: float = 60
) -> list[Track]:
    """Read every audio file under `folder`. Skips hidden files, AppleDouble sidecars, excluded
    top-level dirs and anything shorter than `min_seconds` (loops, samples, jingles)."""
    skip = {e.casefold() for e in exclude}
    tracks = []
    paths = folder.rglob("*") if recursive else folder.iterdir()
    for p in sorted(paths):
        rel = p.relative_to(folder)
        if any(part.startswith(".") for part in rel.parts) or rel.parts[0].casefold() in skip:
            continue
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            track = read_track(p)
        except Exception:  # unreadable/corrupt file: ignore, do not abort the whole scan
            continue
        if track and track.duration >= min_seconds:
            tracks.append(track)
    return tracks


def scan_config(cfg, folder: Path | str | None = None) -> list[Track]:
    """`scan` with the configured library folder, excludes and minimum length."""
    return scan(Path(folder) if folder else cfg.music_path, exclude=tuple(cfg.exclude_dirs),
                min_seconds=cfg.min_track_seconds)
