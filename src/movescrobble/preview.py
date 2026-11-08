"""Listen before you choose: 30-second clips of local files or of YouTube Music songs, played in the background.

A local file is cut from about a third in (past the intro) with ffmpeg; a song you do not own is fetched as just
that section with the configured yt-dlp command. Playback is afplay on macOS, ffplay elsewhere; one clip at a time.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import acquire, audio
from .config import config_dir

CLIP_SECONDS = 30
CLIP_FROM = 0.35  # share of the track to skip: intros say little about a song


def player_command(path: Path) -> list[str]:
    if sys.platform == "darwin" and shutil.which("afplay"):
        return ["afplay", str(path)]
    return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", str(path)]


class Player:
    def __init__(self, cfg, folder: Path | None = None, popen: Callable = subprocess.Popen,
                 run: Callable = acquire.quiet_run, find_song: Callable = acquire.find_song):
        self.cfg, self.popen, self.run, self.find_song = cfg, popen, run, find_song
        self.folder = folder or config_dir() / "preview"
        self.proc = None

    def play_file(self, path: Path, duration: float) -> None:
        self.stop()  # the next clip is written over the file the last one may still be playing from
        self.folder.mkdir(parents=True, exist_ok=True)
        clip = self.folder / "clip.mp3"
        audio.ffmpeg("-ss", f"{max(0.0, duration * CLIP_FROM):.1f}", "-t", str(CLIP_SECONDS), "-i", str(path),
                     "-vn", "-c:a", "libmp3lame", "-q:a", "5", str(clip))
        self._start(clip)

    def play_song(self, artist: str, title: str) -> bool:
        """Stream a clip of a song you do not own; False when YouTube Music has no such song (or it failed)."""
        entry = self.find_song(artist, title)
        if not entry:
            return False
        self.folder.mkdir(parents=True, exist_ok=True)
        for old in self.folder.glob("remote.*"):
            old.unlink()
        start = max(0, int((entry.duration or 200) * CLIP_FROM))
        self.run(acquire.preview_command(self.cfg, entry.video_id, str(self.folder / "remote.%(ext)s"),
                                         start, start + CLIP_SECONDS))
        clip = next(iter(self.folder.glob("remote.mp3")), None)
        if clip is None:
            return False
        self._start(clip)
        return True

    def _start(self, clip: Path) -> None:
        self.stop()
        self.proc = self.popen(player_command(clip), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
