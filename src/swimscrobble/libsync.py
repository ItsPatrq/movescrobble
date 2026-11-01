"""Sync the swim pool with the local library: link wanted tracks to files, find what is missing."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import affinity, history, poolsync
from .acquire import fetched_wishes
from .blocklist import Blocklist
from .buckets import assign
from .config import Config
from .library import Track, scan_config
from .link import LocalIndex
from .taste import Taste
from .ytm import Entry


@dataclass
class LibSyncReport:
    pool: poolsync.SyncReport
    playlist_total: int = 0
    playlist_missing: list[Entry] = field(default_factory=list)
    downloaded: bool = False


def sync_library(
    cfg: Config,
    db,
    *,
    size: int = 1000,
    fetch_playlist: Callable[[str], list[Entry]],
    history_sync: Callable[[], object] | None = None,
    taste_fetch: Callable[[str, dict, dict, list[str]], Taste] | None = None,
    download: Callable[[list[Entry]], None] | None = None,
    fetch_wishes: Callable[[list], int] | None = None,
    library_loader: Callable[[], list[Track]] | None = None,
    now: float | None = None,
) -> LibSyncReport:
    """Pool first (playlist tracks, downloaded if missing), then judge each against Last.fm history.

    `download` receives the playlist entries missing locally; the library is rescanned afterwards.
    Pool keys are always the *local* files' artist/title, so later lookups are exact.
    """
    load = library_loader or (lambda: scan_config(cfg))
    library = load()
    entries = [e for url in cfg.playlists for e in fetch_playlist(url)]
    idx = LocalIndex(library)
    missing = [e for e in entries if not idx.find(e.artist, e.title, e.duration)]
    downloaded = False
    if missing and download:
        download(missing)
        library = load()
        idx = LocalIndex(library)
        missing = [e for e in entries if not idx.find(e.artist, e.title, e.duration)]
        downloaded = True

    key = lambda t: (t.artist.casefold(), t.title.casefold())  # noqa: E731
    when = now if now is not None else time.time()

    # history and taste depend only on Last.fm, not on the library: computed once even if judged twice
    taste, tracks, artists = None, {}, {}  # no history: everything is "discovery"
    if cfg.lastfm_user:
        if history_sync:
            history_sync()
        tracks, artists = affinity.compute(db, when)
        if taste_fetch:
            taste = taste_fetch(cfg.lastfm_user, tracks, artists, sorted({t.artist for t in library}))

    def judge():
        """Pool first (the library folder, or just the playlist tracks), then check each against the history."""
        idx = LocalIndex(library)
        pool = None
        if cfg.pool == "playlist" and entries:
            pool = {key(t) for e in entries if (t := idx.find(e.artist, e.title, e.duration))}
            # tracks fetched from the wishlist belong to the pool too, whether or not they are in a playlist
            pool |= {key(t) for a, ti in fetched_wishes(db) if (t := idx.find(a, ti))}
        owned, wanted = assign(taste, tracks, artists, library, pool, idx, when)
        return blocked.keep(owned), blocked.keep(wanted)  # never pooled, never wished for, never downloaded

    blocked = Blocklist.load(db)

    owned, wanted = judge()
    if wanted and fetch_wishes and fetch_wishes(wanted):
        library = load()  # new files arrived: rescan and judge again
        owned, wanted = judge()
    rep = poolsync.sync_pool(db, owned, {key(t) for t in library}, history.play_counts(db), size)
    rep.missing.extend(wanted)
    return LibSyncReport(rep, len(entries), missing, downloaded)
