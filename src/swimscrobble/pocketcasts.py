"""Mirror the newest unplayed episode of each subscribed podcast to a folder (unofficial, reverse-engineered API).

Pocket Casts does not support third-party clients; use your own account at your own risk.
"""
from __future__ import annotations

import itertools
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import audio
from .config import read_json, secret, write_private_json
from .net import open_url, request_json

API = "https://api.pocketcasts.com"
TOKEN_FILE = "pocketcasts.json"
SCRATCH_PREFIX = "swimscrobble-podcasts-"
STALE_SCRATCH_S = 24 * 3600

Transport = Callable[[str, dict, str | None], dict]  # (path, body, token) -> json
Downloader = Callable[[str, Path], None]
Progress = Callable[[str], None]


def _quiet(_message: str) -> None:
    pass


def http_post(path: str, body: dict, token: str | None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return request_json(API + path, json.dumps(body).encode(), headers)


def http_download(url: str, dest: Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with open_url(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.rename(dest)  # never leave half files that look complete
    finally:
        tmp.unlink(missing_ok=True)


def login(email: str | None = None, password: str | None = None, transport: Transport = http_post) -> str:
    email = email or secret("POCKETCASTS_EMAIL")
    password = password or secret("POCKETCASTS_PASSWORD")
    if not email or not password:
        raise SystemExit("set POCKETCASTS_EMAIL and POCKETCASTS_PASSWORD")
    token = transport("/user/login", {"email": email, "password": password}, None)["token"]
    write_private_json(TOKEN_FILE, {"token": token})
    return token


def saved_token() -> str | None:
    return (read_json(TOKEN_FILE) or {}).get("token")


COMPLETED = 3  # playingStatus: 1 unplayed, 2 in progress, 3 played


def latest_unplayed(token: str, limit: int = 10, transport: Transport = http_post) -> list[dict]:
    """Newest episode of each subscription, if not played or archived yet; newest first, at most `limit`."""
    subs = transport("/user/podcast/list", {"v": 1}, token).get("podcasts", [])
    fresh = [
        p for p in subs
        if p.get("lastEpisodeUuid") and p.get("lastEpisodePlayingStatus") != COMPLETED
        and not p.get("lastEpisodeArchived")
    ]
    fresh.sort(key=lambda p: p.get("lastEpisodePublished") or "", reverse=True)
    with ThreadPoolExecutor(max_workers=5) as pool:  # independent lookups, order kept
        eps = list(pool.map(
            lambda p: transport("/user/episode", {"uuid": p["lastEpisodeUuid"], "podcast": p["uuid"]}, token),
            fresh[:limit]))
    return [e for e in eps if e.get("url") and e.get("uuid")]


@dataclass
class PodcastReport:
    downloaded: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "name: error"
    no_space: list[str] = field(default_factory=list)
    on_device: int = 0

    def __str__(self) -> str:
        parts = [f"{self.on_device} podcasts" + (f" ({len(self.downloaded)} new)" if self.downloaded else "")]
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        if self.no_space:
            parts.append(f"{len(self.no_space)} skipped: device full")
        return ", ".join(parts)


@dataclass(frozen=True)
class Fetcher:
    """How episodes are fetched: the download (into a local scratch file), the format it produces (`ext`, None = the
    URL's own) and a `variant` label stored per episode, so changing the processing refetches instead of renaming."""

    fetch: Downloader = http_download
    ext: str | None = None
    variant: str = "original"

    @classmethod
    def for_settings(cls, speed: float, trim_silence: bool) -> Fetcher:
        if audio.podcast_filter(speed, trim_silence) is None:
            return cls()

        def fetch(url: str, dest: Path) -> None:
            raw = dest.with_name(dest.name + ".raw")
            try:
                http_download(url, raw)
                try:
                    audio.process_podcast(raw, dest, speed, trim_silence)
                except Exception:  # ffmpeg cannot read it: keep the original audio
                    raw.rename(dest)
            finally:
                raw.unlink(missing_ok=True)
        return cls(fetch, ".mp3", f"{speed:.2f}x" + ("+trim" if trim_silence else ""))


def sync_latest(dest: Path, db, limit: int = 10, token: str | None = None, speed: float = 1.0,
                trim_silence: bool = False, reserve_mb: float = 0, progress: Progress = _quiet) -> PodcastReport:
    token = token or saved_token() or login()
    progress("podcasts: checking subscriptions")
    return sync_queue(latest_unplayed(token, limit), dest, db, limit, Fetcher.for_settings(speed, trim_silence),
                      reserve_mb=reserve_mb, progress=progress)


def on_device(db) -> list[dict]:
    """Episodes the last sync put on the headphones, in play order."""
    return [dict(r) for r in db.execute("SELECT uuid, podcast, title, filename FROM podcasts ORDER BY filename")]


def mark_played(episodes: list[dict], token: str | None = None, transport: Transport = http_post) -> int:
    """Tell Pocket Casts these were listened to; the next sync then replaces them."""
    token = token or saved_token() or login()
    for e in episodes:
        transport("/sync/update_episode", {"uuid": e["uuid"], "podcast": e["podcast"], "status": COMPLETED}, token)
    return len(episodes)


def describe_marked(episodes: list[dict]) -> str:
    return f"marked {len(episodes)} played in Pocket Casts: " + ", ".join(e["title"] for e in episodes)


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", s).strip()[:80]


def filename_for(position: int, episode: dict, ext: str | None = None) -> str:
    ext = ext or Path(urlparse(episode["url"]).path).suffix.lower()
    ext = ext if ext in {".mp3", ".m4a", ".aac", ".ogg", ".opus"} else ".mp3"
    return f"{position:02d} - {_safe(episode.get('title', 'episode'))}{ext}"


def _sweep_stale_scratch() -> None:
    """A killed sync cannot clean up after itself. Recent folders are left alone: they may belong to a running sync."""
    for old in Path(tempfile.gettempdir()).glob(SCRATCH_PREFIX + "*"):
        try:
            if time.time() - old.stat().st_mtime > STALE_SCRATCH_S:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass  # gone already


def _copy_whole(src: Path, dest: Path) -> None:
    """A file under its final name is complete: sync takes an existing episode as done and never reads it back."""
    part = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def sync_queue(
    episodes: list[dict],
    dest: Path,
    db,
    max_episodes: int = 10,
    fetcher: Fetcher | Downloader | None = None,
    reserve_mb: float = 0,
    workers: int = 3,
    progress: Progress = _quiet,
) -> PodcastReport:
    """Make `dest` hold the first N episodes, ordered by filename prefix.

    New episodes are fetched (and processed) in parallel into a local scratch folder, then copied to `dest` once each:
    the device is written to once per file and never read back. Each scratch file is deleted as soon as it is copied,
    so episodes never stay on this computer. An episode that would leave less than `reserve_mb` free (room for the next
    playlist) is skipped; newest come first, so the oldest are the ones left out.
    """
    fetcher = fetcher if isinstance(fetcher, Fetcher) else Fetcher(fetcher) if fetcher else Fetcher()
    dest.mkdir(parents=True, exist_ok=True)
    for leftover in dest.glob("*.part"):  # copy cut short by a killed run
        leftover.unlink(missing_ok=True)
    want = {e["uuid"]: (filename_for(i + 1, e, fetcher.ext), e) for i, e in enumerate(episodes[:max_episodes])}
    have = {r["uuid"]: (r["filename"], r["variant"]) for r in db.execute("SELECT uuid, filename, variant FROM podcasts")}
    report = PodcastReport()

    for uuid, (name, _) in have.items():
        if uuid not in want:
            (dest / name).unlink(missing_ok=True)
            db.execute("DELETE FROM podcasts WHERE uuid=?", (uuid,))
            report.removed.append(name)
    todo, budget = [], shutil.disk_usage(dest).free - reserve_mb * 1e6
    for uuid, (name, ep) in want.items():
        old, variant = have.get(uuid, (None, None))
        if old and (dest / old).exists():
            if variant != fetcher.variant:  # processed differently: fetch again
                (dest / old).unlink()
            elif old != name:
                (dest / old).rename(dest / name)
                report.renamed.append(name)
        if (dest / name).exists():
            continue
        size = int(ep.get("size") or 0)
        if size > budget:
            report.no_space.append(name)
            continue
        budget -= size
        todo.append((uuid, name, ep))

    _sweep_stale_scratch()
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as tmp:
        done = itertools.count(1)  # next() on a count is atomic in CPython: safe across the pool's threads

        def fetch(item):
            _, name, ep = item
            try:
                fetcher.fetch(ep["url"], Path(tmp) / name)
                return None
            except Exception as e:  # one bad host must not cost the other episodes
                return e
            finally:
                progress(f"podcasts: fetched {next(done)}/{len(todo)}")
        if todo:
            progress(f"podcasts: fetching 0/{len(todo)}")
        with ThreadPoolExecutor(max_workers=workers) as pool:  # network + ffmpeg bound, independent
            try:
                # map yields as episodes finish: copying meanwhile keeps the scratch folder from holding them all
                for i, ((_uuid, name, _ep), err) in enumerate(zip(todo, pool.map(fetch, todo), strict=True), 1):
                    if err:
                        report.failed.append(f"{name}: {err}")
                        continue
                    progress(f"podcasts: copying to the headphones {i}/{len(todo)}")
                    _copy_whole(Path(tmp) / name, dest / name)
                    (Path(tmp) / name).unlink()
                    report.downloaded.append(name)
            except BaseException:  # device gone: do not download the rest for nothing
                pool.shutdown(wait=False, cancel_futures=True)
                raise

    failed = {f.split(": ", 1)[0] for f in report.failed} | set(report.no_space)
    for uuid, (name, ep) in want.items():
        if name in failed:
            continue
        db.execute(
            "INSERT INTO podcasts(uuid,podcast,title,filename,variant,downloaded_at) "
            "VALUES(?,?,?,?,?,strftime('%s','now')) ON CONFLICT(uuid) DO UPDATE SET filename=excluded.filename, "
            "podcast=excluded.podcast, variant=excluded.variant",
            (uuid, ep.get("podcastUuid") or ep.get("podcast"), ep.get("title", ""), name, fetcher.variant),
        )
    db.commit()
    report.on_device = sum((dest / name).exists() for name, _ in want.values())
    return report
