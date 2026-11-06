"""One YouTube Music playlist that always holds what is on the headphones.

The staged playlist is the source of truth: the mirror is replaced to match it, so edits made on YouTube are overwritten.
It is created once and its id remembered; it is never one of your `ytm_playlist` sources. A failure here only leaves
the mirror out of date until the next plug-in. It never gets in the way of the headphones.
"""
from __future__ import annotations

from collections.abc import Callable

from . import acquire, history

META_KEY = "ytm_mirror_playlist"
DESCRIPTION = "What is on the headphones right now. Managed by swimscrobble: edits made here are overwritten."


def open_client(cfg):
    """A logged-in YTMusic, or None when the mirror is off or `ytm-auth` has not been run."""
    if not cfg.ytm_mirror or not cfg.ytm_auth_path:
        return None
    from ytmusicapi import YTMusic

    return YTMusic(str(cfg.ytm_auth_path))


def _find_video(artist: str, title: str) -> str | None:
    entry = acquire.find_song(artist, title)
    return entry.video_id if entry else None


def _video_id(db, artist: str, title: str, find: Callable[[str, str], str | None]) -> str | None:
    """Found videos are remembered, so a re-sync costs no searches; a miss is tried again next time."""
    row = db.execute("SELECT video_id FROM yt_videos WHERE artist=? AND title=?", (artist, title)).fetchone()
    if row:
        return row["video_id"]
    video = find(artist, title)
    if video:
        db.execute("INSERT OR REPLACE INTO yt_videos(artist, title, video_id) VALUES(?,?,?)", (artist, title, video))
        db.commit()
    return video


def _existing_tracks(client, playlist: str) -> list[dict] | None:
    """The playlist's tracks, or None if it is gone. Only a missing playlist reads as gone (ytmusicapi fails to parse
    its page): a network error must surface, or it would leave a second playlist behind."""
    try:
        return client.get_playlist(playlist, limit=None).get("tracks") or []
    except KeyError:
        return None


def _push(db, cfg, client, video_ids: list[str]) -> bool:
    """Make YouTube match `video_ids`; False when it already did."""
    playlist = history.get_meta(db, META_KEY)
    current = _existing_tracks(client, playlist) if playlist else None
    if current is None:
        created = client.create_playlist(cfg.ytm_mirror_title, DESCRIPTION, cfg.ytm_mirror_privacy, video_ids)
        if not isinstance(created, str):  # ytmusicapi hands back the raw response when YouTube refuses
            raise RuntimeError(f"YouTube Music did not create the playlist: {created}")
        history.set_meta(db, META_KEY, created)
        db.commit()
        return True
    if [t.get("videoId") for t in current] == video_ids:
        return False
    if current:
        client.remove_playlist_items(playlist, current)
    client.add_playlist_items(playlist, video_ids, duplicates=True)
    return True


def update(db, cfg, manifest: dict, client=None, find: Callable[[str, str], str | None] = _find_video) -> str:
    """Bring the mirror in line with `manifest`. Returns what to tell you; empty when there is nothing to say."""
    try:
        client = client or open_client(cfg)
        if client is None:
            return ""
        tracks = sorted(manifest["tracks"], key=lambda t: t["position"])
        found = [_video_id(db, t["artist"], t["title"], find) for t in tracks]
        video_ids = list(dict.fromkeys(v for v in found if v))
        if not video_ids or not _push(db, cfg, client, video_ids):
            return ""
        return f"YouTube playlist updated ({len(video_ids)}/{len(tracks)} tracks)"
    except Exception as e:
        return f"YouTube playlist FAILED: {e}"
