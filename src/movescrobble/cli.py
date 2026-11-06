from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import history
from .library import scan_config
from .planner import fmt_min, parse_positions, plan_session, total_seconds
from .scrobble import build_scrobbles, confirm, to_csv
from .session import played_part, to_manifest
from .workouts import WORKOUTS


def notify(msg: str) -> None:
    print(msg)
    if os.uname().sysname == "Darwin":
        # the text goes in as an argument: quoting it into the script breaks on non-ASCII (json's \u escapes)
        subprocess.run(["osascript", "-e", "on run argv", "-e", 'display notification (item 1 of argv) with title '
                        '"movescrobble"', "-e", "end run", msg], capture_output=True)


def cmd_plan(a) -> None:
    cfg = cfgmod.load()
    tracks = scan_config(cfg, a.music)
    swim = a.minutes or cfg.minutes
    plan = plan_session(tracks, a.playlist_minutes or cfg.playlist_minutes, a.seed, arc=cfg.arc)
    for p in plan:
        print(f"{p.position:03d}  {fmt_min(p.track.duration)}  {p.track.artist} - {p.track.title}")
    print(f"\n{len(plan)} tracks, {fmt_min(total_seconds(plan))}")
    manifest = to_manifest(plan, a.seed, swim)
    Path(a.out).write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"manifest: {a.out}")
    if a.stage:
        from . import audio
        from .flow import stage_plan

        stage_plan(plan, Path(a.stage), audio.leveller(cfg.loudness))
        print(f"staged {len(plan)} files into {a.stage} (originals untouched)")


def _deliver(scrobbles, a, cfg) -> bool:
    if a.confirm and not confirm(scrobbles):
        print("aborted")
        return False
    if a.submit:
        from . import flow

        try:
            n = flow.send_scrobbles(history.connect(), scrobbles, cfg, listenbrainz_too=a.listenbrainz)
        except ValueError as e:
            raise SystemExit(f"{e}; fix --start") from None
        print(flow.describe_sent(n, scrobbles, cfg, a.listenbrainz))
        return True
    text = to_csv(scrobbles)
    if a.out:
        Path(a.out).write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text)
    return False


def cmd_scrobble(a) -> None:
    cfg = cfgmod.load()
    manifest = json.loads(Path(a.manifest).read_text())
    plan, _ = played_part(manifest, a.played_minutes, parse_positions(a.skip))
    _deliver(build_scrobbles(plan, datetime.fromisoformat(a.start)), a, cfg)


def cmd_scrobble_pending(a) -> None:
    from . import flow, garmin

    cfg = cfgmod.load()
    db = history.connect()
    row = history.pending_session(db)
    if not row:
        raise SystemExit("no pending session")
    now = datetime.now()
    # what you pass wins; otherwise the Garmin swim; without Garmin set up, the plan
    explicit = a.start or a.played_minutes
    find_swim = garmin.finder(cfg)
    swim = None if explicit else flow.lookup_swim(find_swim, row, now, print)
    if find_swim and not explicit and not swim:
        raise SystemExit("no swim on Garmin since this playlist was staged; if you did swim, say when with --start")
    if swim:
        print(f"Garmin: {swim.describe()}")
    settled = flow.settle(json.loads(row["manifest"]), cfg, now, swim, a.played_minutes, parse_positions(a.skip),
                          datetime.fromisoformat(a.start) if a.start else None)
    if _deliver(settled.scrobbles, a, cfg):
        flow.record(db, row, settled)
        flow._set_handled(False)  # lets `on-connect` stage the next session without re-plugging
        if settled.swim and (sync := garmin_sync(cfg, db)):
            try:
                print(f"Garmin: {sync()}")
            except Exception as e:  # scrobbled already; the next on-connect retries the analysis
                print(f"Garmin analysis failed: {e}")
        print("settled; run `movescrobble on-connect` to stage the next session")


def cmd_garmin_auth(a) -> None:
    import getpass

    from . import garmin

    g = garmin.client(ask=input, ask_secret=getpass.getpass)
    print(f"saved Garmin tokens for {g.get_full_name() or 'your account'}")
    if not cfgmod.load().garmin:
        print("now set `garmin = true` in config.toml so swims are read from Garmin")


def _pick_browser(a) -> str:
    from . import ytmauth

    if a.browser:
        return a.browser
    found = ytmauth.installed()
    if not found:
        raise SystemExit("no supported browser found: name one with --browser, or export cookies.txt and use --cookies")
    if len(found) == 1:
        return found[0]
    print("Which browser are you signed in to YouTube Music with?")
    for i, name in enumerate(found, 1):
        print(f"  {i}. {name}")
    answer = input(f"Number [1-{len(found)}]: ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(found):
        raise SystemExit("no browser picked; nothing was read")
    return found[int(answer) - 1]


def cmd_ytm_auth(a) -> None:
    """Sign in to YouTube Music with a browser session: needed to keep the headphones playlist there."""
    from . import ytmauth

    path = cfgmod.load().ytm_auth_file_path
    print("YouTube Music has no login for tools like this, so movescrobble uses your browser's session cookies.")
    print(f"Only the youtube.com ones are kept, in {path}, readable only by you.")
    print("They can act as your Google account on YouTube: never share that file. Delete it to sign out here.\n")
    try:
        if a.cookies:
            cookies = ytmauth.read_file(Path(a.cookies).expanduser())
        else:
            browser = _pick_browser(a)
            if note := ytmauth.WHAT_TO_EXPECT.get(browser):
                print(note)
            cookies = ytmauth.read_browser(browser, a.profile)
        headers = ytmauth.headers_from(cookies, a.account)
        name = ytmauth.account_name(headers)
    except ytmauth.LoginError as e:
        raise SystemExit(str(e)) from None
    ytmauth.save(path, headers)
    print(f"Signed in as {name}. If that is the wrong account, run this again with --account 1 (2, 3...).")
    print("Next: `movescrobble on-connect` with the headphones plugged in creates the playlist.")


def cmd_recap(a) -> None:
    from . import stats

    print("\n".join(stats.recap(history.connect(), a.days).lines()))


def cmd_recs(a) -> None:
    """What the taste graph would bring next, and which of your artists lead there."""
    from rich.console import Console

    from . import affinity, taste
    from .matcher import norm
    from .ui import Busy

    cfg = cfgmod.load()
    db = history.connect()
    tracks, artists = affinity.compute(db, time.time())
    busy = Busy(Console())
    with busy("building your taste graph"):
        t = taste.recommend(db, cfg.lastfm_user, tracks, artists, sorted({x.artist for x in scan_config(cfg)}),
                            progress=busy.update)
    plays = {}
    for name, aff in artists.items():
        plays[norm(name)] = plays.get(norm(name), 0) + aff.total
    print("new to you:")
    new = [x for x in sorted(t.artist_score, key=lambda x: -t.artist_score[x]) if plays.get(x, 0) < taste.KNOWN_PLAYS]
    for x in new[:a.n]:
        print(f"  {t.artist_score[x]:.2f}  {x}" + (f"  (like {', '.join(t.why[x])})" if t.why.get(x) else ""))
    print("\ntracks to try:")
    for artist, title, score in t.recs[:a.n]:
        tag = "new" if plays.get(norm(artist), 0) < taste.KNOWN_PLAYS else "fresh"
        print(f"  {score:.2f}  {artist} - {title}  [{tag}]")


def cmd_block(a) -> None:
    from . import blocklist

    db = history.connect()
    if a.artist:
        if a.remove:
            ok = blocklist.unblock(db, a.artist, a.title or "")
            print(f"unblocked {blocklist.describe(a.artist, a.title)}" if ok else "not on the blocklist")
        else:
            blocklist.block(db, a.artist, a.title or "")
            print(f"blocked {blocklist.describe(a.artist, a.title)}: left out of playlists, the pool and the wishlist")
            return
    rows = blocklist.entries(db)
    print("\n".join(blocklist.describe(*r) for r in rows) if rows else "nothing blocked")


def cmd_lastfm_auth(a) -> None:
    from . import lastfm

    lastfm.authenticate()
    print("saved session")


def cmd_playlist(a) -> None:
    """Show which playlist tracks exist under ~/Music; --download fetches the rest with ymp3's command."""
    from . import acquire, ytm
    from .link import LocalIndex

    cfg = cfgmod.load()
    url = a.url or (cfg.playlists[0] if cfg.playlists else "")
    if not url:
        raise SystemExit("pass a playlist URL or set ytm_playlist in config")
    idx = LocalIndex(scan_config(cfg))
    missing = []
    for e in ytm.fetch(url, cookies=cfg.cookies_path):
        t = idx.find(e.artist, e.title, e.duration)
        print(f"{'found  ' if t else 'MISSING'}  #{e.index:<3} {e.artist} - {e.title}" + (f"  -> {t.path}" if t else ""))
        if not t:
            missing.append(e)
    print(f"\n{len(missing)} missing")
    if a.download and missing:
        ok = acquire.download_missing(cfg, url, missing, ask=input)
        print("done; re-run to verify" if ok else "download skipped/failed")


def run_sync(cfg, db, size: int = 1000, download: str | bool = False, progress=None, log=print):
    """Fetch playlists, check every pool track against your Last.fm history, refresh the pool.

    download: False = report only, "ask" = prompt first, True = fetch missing playlist tracks.
    `progress` gets step updates (the spinner), `log` the outcome lines.
    """
    from . import acquire, lfhistory, libsync, taste, ytm
    from .flow import _quiet

    progress = progress or _quiet

    def sync_history():
        progress("reading your recent plays from Last.fm")
        since = int(time.time()) - cfg.history_days * 86400
        added = lfhistory.sync_history(db, cfg.lastfm_user, log=progress, first_since=since)
        log(f"Last.fm history: read {added} new plays of yours (only read, to know your taste; nothing scrobbled)")

    def dl(missing):
        ask = input if download == "ask" else None
        for url in dict.fromkeys(e.playlist for e in missing):
            ok = acquire.download_missing(cfg, url, [e for e in missing if e.playlist == url], ask=ask,
                                          progress=progress)
            log("download finished" if ok else "download skipped/failed")

    report = libsync.sync_library(
        cfg, db, size=size,
        fetch_playlist=lambda url: ytm.fetch(url, cookies=cfg.cookies_path),
        history_sync=sync_history,
        taste_fetch=lambda user, tracks, artists, lib: taste.recommend(db, user, tracks, artists, lib, progress=progress),
        download=dl if download else None,
        fetch_wishes=(lambda w: acquire.fetch_wishes(cfg, db, w, cfg.auto_fetch, ask=input if download == "ask" else None,
                                                     progress=progress))
        if (download and cfg.auto_fetch) else None,
    )
    history.set_meta(db, "last_sync", time.time())
    db.commit()
    return report


def cmd_sync(a) -> None:
    import subprocess
    from urllib.parse import quote_plus

    from . import acquire

    cfg = cfgmod.load()
    rep = run_sync(cfg, history.connect(), a.size, download=("ask" if not a.yes else True) if a.download else False)
    p = rep.pool
    print(f"pool: +{len(p.added)} -{len(p.retired)}; playlist {rep.playlist_total - len(rep.playlist_missing)}/"
          f"{rep.playlist_total} found locally")
    for e in rep.playlist_missing:
        print(f"  not downloaded yet: #{e.index} {e.artist} - {e.title}")
    if p.missing:
        wish = cfgmod.config_dir() / "wishlist.txt"
        wish.parent.mkdir(parents=True, exist_ok=True)
        wish.write_text("".join(
            f"{c.artist} - {c.title}\t[{','.join(sorted(c.sources))}]\thttps://music.youtube.com/search?q="
            f"{quote_plus(c.artist + ' ' + c.title)}\n" for c in p.missing))
        print(f"\n{len(p.missing)} tracks you play/should like but have no file for -> add them to your playlist:")
        for c in p.missing[:15]:
            print(f"  [{','.join(sorted(c.sources))}] {c.artist} - {c.title}")
        print(f"full list with YouTube Music search links: {wish}")
    if a.fetch_missing and p.missing:
        for c in p.missing[: a.fetch_missing]:
            subprocess.run(acquire.playlist_download_command(cfg, f"ytsearch1:{c.artist} {c.title}"))
        print("fetched; re-run sync to add them to the pool")


def cmd_mix(a) -> None:
    from . import audio, flow

    cfg = cfgmod.load()
    db = history.connect()
    swim = a.minutes or cfg.minutes
    plan = flow.build_plan(db, cfg, scan_config(cfg, a.music), a.seed, workout=a.workout,
                           minutes=a.playlist_minutes, swim_minutes=swim, min_energy=a.min_energy)
    n_scrobbles = db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"]
    if cfg.lastfm_user and not n_scrobbles:
        print("! no Last.fm history synced yet: every track counts as discovery. Run `movescrobble sync` first.\n")
    elif db.execute("SELECT 1 FROM pool WHERE retired=0 AND source IN ('top','loved','playlist','similar') LIMIT 1").fetchone():
        print("! the pool was built by an older version: run `movescrobble sync` to rebuild it.\n")
    for p in plan:
        print(f"{p.position:03d}  {fmt_min(p.track.duration)}  e={p.energy:.2f}  {p.source:9}  "
              f"{p.track.artist} - {p.track.title}")
    mixes = Counter(p.source for p in plan)
    print(f"\n{len(plan)} tracks, {fmt_min(total_seconds(plan))}  " + "  ".join(f"{b}={n}" for b, n in mixes.most_common()))
    Path(a.out).write_text(json.dumps(to_manifest(plan, a.seed, swim), indent=2, ensure_ascii=False))
    if a.stage:
        flow.stage_plan(plan, Path(a.stage), audio.leveller(cfg.loudness))
        print(f"staged {len(plan)} files into {a.stage}")


def cmd_eval(a) -> None:
    """Replay your history: how well do the scoring models predict what you played next?"""
    from . import evaluate

    db = history.connect()
    if not db.execute("SELECT 1 FROM scrobbles LIMIT 1").fetchone():
        raise SystemExit("no Last.fm history yet: run `movescrobble sync` first")
    print("Repeat listening: rank tracks played in the last 2 years, hit = played in the next 30 days")
    repeat = evaluate.evaluate_models(db, evaluate.REPEAT_MODELS)
    for name, (rec, ndcg, n) in sorted(repeat.items(), key=lambda kv: -kv[1][1]):
        print(f"  {name:32s} recall@100 {rec:.3f}  ndcg@100 {ndcg:.3f}  ({n} cutoffs)")
    print("\nLost gems: tracks with 4+ plays silent for 90+ days, hit = comes back within 30 days")
    gems = evaluate.evaluate_models(db, evaluate.GEM_MODELS, gems=True)
    for name, (prec, ndcg, n) in sorted(gems.items(), key=lambda kv: -kv[1][1]):
        print(f"  {name:32s} precision@50 {prec:.3f}  ndcg@50 {ndcg:.3f}  ({n} cutoffs)")
    print("\nNew-to-you tracks per 30 days, by how well you already knew the artist:")
    for name, v in evaluate.new_track_artist_coverage(db).items():
        print(f"  {name:36s} {v:.0%}")


def cmd_skip(a) -> None:
    """The staged session was never swum: forget it without scrobbling anything."""
    from . import flow

    db = history.connect()
    sid = history.discard_pending(db)
    flow._set_handled(False)
    print(f"discarded session {sid}; nothing was scrobbled" if sid else "no pending session")


def cmd_reroll(a) -> None:
    """Replace the staged playlist on the connected headphones with a new one."""
    from . import flow

    cfg = cfgmod.load()
    db = history.connect()
    if a.sync:
        rep = run_sync(cfg, db, download=False)
        print(f"pool refreshed: +{len(rep.pool.added)} -{len(rep.pool.retired)}")
    try:
        m = flow.reroll(cfg, db, a.seed, workout=a.workout)
    except RuntimeError as e:
        raise SystemExit(str(e)) from None
    flow._set_handled(True)
    mins = sum(t["duration"] for t in m["tracks"]) / 60
    mixes = Counter(t["source"] for t in m["tracks"])
    print(f"staged new session {m['id']}: {len(m['tracks'])} tracks, {mins:.0f} min  "
          + "  ".join(f"{b}={n}" for b, n in mixes.most_common()))


def cmd_status(a) -> None:
    cfg = cfgmod.load()
    db = history.connect()
    row = history.pending_session(db)
    if row:
        m = json.loads(row["manifest"])
        age = (time.time() - row["planned_at"]) / 3600
        mins = sum(t["duration"] for t in m["tracks"]) / 60
        print(f"staged session {row['id']}: {len(m['tracks'])} tracks, {mins:.0f} min, staged {age:.1f} h ago "
              f"(swim {m.get('minutes', cfg.minutes):.0f} min counts when scrobbled)")
    else:
        print("no staged session waiting to be scrobbled")
    n = db.execute("SELECT COUNT(*) c FROM scrobbles").fetchone()["c"]
    pool = db.execute("SELECT source, COUNT(*) c FROM pool WHERE retired=0 GROUP BY source ORDER BY c DESC").fetchall()
    print(f"Last.fm history: {n} scrobbles; pool: " + (", ".join(f"{r['source']}={r['c']}" for r in pool) or "empty"))
    recent = len(history.recent_plays(db, cfg.no_repeat_days))
    print(f"swum in the last {cfg.no_repeat_days} days (skipped for now): {recent} tracks")
    print(f"device {cfg.device_name}: " + ("connected" if cfg.device_path.is_dir() else "not connected"))


def cmd_shell(a) -> None:
    from rich.console import Console

    from . import keys, shell
    from .ui import Busy, KeyUI

    cfg = cfgmod.load()
    db = history.connect()
    console = Console(highlight=False)
    plain = os.environ.get("MOVESCROBBLE_PLAIN") or getattr(a, "plain", False)
    ui = KeyUI(console) if keys.available() and not plain else None
    busy = Busy(console)
    shell.Shell(cfg, db, shell.real_actions(cfg, db, busy.update), console, keys=ui, busy=busy).run()


def cmd_podcasts(a) -> None:
    from . import pocketcasts

    cfg = cfgmod.load()
    db = history.connect()
    if a.done:
        done = [e for i, e in enumerate(pocketcasts.on_device(db), 1) if i in parse_positions(a.done)]
        pocketcasts.mark_played(done)
        print(pocketcasts.describe_marked(done))
    if a.dest:
        print(pocketcasts.sync_latest(Path(a.dest), db, cfg.max_podcasts, speed=cfg.podcast_speed,
                                      trim_silence=cfg.podcast_trim_silence))
    elif cfg.device_path.is_dir():
        print(podcast_sync(cfg, db, only_if_logged_in=False)())
    else:  # episodes only live on the headphones: no local copy to fall back to
        raise SystemExit(f"device {cfg.device_name} not connected (or pass --dest)")


def podcast_sync(cfg, db, only_if_logged_in: bool = True, progress=None):
    """The on-connect podcast step, or None when Pocket Casts was never set up (automatic runs must not prompt)."""
    from . import pocketcasts
    from .flow import _quiet, music_reserve_mb

    if only_if_logged_in and not pocketcasts.saved_token():
        return None
    return lambda: pocketcasts.sync_latest(cfg.device_path / cfg.device_podcast_subdir, db, cfg.max_podcasts,
                                           speed=cfg.podcast_speed, trim_silence=cfg.podcast_trim_silence,
                                           reserve_mb=music_reserve_mb(cfg, db), progress=progress or _quiet)


def garmin_sync(cfg, db):
    """Time a just-scrobbled swim's tracks, write its soundtrack; None when Garmin is off or not authorised."""
    from . import garmin, power

    return (lambda: power.sync(db, cfg)) if garmin.finder(cfg) else None


def activity_sync(cfg, db, days: int = 60, run_days: int = 30) -> list[str]:
    """What the watch recorded, headphones or not: a swim settles the staged playlist, runs get their soundtrack."""
    from . import flow, garmin, power, runs

    lines: list[str] = []

    def step(name: str, fn) -> None:
        try:  # each works without the others; Garmin or Last.fm being down is reported, the next sync catches up
            lines.append(f"{name}: {fn()}")
        except Exception as e:
            lines.append(f"{name} FAILED: {e}")

    step("swim", lambda: flow.settle_swim(db, cfg, datetime.now(), garmin.swim_between, lines.append))
    step("swim timing", lambda: power.sync(db, cfg, days=days))
    if cfg.garmin_run_soundtrack:
        step("runs", lambda: runs.sync(db, cfg, days=run_days))
    return lines


def cmd_garmin_sync(a) -> None:
    from . import garmin

    cfg = cfgmod.load()
    if not garmin.authorised():
        raise SystemExit("run `movescrobble garmin-auth` first")
    print("\n".join(activity_sync(cfg, history.connect(), a.days, a.run_days)))


def cmd_on_connect(a) -> None:
    from . import flow

    cfg = cfgmod.load()
    db = history.connect()

    def pool_sync():
        if not (cfg.lastfm_user or cfg.playlists):
            return "skipped (no lastfm_user / ytm_playlist configured)"
        rep = run_sync(cfg, db, download=cfg.auto_download and bool(cfg.playlists))
        left = len(rep.playlist_missing)
        return f"+{len(rep.pool.added)} -{len(rep.pool.retired)}, {left} playlist tracks still missing"

    for line in flow.on_connect(cfg, db, notify=notify, podcast_sync=podcast_sync(cfg, db), pool_sync=pool_sync):
        print(line)


def cmd_agent(a) -> None:
    from . import launchd

    if a.action == "install":
        print(f"installed {launchd.install(os.getuid())}")
    else:
        launchd.uninstall(os.getuid())
        print("uninstalled")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="movescrobble")
    sub = ap.add_subparsers()

    p = sub.add_parser("plan", help="build a randomised session")
    p.add_argument("music", nargs="?")
    p.add_argument("--minutes", type=float, help="swim length (what gets scrobbled)")
    p.add_argument("--playlist-minutes", type=float, help="staged playlist length (default 120)")
    p.add_argument("--seed", type=int)
    p.add_argument("--out", default="session.json")
    p.add_argument("--stage", help="copy files here with position prefixes")
    p.set_defaults(func=cmd_plan)

    def scrobble_opts(x, start_required):
        x.add_argument("--start", required=start_required, help="session start, ISO e.g. 2026-10-25T07:30")
        x.add_argument("--played-minutes", type=float, help="stopped early: only count what was heard")
        x.add_argument("--skip", help="comma-separated positions you skipped, e.g. 3,7")
        x.add_argument("--confirm", action="store_true", help="show the list and ask first")
        x.add_argument("--submit", action="store_true", help="send to Last.fm (default: print CSV)")
        x.add_argument("--listenbrainz", action="store_true")
        x.add_argument("--out")

    s = sub.add_parser("scrobble", help="scrobble a session manifest")
    s.add_argument("manifest")
    scrobble_opts(s, True)
    s.set_defaults(func=cmd_scrobble)

    sp = sub.add_parser("scrobble-pending", help="scrobble the last staged session")
    scrobble_opts(sp, False)
    sp.set_defaults(func=cmd_scrobble_pending)

    mx = sub.add_parser("mix", help="energising favourites + a few recommendations (uses pool, energy analysis)")
    mx.add_argument("music", nargs="?")
    mx.add_argument("--minutes", type=float, help="swim length (what gets scrobbled)")
    mx.add_argument("--playlist-minutes", type=float, help="staged playlist length (default 120)")
    mx.add_argument("--seed", type=int)
    mx.add_argument("--min-energy", type=float)
    mx.add_argument("--workout", choices=sorted(WORKOUTS))
    mx.add_argument("--out", default="session.json")
    mx.add_argument("--stage")
    mx.set_defaults(func=cmd_mix)

    sub.add_parser("skip", help="discard the staged session without scrobbling (you did not swim)").set_defaults(func=cmd_skip)

    rr = sub.add_parser("reroll", help="replace the staged playlist with a fresh one")
    rr.add_argument("--seed", type=int)
    rr.add_argument("--sync", action="store_true", help="refresh the pool from Last.fm first")
    rr.add_argument("--workout", choices=sorted(WORKOUTS), help="energy curve for this swim (default: config)")
    rr.set_defaults(func=cmd_reroll)

    sh = sub.add_parser("shell", help="interactive shell (also: run with no arguments)")
    sh.add_argument("--plain", action="store_true", help="line input instead of arrow-key menus")
    sh.set_defaults(func=cmd_shell)

    sub.add_parser("status", help="what is staged, pool and history sizes").set_defaults(func=cmd_status)

    sub.add_parser("eval", help="replay your Last.fm history to evaluate the scoring models").set_defaults(func=cmd_eval)

    sub.add_parser("lastfm-auth", help="authorise on Last.fm").set_defaults(func=cmd_lastfm_auth)
    ya = sub.add_parser("ytm-auth", help="sign in to YouTube Music using a browser you are already signed in to")
    ya.add_argument("--browser", help="firefox, chrome, brave, edge, vivaldi, opera or safari (default: ask)")
    ya.add_argument("--profile", help="browser profile, if not the default one")
    ya.add_argument("--account", type=int, default=0, help="which Google account, with several signed in (0 = first)")
    ya.add_argument("--cookies", help="use a cookies.txt exported from a signed-in browser instead")
    ya.set_defaults(func=cmd_ytm_auth)
    rc = sub.add_parser("recap", help="swims, minutes, discoveries over the last days")
    rc.add_argument("--days", type=int, default=7)
    rc.set_defaults(func=cmd_recap)
    sub.add_parser("garmin-auth", help="log in to Garmin Connect (swims and runs)").set_defaults(func=cmd_garmin_auth)
    gs = sub.add_parser("garmin-sync", help="what the watch recorded: a swim scrobbles the playlist, runs get their tracks")
    gs.add_argument("--days", type=int, default=60)
    gs.add_argument("--run-days", type=int, default=30)
    gs.set_defaults(func=cmd_garmin_sync)
    rs = sub.add_parser("recs", help="what the taste graph recommends next, and why")
    rs.add_argument("-n", type=int, default=25)
    rs.set_defaults(func=cmd_recs)
    bl = sub.add_parser("block", help="never play an artist (or one track); no arguments lists the blocklist")
    bl.add_argument("artist", nargs="?")
    bl.add_argument("title", nargs="?", help="only this track (default: every track by the artist)")
    bl.add_argument("--remove", action="store_true", help="take it off the blocklist")
    bl.set_defaults(func=cmd_block)

    pl = sub.add_parser("playlist", help="match a YouTube Music playlist to local files")
    pl.add_argument("url", nargs="?")
    pl.add_argument("--download", action="store_true", help="offer to download missing tracks")
    pl.set_defaults(func=cmd_playlist)

    sy = sub.add_parser("sync", help="refresh the swim pool from playlist + Last.fm taste")
    sy.add_argument("--size", type=int, default=1000)
    sy.add_argument("--download", action="store_true", help="download playlist tracks missing locally (asks first)")
    sy.add_argument("--yes", action="store_true", help="with --download: do not ask")
    sy.add_argument("--fetch-missing", type=int, default=0, metavar="N",
                    help="offer to download up to N wanted-but-missing tracks")
    sy.set_defaults(func=cmd_sync)

    po = sub.add_parser("podcasts", help="mirror the newest unplayed episode of each Pocket Casts subscription")
    po.add_argument("--dest", help="folder to mirror to instead of the headphones (episodes stay there)")
    po.add_argument("--done", metavar="1,3", help="mark these episodes (number on the headphones) played first")
    po.set_defaults(func=cmd_podcasts)

    sub.add_parser("on-connect", help="the automatic flow (run by launchd)").set_defaults(func=cmd_on_connect)

    ag = sub.add_parser("agent", help="install/uninstall the launchd agent")
    ag.add_argument("action", choices=["install", "uninstall"])
    ag.set_defaults(func=cmd_agent)

    args = ap.parse_args(argv)
    if not hasattr(args, "func"):  # bare `movescrobble`: the shell on a terminal, the help otherwise
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_shell(args)
        return ap.print_help()
    args.func(args)
