"""Fetch files for playlist entries missing locally (explicit opt-in, via yt-dlp)."""
from __future__ import annotations

import functools
import subprocess
from collections.abc import Callable
from pathlib import Path

from .ytm import Entry


def quiet_run(cmd: list[str]) -> subprocess.CompletedProcess:
    """yt-dlp is chatty: keep its output off the screen (the spinner says what is going on)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def download_command(entry: Entry, dest: Path) -> list[str]:
    return [
        "yt-dlp", "-x", "--audio-format", "mp3", "--embed-metadata", "--no-playlist",
        "-o", str(dest / "%(artist,channel)s - %(title)s.%(ext)s"),
        f"https://music.youtube.com/watch?v={entry.video_id}",
    ]


def acquire(
    entries: list[Entry],
    dest: Path,
    run: Callable = quiet_run,
    ask: Callable[[str], str] | None = None,
) -> list[Entry]:
    """Download each entry; `ask` (if given) gates the batch. Returns entries that failed."""
    if ask and ask(f"Download {len(entries)} missing tracks into {dest}? [y/N] ").strip().lower() != "y":
        return list(entries)
    dest.mkdir(parents=True, exist_ok=True)
    failed = []
    for e in entries:
        if run(download_command(e, dest)).returncode != 0:
            failed.append(e)
    return failed


def playlist_download_command(
    cfg, url: str, items: list[int] | None = None, output: str | None = None
) -> list[str]:
    """The user's `ymp3` command, optionally limited to playlist positions (--playlist-items)."""
    subs = {
        "cookies": str(Path(cfg.cookies_file).expanduser()),
        "download_dir": str(cfg.music_path),
    }
    cmd = [part.format(**subs) for part in cfg.download_command]
    if output and "--output" in cmd:
        cmd[cmd.index("--output") + 1] = output
    if items:
        cmd += ["--playlist-items", ",".join(str(i) for i in sorted(items))]
    return [*cmd, url]


def download_missing(
    cfg, url: str, missing: list[Entry], run: Callable = quiet_run, ask: Callable[[str], str] | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> bool:
    """Download only the missing playlist positions with the configured command. True if it ran OK."""
    if not missing:
        return True
    if ask and ask(f"Download {len(missing)} missing tracks into {cfg.music_dir}? [y/N] ").strip().lower() != "y":
        return False
    progress(f"downloading {len(missing)} playlist track{'s' * (len(missing) != 1)}")
    return run(playlist_download_command(cfg, url, [e.index for e in missing if e.index])).returncode == 0


# --- wishlist: tracks you play / Last.fm suggests but have no file for ---------------------------

RETRY_AFTER = 30 * 86400


@functools.cache
def _ytmusic():
    from ytmusicapi import YTMusic  # unauthenticated song search; imported lazily

    return YTMusic()


def yt_search(query: str, limit: int = 10) -> list[dict]:
    return _ytmusic().search(query, filter="songs", limit=limit)


def find_song(artist: str, title: str, search: Callable[[str], list[dict]] = yt_search) -> Entry | None:
    """First YouTube Music *song* whose title and artist both match (so no covers, lives or lookalikes)."""
    from .link import _artist_ok
    from .matcher import norm

    for r in search(f"{artist} {title}"):
        names = ", ".join(a["name"] for a in r.get("artists") or [])
        if norm(r.get("title", "")) == norm(title) and _artist_ok(artist, names):
            return Entry(r["videoId"], names, r["title"], r.get("duration_seconds"))
    return None


def balanced(wishes: list, weights: dict[str, float] | None) -> list:
    """Interleave wishes by bucket in proportion to the mix shares, best first inside each bucket,
    so a limited download quota still brings obsessions, discoveries and lost gems alike."""
    if not weights:
        return list(wishes)
    queues: dict[str, list] = {}
    for w in wishes:
        queues.setdefault(next(iter(w.sources), ""), []).append(w)
    credit = dict.fromkeys(queues, 0.0)
    out = []
    while any(queues.values()):
        for b, q in queues.items():
            if q:
                credit[b] += weights.get(b, 0.05)
        b = max((b for b in queues if queues[b]), key=lambda b: credit[b])
        credit[b] -= sum(weights.get(x, 0.05) for x in queues if queues[x])
        out.append(queues[b].pop(0))
    return out


def preview_command(cfg, video_id: str, output: str, start: int, end: int) -> list[str]:
    """The configured download command, cut down to an mp3 of seconds start..end (no tags, no cover art)."""
    cmd = playlist_download_command(cfg, f"https://music.youtube.com/watch?v={video_id}", output=output)
    url = cmd.pop()
    out, skip = [], False
    for part in cmd:
        if skip:
            skip = False
        elif part == "--parse-metadata":
            skip = True  # and its argument
        elif part not in ("--embed-metadata", "--embed-thumbnail"):
            out.append(part)
    if "-x" not in out and "--extract-audio" not in out:  # the player needs mp3, not a video container
        out += ["--extract-audio", "--audio-format", "mp3"]
    return [*out, "--download-sections", f"*{start}-{end}", "--no-playlist", url]


def download_video(cfg, video_id: str, run: Callable = quiet_run) -> bool:
    """Download one YouTube Music song into the library folder with the configured command."""
    out = str(cfg.music_path / "%(album|Singles)s" / "%(artist,channel)s - %(title)s.%(ext)s")
    cmd = playlist_download_command(cfg, f"https://music.youtube.com/watch?v={video_id}", output=out)
    return run(cmd).returncode == 0


def fetch_wishes(
    cfg,
    db,
    wishes: list,
    limit: int,
    search: Callable[[str], list[dict]] = yt_search,
    run: Callable = quiet_run,
    ask: Callable[[str], str] | None = None,
    now: float | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> int:
    """Download up to `limit` wishlist tracks into the library folder (cfg.music_dir). Remembers outcomes so nothing
    that was not found (or already fetched) is retried on every sync. Returns the number fetched."""
    import time

    from .matcher import norm

    now = now if now is not None else time.time()
    tried = {r["key"]: r for r in db.execute("SELECT key, status, at FROM wish_attempts")}
    todo = []
    for w in balanced(wishes, getattr(cfg, "mix_weights", None)):
        key = f"{norm(w.artist)}|{norm(w.title)}"
        row = tried.get(key)
        if row and (row["status"] == "ok" or now - row["at"] < RETRY_AFTER):
            continue
        todo.append((key, w))
        if len(todo) >= limit * 3:  # misses (not on YouTube Music as a song) must not eat the quota
            break
    if not todo:
        return 0
    if ask and ask(f"Download up to {limit} of {len(todo)} wishlist tracks into {cfg.music_dir}? [y/N] ").strip().lower() != "y":
        return 0
    fetched = 0
    for key, w in todo:
        if fetched >= limit:
            break
        progress(f"downloading {fetched + 1}/{limit}: {w.artist} - {w.title}")
        entry = find_song(w.artist, w.title, search)
        status = "notfound"
        if entry:
            status = "ok" if download_video(cfg, entry.video_id, run) else "failed"
        db.execute("INSERT OR REPLACE INTO wish_attempts(key, status, at) VALUES(?,?,?)", (key, status, now))
        db.commit()  # per attempt: a download can take minutes, keep what is done if interrupted
        fetched += status == "ok"
    return fetched


def fetched_wishes(db) -> list[tuple[str, str]]:
    """(artist, title) of every wishlist track downloaded so far, in normalised spelling."""
    out = []
    for r in db.execute("SELECT key FROM wish_attempts WHERE status='ok'"):
        artist, _, title = r["key"].partition("|")
        out.append((artist, title))
    return out
