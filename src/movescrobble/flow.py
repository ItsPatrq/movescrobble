"""The two separate flows.

Headphones connected (`on_connect`): syncs what is on them and nothing else.
1. refresh podcasts: newest unplayed episode of each Pocket Casts subscription
2. if the staged playlist has been settled, plan + stage the next one; otherwise it stays
3. keep the YouTube Music copy of the playlist in line

Activities (`settle_swim`, run from `garmin-sync` at any time, headphones or not): Garmin data decides. Only a swim
recorded after staging scrobbles the playlist. A run never touches it; see runs.py.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import audio, canonical, history, lastfm, listenbrainz, power, ytmirror
from .blocklist import Blocklist
from .config import Config, read_json, write_json
from .energy import Features, cached_features
from .generator import Candidate
from .library import Track, scan_config
from .mix import build_mix
from .planner import NEW_TO_YOU, STAGED_RE, PlannedTrack
from .scrobble import build_scrobbles
from .session import played_part, to_manifest
from .workouts import DEFAULT as DEFAULT_WORKOUT

if TYPE_CHECKING:
    from .garmin import Swim

FindSwim = Callable[[datetime, datetime], "Swim | None"]  # (staged at, now) -> the swim in between
Progress = Callable[[str], None]  # "what is happening now", for a spinner; may be called from worker threads


def _quiet(_message: str) -> None:
    pass


STATE_FILE = "device_state.json"


def _handled() -> bool:
    return (read_json(STATE_FILE) or {}).get("handled", False)


def _set_handled(v: bool) -> None:
    write_json(STATE_FILE, {"handled": v})


def describe_sent(n: int, scrobbles: list, cfg: Config, listenbrainz_too: bool = False) -> str:
    return f"Last.fm accepted {n}/{len(scrobbles)}" + ("; sent to ListenBrainz" if cfg.listenbrainz or listenbrainz_too else "")


def send_scrobbles(db, scrobbles, cfg: Config, *, listenbrainz_too=False, lastfm_submit=None, lb_submit=None,
                   listeners=None) -> int:
    """The only path that sends scrobbles: Last.fm, plus ListenBrainz when configured. Returns Last.fm's accepted count.
    Each track goes to the page it already has (canonical.py); `scrobbles` itself keeps the file's tags."""
    lastfm_submit = lastfm_submit or (lambda s: lastfm.submit(s, lastfm.load_session_key()))
    lb_submit = lb_submit or listenbrainz.submit
    stale = lastfm.check_window(scrobbles)
    if stale:
        raise ValueError(f"{len(stale)} scrobbles outside Last.fm's 14-day window")
    outgoing = canonical.canonicalize(scrobbles, db, listeners)
    n = lastfm_submit(outgoing)
    if cfg.listenbrainz or listenbrainz_too:
        lb_submit(outgoing)
    return n


@dataclass
class Settled:
    """A swim worked out from the staged session: what was heard, when, and how long you swam."""

    plan: list[PlannedTrack]
    scrobbles: list
    minutes: float
    distance_m: float | None = None
    skipped: list[tuple[str, str]] = field(default_factory=list)
    swim: Swim | None = None


def settle(manifest: dict, cfg: Config, now: datetime, swim: Swim | None = None, played_minutes: float | None = None,
           skipped: set[int] = frozenset(), start: datetime | None = None) -> Settled:
    """The one place that turns a session into scrobbles. Length: what you said, else the Garmin swim, else the plan.
    Start: what you said, else when the Garmin swim started, else counted back from `now` (plug-in).
    With a Garmin swim the music started `warmup_minutes` before the watch and played on for `cooldown_minutes` after
    it: those are heard (and scrobbled) too. The session still records the swim itself."""
    swum = played_minutes if played_minutes is not None else swim.minutes if swim else None
    warmup = cfg.warmup_minutes if swim else 0
    listening = swum + warmup + (cfg.cooldown_minutes if swim else 0) if swum is not None else None
    plan, listened = played_part(manifest, listening, skipped)
    if start is None:
        start = (swim.start - timedelta(minutes=warmup) if swim
                 else now - timedelta(minutes=cfg.session_offset_minutes, seconds=listened))
    return Settled(plan, build_scrobbles(plan, start), swum if swum is not None else listened / 60,
                   swim.distance_m if swim else None,
                   [(t["artist"], t["title"]) for t in manifest["tracks"] if t["position"] in skipped], swim)


def lookup_swim(find_swim: FindSwim | None, row, now: datetime, warn: Callable[[str], None]) -> Swim | None:
    """The Garmin swim between staging and `now`; a Garmin failure is reported, never fatal."""
    if not find_swim:
        return None
    try:
        return find_swim(datetime.fromtimestamp(row["planned_at"]), now)
    except Exception as e:  # Garmin down or logged out: fall back to asking / the planned length
        warn(f"Garmin lookup failed: {e}")
        return None


def record(db, session_row, settled: Settled) -> None:
    history.mark_scrobbled(
        db, session_row["id"], [(s.artist, s.title, s.timestamp.timestamp()) for s in settled.scrobbles],
        skipped=settled.skipped, minutes=settled.minutes, distance_m=settled.distance_m,
    )


def ensure_pending(db, session_row) -> None:
    """Right before sending: another run (the shell, `garmin-sync`, `scrobble-pending`) may have settled or discarded
    this session since it was read, and Last.fm would take the same swim twice."""
    if not history.still_planned(db, session_row["id"]):
        raise ValueError(f"session {session_row['id']} was settled or discarded meanwhile; nothing was sent")


def submit_session(db, session_row, settled: Settled, cfg: Config, **deps) -> int:
    """Send to configured services, then mark the session scrobbled exactly once. Returns Last.fm's accepted count."""
    ensure_pending(db, session_row)
    n = send_scrobbles(db, settled.scrobbles, cfg, **deps)
    record(db, session_row, settled)
    return n


def settle_swim(db, cfg: Config, now: datetime, find_swim: FindSwim, warn: Callable[[str], None] = _quiet,
                **deps) -> str:
    """Scrobble the staged playlist if Garmin shows a swim since it was staged; without one, nothing happens."""
    row = history.pending_session(db)
    if row is None:
        return "no staged playlist waiting for a swim"
    swim = lookup_swim(find_swim, row, now, warn)
    if swim is None:
        return "no swim on Garmin since the playlist was staged"
    settled = settle(json.loads(row["manifest"]), cfg, now, swim)
    try:
        n = submit_session(db, row, settled, cfg, **deps)
    except Exception as e:  # keep the session pending so nothing is lost
        return f"scrobbling FAILED: {e}"
    _set_handled(False)  # the headphones may stage the next playlist
    return f"{swim.describe()}: scrobbled {n} tracks"


def clear_staged(stage_dir: Path, previous_manifest: dict | None) -> int:
    """Remove only files this tool staged earlier (named in the manifest, prefix-checked)."""
    if not previous_manifest:
        return 0
    n = 0
    for t in previous_manifest["tracks"]:
        f = stage_dir / t["staged"]
        if STAGED_RE.match(f.name) and f.exists():
            f.unlink()
            n += 1
    return n


def stage_plan(plan: list[PlannedTrack], dest: Path, prepare: Callable[[Path], Path],
               progress: Progress = _quiet) -> None:
    """Copy each planned file to `dest` under its play-order name. `prepare` maps a library file to the file to copy
    (audio.leveller: a cached levelled copy). Two phases: prepare in parallel on this machine (CPU-bound ffmpeg), then
    copy to the device one file at a time in play order (flash storage is slowest with interleaved writes)."""
    dest.mkdir(parents=True, exist_ok=True)
    done = itertools.count(1)  # next() on a count is atomic in CPython: safe across the pool's threads

    def prep(p: PlannedTrack) -> Path:
        src = prepare(p.track.path)
        progress(f"levelling tracks {next(done)}/{len(plan)}")
        return src

    progress(f"levelling tracks 0/{len(plan)}")
    with ThreadPoolExecutor(max_workers=max((os.cpu_count() or 2) // 2, 1)) as pool:
        sources = list(pool.map(prep, plan))
    for i, (p, src) in enumerate(zip(plan, sources, strict=True), 1):
        progress(f"copying to the headphones {i}/{len(plan)}")
        shutil.copy2(src, dest / p.staged_name)
    audio.prune()


def pool_candidates(db) -> list[Candidate]:
    return [
        Candidate(r["artist"], r["title"], float(r["score"]), set(filter(None, (r["source"] or "").split(","))))
        for r in db.execute("SELECT artist, title, score, source FROM pool WHERE retired=0")
    ]


def build_plan(db, cfg: Config, library: list[Track], seed: int | None = None,
               feature_of: Callable[[Track], Features] | None = None, workout: str | None = None,
               **overrides) -> list[PlannedTrack]:
    """The mix with every config-derived setting, your swim history and skips; `overrides` replace settings."""
    skips, artist_skips = history.skip_counts(db)
    kw = dict(
        minutes=cfg.playlist_minutes, swim_minutes=cfg.minutes, seed=seed, quotas=cfg.mix_weights,
        min_energy=cfg.min_energy, max_per_artist=cfg.max_per_artist, variety=cfg.variety,
        plays=history.play_counts(db), recent=history.recent_plays(db, cfg.no_repeat_days),
        workout=workout or cfg.workout, skips=skips, artist_skips=artist_skips, power=power.scores(db),
    )
    kw.update({k: v for k, v in overrides.items() if v is not None})
    blocked = Blocklist.load(db)
    return build_mix(blocked.keep(library), blocked.keep(pool_candidates(db)),
                     feature_of or (lambda t: cached_features(db, t)), **kw)


def fit_to_space(plan: list[PlannedTrack], cfg: Config) -> list[PlannedTrack]:
    """Drop buffer tracks from the end until the files fit on the device (keeping `space_reserve_mb` free).
    The swim itself must fit, or nothing is staged."""
    free = shutil.disk_usage(cfg.stage_path).free - cfg.space_reserve_mb * 1e6
    sizes = [p.track.path.stat().st_size * 1.1 for p in plan]  # levelled copies can come out a little bigger
    keep = len(plan)
    while keep and sum(sizes[:keep]) > free:
        keep -= 1
    if sum(p.track.duration for p in plan[:keep]) < min(cfg.minutes * 60, sum(p.track.duration for p in plan)) - 1:
        raise RuntimeError(f"not enough space on the device: {sum(sizes) / 1e6:.0f} MB needed, "
                           f"{max(free, 0) / 1e6:.0f} MB free (free some space or lower playlist_minutes)")
    return plan[:keep]


def music_reserve_mb(cfg: Config, db) -> float:
    """Space podcasts must leave for the next playlist: an estimate of its size, minus what the current one uses
    (that is freed before staging), plus the safety margin."""
    need = cfg.playlist_minutes * 2.5  # MB at ~320 kbit/s
    staged = {t["staged"] for t in (last_staged_manifest(db) or {}).get("tracks", [])}
    used = sum(e.stat().st_size for e in os.scandir(cfg.stage_path) if e.name in staged) / 1e6 \
        if cfg.stage_path.is_dir() else 0
    return max(need - used, 0) + cfg.space_reserve_mb


def plan_and_stage(
    db,
    cfg: Config,
    library: list[Track],
    seed: int | None = None,
    previous: dict | None = None,
    feature_of: Callable[[Track], Features] | None = None,
    now: datetime | None = None,
    workout: str | None = None,
    progress: Progress = _quiet,
) -> dict:
    stage_dir = cfg.stage_path
    stage_dir.mkdir(parents=True, exist_ok=True)  # clear_staged needs it before stage_plan runs
    progress("removing the previous playlist")
    clear_staged(stage_dir, previous)
    progress("picking tracks")
    plan = fit_to_space(build_plan(db, cfg, library, seed, feature_of, workout), cfg)
    stage_plan(plan, stage_dir, audio.leveller(cfg.loudness), progress)
    manifest = to_manifest(plan, seed, cfg.minutes)
    manifest["workout"] = workout or cfg.workout
    manifest["id"] = history.add_session(
        db, manifest, cfg.minutes, seed, str(stage_dir), at=now.timestamp() if now else None)
    return manifest


def on_connect(
    cfg: Config,
    db,
    now: datetime | None = None,
    notify: Callable[[str], None] = print,
    library_loader: Callable[[Path], list[Track]] | None = None,
    feature_of: Callable[[Track], Features] | None = None,
    podcast_sync: Callable[[], object] | None = None,  # -> a report whose str() is the summary
    pool_sync: Callable[[], str] | None = None,
    force: bool = False,
    progress: Progress = _quiet,
    mirror: Callable[[dict], str] | None = None,
) -> list[str]:
    """Sync the headphones; activities are not looked at. A playlist not yet settled stays on them, because a swim to it
    may still show up on Garmin. `force`: run even if this connection was already handled (the shell's "run it now")."""
    mirror = mirror or (lambda m: ytmirror.update(db, cfg, m))
    now = now or datetime.now()
    log: list[str] = []
    library_loader = library_loader or (lambda p: scan_config(cfg, p))
    if not cfg.device_path.is_dir():
        _set_handled(False)  # unplugged: the next connection is a new one
        return ["device not connected"]
    if _handled() and not force:
        return ["already handled this connection"]
    _set_handled(True)
    note: list[str] = []  # one notification for the whole connection; problems are in it, in capitals

    def step(name: str, fn: Callable[[], object], describe: Callable[[object], str] | None = None) -> None:
        """Run an optional refresh; a failure is logged and noted but never stops staging."""
        try:
            result = fn()
        except Exception as e:
            log.append(f"{name} failed: {e}")
            note.append(f"{name} FAILED: {e}")
            return
        text = describe(result) if describe else str(result)
        log.append(f"{name}: {text}")
        if describe:
            note.append(text)

    try:
        if podcast_sync:  # independent of the playlist: runs on every connection
            progress("syncing podcasts")
            step("podcasts", podcast_sync, str)
        row = history.pending_session(db)
        if row:
            manifest = json.loads(row["manifest"])
            log.append(f"keeping session {row['id']}: settled only by a swim on Garmin (garmin-sync)")
            note.append("playlist unchanged")
        else:
            if pool_sync:  # a failed refresh must not block staging; the old pool still works
                progress("syncing Last.fm history and the pool")
                step("pool sync", pool_sync)
            progress("scanning the library")
            library = library_loader(cfg.music_path)
            manifest = plan_and_stage(db, cfg, library, previous=last_staged_manifest(db),
                                      feature_of=feature_of, now=now, progress=progress)
            log.append(f"staged session {manifest['id']}: {len(manifest['tracks'])} tracks")
            note.append(staged_summary(manifest))
        progress("updating the YouTube Music playlist")
        if mirrored := mirror(manifest):
            log.append(mirrored)
            note.append(mirrored)
    except Exception as e:
        log.append(f"staging failed: {e}")
        note.append(f"staging FAILED: {e}")
    notify("movescrobble: " + " · ".join(note))
    return log


def staged_summary(manifest: dict) -> str:
    tracks = manifest["tracks"]
    parts = [f"staged {len(tracks)} tracks", f"{sum(t['duration'] for t in tracks) / 60:.0f} min"]
    new = sum(t.get("source") in NEW_TO_YOU for t in tracks)
    if new:
        parts.append(f"{new} new")
    workout = manifest.get("workout", DEFAULT_WORKOUT)
    return ", ".join(parts) + (f" ({workout})" if workout != DEFAULT_WORKOUT else "")


def reroll(
    cfg: Config,
    db,
    seed: int | None = None,
    library_loader: Callable[[Path], list[Track]] | None = None,
    feature_of: Callable[[Track], Features] | None = None,
    now: datetime | None = None,
    workout: str | None = None,
    progress: Progress = _quiet,
) -> dict:
    """Replace the staged playlist with a fresh one. The old session is discarded, not scrobbled."""
    if not cfg.device_path.is_dir():
        raise RuntimeError(f"{cfg.device_name} is not connected")
    library_loader = library_loader or (lambda p: scan_config(cfg, p))
    previous = last_staged_manifest(db)
    history.discard_pending(db)
    progress("scanning the library")
    library = library_loader(cfg.music_path)
    manifest = plan_and_stage(db, cfg, library, seed=seed, previous=previous, workout=workout,
                              feature_of=feature_of, now=now, progress=progress)
    ytmirror.update(db, cfg, manifest)
    return manifest


def last_staged(db):
    return db.execute("SELECT * FROM sessions WHERE staged_dir IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()


def last_staged_manifest(db) -> dict | None:
    row = last_staged(db)
    return json.loads(row["manifest"]) if row else None


def orphans(cfg: Config, db) -> tuple[int, int | None]:
    """(prefixed files from the latest session still on the device, that session's id) when nothing is pending."""
    if history.pending_session(db) is not None or not cfg.device_path.is_dir():
        return 0, None
    row = last_staged(db)
    if row is None:
        return 0, None
    manifest = json.loads(row["manifest"])
    n = sum(1 for t in manifest["tracks"] if (cfg.stage_path / t["staged"]).exists())
    return n, row["id"]


def adopt_orphans(cfg: Config, db) -> int | None:
    """Make the playlist still on the headphones the current session again (so it can be scrobbled)."""
    n, sid = orphans(cfg, db)
    if not n:
        return None
    db.execute("UPDATE sessions SET status='planned' WHERE id=?", (sid,))
    db.commit()
    return sid


def clear_orphans(cfg: Config, db) -> int:
    n, sid = orphans(cfg, db)
    if not n:
        return 0
    return clear_staged(cfg.stage_path, last_staged_manifest(db))
