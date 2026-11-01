"""Interactive shell: dashboard, stats, playlist editing, reroll, sync, scrobble, agent.

Run `swimscrobble` with no arguments. Every screen is a method; all slow or system-touching actions
(Last.fm, YouTube, the launchd agent) go through `Actions`, so the whole thing runs against fakes in tests.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import blocklist, editing, flow, history, stats
from .config import Config
from .library import Track
from .link import LocalIndex
from .matcher import norm
from .planner import NEW_TO_YOU, fmt_min, parse_positions
from .workouts import WORKOUTS
from .ui import Busy, Item, KeyUI

COLORS = {"obsession": "magenta", "mid": "cyan", "lost": "yellow", "fresh": "green", "rec": "bright_blue", "added": "white"}
HELP = """\
[bold]p[/] playlist      view the staged playlist (and edit it: d 3,5 delete · m 7 2 move · a add · b 4 block)
[bold]b[/] blocklist     artists and tracks you never want to hear (block, unblock)
[bold]r[/] reroll        replace the staged playlist with a new one
[bold]a[/] add track     search your library (or YouTube Music) and put a track into the playlist
[bold]s[/] stats         listening, pool and swim statistics
[bold]w[/] wishlist      tracks you play or Last.fm suggests but you do not own; download them
[bold]y[/] sync          refresh Last.fm history and the pool; download missing tracks
[bold]c[/] scrobble      settle the last swim (how long, what you skipped) and send it to Last.fm
[bold]k[/] skip          forget the staged playlist: you did not swim
[bold]u[/] / [bold]x[/]        use / remove staged files left on the headphones by a discarded session
[bold]v[/] recap         the last 7 days of swimming
[bold]o[/] podcasts      newest unplayed episode per subscription to the headphones
[bold]e[/] on-connect    run the whole automatic routine now (settle, sync, stage)
[bold]g[/] agent         install / remove the launchd agent that runs the routine when you plug in
[bold]q[/] quit"""


@dataclass
class Actions:
    """Everything that talks to the network, the disk-heavy analysers or the OS."""

    load_library: Callable[[], list[Track]]
    features: Callable[[Track], object]
    sync: Callable[[bool], str]  # download? -> summary
    search_youtube: Callable[[str], list[dict]]
    download_video: Callable[[str], bool]
    find_song: Callable[[str, str], object]  # artist, title -> Entry | None
    podcasts: Callable[[], str]
    on_connect: Callable[[], list[str]]
    submit: Callable[[list], str]
    agent_installed: Callable[[], bool]
    agent_set: Callable[[bool], None]
    find_swim: Callable[[datetime, datetime], object] | None = None  # Garmin swim in the window, if set up
    podcast_episodes: Callable[[], list[dict]] = list  # what the last podcast sync put on the headphones
    mark_played: Callable[[list[dict]], str] = lambda eps: ""  # tell Pocket Casts, then sync again
    love: Callable[[list[tuple[str, str]]], str] = lambda tracks: ""  # Last.fm love (+ your playlist if authorised)
    garmin_sync: Callable[[], str] | None = None  # time the scrobbled swim's tracks, write its soundtrack


class Shell:
    def __init__(self, cfg: Config, db, actions: Actions, console: Console | None = None,
                 ask: Callable[[str], str] = input, keys: KeyUI | None = None, busy: Busy | None = None):
        self.cfg, self.db, self.act = cfg, db, actions
        self.keys = keys  # arrow-key widgets; None = plain line input (pipes, tests)
        self.console = console or Console(highlight=False)
        self.busy = busy or Busy(self.console)  # spinner around every slow call; real_actions report into it
        self._ask = ask
        self._library: list[Track] | None = None

    # -- plumbing ---------------------------------------------------------------------------------
    def ask(self, prompt: str, default: str = "") -> str:
        try:
            answer = self._ask(f"{prompt}{f' [{default}]' if default else ''} ").strip()
        except EOFError:
            raise SystemExit(0) from None
        return answer or default

    _said = 0

    def say(self, *a, **kw) -> None:
        self._said += 1
        self.console.print(*a, **kw)

    def yes(self, prompt: str, default: bool = False) -> bool:
        if self.keys:
            return self.keys.confirm(prompt, default)
        a = self.ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
        return default if a == "" else a.startswith("y")

    def library(self, refresh: bool = False) -> list[Track]:
        if self._library is None or refresh:
            with self.busy("scanning the library"):
                self._library = self.act.load_library()
        return self._library

    def session(self):
        row = history.pending_session(self.db)
        return (row, json.loads(row["manifest"])) if row else (None, None)

    # -- dashboard --------------------------------------------------------------------------------
    def dashboard(self) -> None:
        d = stats.dashboard(self.cfg, self.db, agent_installed=self.act.agent_installed())
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        dev = (f"[green]connected[/] · {d.device_free_gb:.1f} GB free" if d.device_connected
               else "[red]not connected[/]")
        t.add_row("Headphones", f"{self.cfg.device_name} {dev}")
        if d.staged:
            s = d.staged
            ranked = sorted(s["buckets"].items(), key=lambda kv: -kv[1])
            mix = "  ".join(f"[{COLORS.get(b, 'white')}]{b} {n}[/]" for b, n in ranked)
            t.add_row("Staged", f"session {s['id']}: {s['tracks']} tracks, {s['minutes']:.0f} min, "
                                f"staged {s['age_hours']:.1f} h ago")
            t.add_row("", f"{s['swim_tracks']} tracks fill your {s['swim_minutes']:.0f}-min swim · {mix}")
        elif d.orphans:
            t.add_row("Staged", f"[yellow]none tracked[/], but {d.orphans} tracks of discarded session {d.orphan_session} "
                                "are still on the headphones and will play")
            t.add_row("", "[dim]u use them as the current playlist · x remove them · r replace them with a new one[/]")
        else:
            t.add_row("Staged", "[dim]nothing waiting: press r to stage a playlist[/]")
        sync = "never" if d.last_sync_hours is None else f"{d.last_sync_hours:.1f} h ago"
        t.add_row("Library", f"{sum(d.pool.values())} tracks in the pool · wishlist {d.wishlist} · synced {sync}")
        t.add_row("History", f"{d.scrobbles:,} scrobbles ({d.scrobbles_30d:,} in 30 days) · {d.swum_recently} swum in the last "
                             f"{self.cfg.no_repeat_days} days (skipped)")
        t.add_row("Agent", "[green]installed[/] (runs when you plug in)" if d.agent_installed else "[dim]not installed (g)[/]")
        self.say(Panel(t, title="[bold]swimscrobble[/]", border_style="cyan"))
        self.say(f"[dim]{self.hint(d)}[/]")

    def hint(self, d) -> str:
        if d.staged and d.staged["age_hours"] * 60 >= self.cfg.min_swim_gap_minutes and d.device_connected:
            return "Next: swimming? plug in afterwards and press c to scrobble it · r re-rolls the playlist"
        if not d.staged:
            return "Next: r stages a playlist on the headphones" if d.device_connected else "Next: connect the headphones, then r"
        return "Keys: p playlist · r reroll · a add track · s stats · w wishlist · y sync · c scrobble · ? help · q quit"

    # -- screens ----------------------------------------------------------------------------------
    def playlist(self) -> None:
        if self.keys:
            return self.playlist_keys()
        while True:
            row, m = self.session()
            if not row:
                self.say("[yellow]No staged playlist.[/] Press r to create one.")
                return
            tracks = sorted(m["tracks"], key=lambda t: t["position"])
            swim = m.get("minutes", self.cfg.minutes) * 60
            table = Table(title=f"Session {row['id']} · {len(tracks)} tracks", header_style="bold", show_edge=False)
            cols = (("#", "right"), ("at", "right"), ("len", "right"), ("energy", "right"), ("bucket", "left"),
                    ("track", "left"))
            for h, j in cols:
                table.add_column(h, justify=j)
            t, marked = 0.0, False
            for tr in tracks:
                if not marked and t >= swim:
                    table.add_row("", "", "", "", "", f"[dim]── {swim / 60:.0f} min: end of your swim, buffer below ──[/]")
                    marked = True
                b = tr.get("source", "")
                e = tr.get("energy")
                table.add_row(str(tr["position"]), fmt_min(t), fmt_min(tr["duration"]), f"{e:.2f}" if e else "",
                              f"[{COLORS.get(b, 'white')}]{b}[/]", f"{tr['artist']} - {tr['title']}")
                t += tr["duration"]
            self.say(table)
            cmd = self.ask("[playlist] d 3,5 delete · m 7 2 move · a add · b 4 block · r reroll · Enter back >").split()
            if not cmd:
                return
            try:
                if cmd[0] == "d" and len(cmd) > 1:
                    pos = {int(x) for x in cmd[1].split(",")}
                    editing.remove_tracks(self.cfg, self.db, pos)
                    self.say(f"[green]removed {len(pos)}[/]")
                elif cmd[0] == "m" and len(cmd) == 3:
                    editing.move_track(self.cfg, self.db, int(cmd[1]), int(cmd[2]))
                    self.say("[green]moved[/]")
                elif cmd[0] == "b" and len(cmd) == 2:
                    self.block_track(int(cmd[1]), tracks[int(cmd[1]) - 1])
                elif cmd[0] == "a":
                    self.add_track()
                elif cmd[0] == "r":
                    self.reroll()
                else:
                    self.say("[yellow]unknown command[/]")
            except _Cancel:
                pass
            except IndexError:
                self.say("[yellow]no track at that position[/]")
            except (ValueError, RuntimeError) as e:
                self.say(f"[red]{e}[/]")

    def playlist_keys(self) -> None:
        """Cursor-driven editor: Enter picks a track up, arrows move it, Enter drops it; d deletes."""
        cursor = 0
        while True:
            row, m = self.session()
            if not row:
                self.say("[yellow]No staged playlist.[/] Press r to create one.")
                return
            order = sorted(m["tracks"], key=lambda t: t["position"])
            swim = m.get("minutes", self.cfg.minutes) * 60
            state = {"grab": None}  # origin index of the track being carried

            def render(idx, order=order, swim=swim, row=row, state=state):
                table = Table(title=f"Session {row['id']} · {len(order)} tracks", header_style="bold", show_edge=False)
                for h, j in (("", "left"), ("#", "right"), ("at", "right"), ("len", "right"), ("energy", "right"),
                             ("bucket", "left"), ("track", "left")):
                    table.add_column(h, justify=j)
                t, marked = 0.0, False
                for i, tr in enumerate(order):
                    if not marked and t >= swim:
                        table.add_row("", "", "", "", "", "",
                                      f"[dim]── {swim / 60:.0f} min: end of your swim, buffer below ──[/]")
                        marked = True
                    b, e = tr.get("source", ""), tr.get("energy")
                    carried = state["grab"] is not None and i == idx
                    style = "bold black on yellow" if carried else "bold reverse" if i == idx else ""
                    cell = lambda x, style=style: Text(str(x), style=style)  # noqa: E731
                    table.add_row(cell("▸" if i == idx else " "), cell(i + 1), cell(fmt_min(t)), cell(fmt_min(tr["duration"])),
                                  cell(f"{e:.2f}" if e else ""), cell(b), cell(f"{tr['artist']} - {tr['title']}"))
                    t += tr["duration"]
                hint = ("↑/↓ carry it · Enter drop here · Esc cancel" if state["grab"] is not None
                        else "↑/↓ move · Enter pick up · d delete · b block · a add · r reroll · q back")
                return Group(table, Text(hint, style="dim"))

            def on_move(old, new, order=order, state=state):
                if state["grab"] is not None:  # the carried track follows the cursor
                    order.insert(new, order.pop(old))

            def on_key(key, idx, order=order, state=state):
                if key in ("enter", "space"):
                    if state["grab"] is None:
                        state["grab"] = idx
                        return idx, None
                    return idx, ("drop", state["grab"] + 1, idx + 1)
                if key == "esc" or key == "q":
                    return idx, ("reload",) if state["grab"] is not None else ("quit",)
                if state["grab"] is None and key in ("d", "a", "r", "b"):
                    return idx, ({"d": "delete", "a": "add", "r": "reroll", "b": "block"}[key], idx + 1)
                return idx, None

            action = self.keys._loop(render, len(order), on_key, cursor, on_move)
            kind = action[0]
            try:
                if kind == "quit":
                    return
                if kind == "drop":
                    if action[1] != action[2]:
                        editing.move_track(self.cfg, self.db, action[1], action[2])
                    cursor = action[2] - 1
                elif kind == "delete":
                    t = order[action[1] - 1]
                    if self.yes(f"Delete {t['artist']} - {t['title']}?", default=True):
                        editing.remove_tracks(self.cfg, self.db, {action[1]})
                    cursor = max(0, min(action[1] - 1, len(order) - 2))
                elif kind == "block":
                    self.block_track(action[1], order[action[1] - 1])
                    cursor = max(0, min(action[1] - 1, len(order) - 2))
                elif kind == "add":
                    self.add_track()
                    cursor = 0
                elif kind == "reroll":
                    self.reroll(show=False)
                    cursor = 0
            except _Cancel:
                pass
            except (ValueError, RuntimeError) as e:
                self.say(f"[red]{e}[/]")

    def block_track(self, position: int, track: dict) -> None:
        """Never play this track (or its artist) again, and take it (all of them) out of the staged playlist now."""
        artist, title = track["artist"], track["title"]
        choices = [f"Only this track: {artist} - {title}", f"Every track by {artist}"]
        if self.keys:
            pick = self.keys.select(choices, title="Block")
        else:
            a = self.ask(f"Block 1) only this track  2) every track by {artist}  (Enter cancels)")
            pick = int(a) - 1 if a in ("1", "2") else None
        if pick is None:
            raise _Cancel
        blocklist.block(self.db, artist, "" if pick == 1 else title)
        _, m = self.session()
        blocked = blocklist.Blocklist.load(self.db)
        gone = {t["position"] for t in m["tracks"] if blocked.blocks(t["artist"], t["title"])} | {position}
        editing.remove_tracks(self.cfg, self.db, gone)
        self.say(f"[green]blocked {blocklist.describe(artist, '' if pick == 1 else title)}[/]: removed {len(gone)} from "
                 "the playlist, never picked again")

    def blocklist(self) -> None:
        rows = blocklist.entries(self.db)
        if rows:
            for i in self.pick_many([blocklist.describe(*r) for r in rows], "Blocked: tick any to unblock"):
                blocklist.unblock(self.db, *rows[i])
                self.say(f"[green]unblocked[/] {blocklist.describe(*rows[i])}")
        else:
            self.say("[dim]Nothing blocked yet.[/]")
        artist = self.ask("Block an artist (exact name, Enter = done)")
        if artist:
            blocklist.block(self.db, artist)
            self.say(f"[green]blocked {blocklist.describe(artist)}[/]: never picked again (re-roll to drop them now)")

    def reroll(self, show: bool = True) -> None:
        if not self.cfg.device_path.is_dir():
            self.say(f"[red]{self.cfg.device_name} is not connected.[/]")
            return
        row, _ = self.session()
        if row and not self.yes("Replace the staged playlist? It will not be scrobbled", default=True):
            return
        if self.yes("Refresh Last.fm history and the pool first? (slower)", default=False):
            with self.busy("syncing Last.fm history and the pool"):
                self.say(f"[dim]{self.act.sync(False)}[/]")
            self._library = None
        workout = self.pick_workout()
        seed = self.ask("Seed (blank = random)")
        try:
            with self.busy("building the playlist"):
                m = flow.reroll(self.cfg, self.db, int(seed) if seed.isdigit() else None,
                                library_loader=lambda _: self.library(), feature_of=self.act.features, workout=workout,
                                progress=self.busy.update)
        except RuntimeError as e:
            self.say(f"[red]{e}[/]")
            return
        flow._set_handled(True)
        self.say(f"[green]staged session {m['id']}:[/] {len(m['tracks'])} tracks, "
                 f"{sum(t['duration'] for t in m['tracks']) / 60:.0f} min")
        if show:
            self.playlist()

    def pick_workout(self) -> str:
        names = list(WORKOUTS)
        default = self.cfg.workout
        if self.keys:
            i = self.keys.select([f"{n:10} {WORKOUTS[n].help}" for n in names], title="Workout")
            return names[i] if isinstance(i, int) else default
        answer = self.ask("Workout (" + " / ".join(names) + ")", default).lower()
        return answer if answer in WORKOUTS else default

    def search_local(self, query: str, limit: int = 10) -> list[Track]:
        words = norm(query).split()
        hits = [t for t in self.library() if all(w in norm(f"{t.artist} {t.title}") for w in words)]
        return sorted(hits, key=lambda t: (t.artist.casefold(), t.title.casefold()))[:limit]

    def add_track(self) -> None:
        row, m = self.session()
        if not row:
            self.say("[yellow]No staged playlist to add to.[/] Press r first.")
            return
        q = self.ask("Search (artist / title)")
        if not q:
            return
        track = self.pick_local(q) or self.pick_youtube(q)
        if track is None:
            return
        n_swim, _ = stats.swim_split(m, m.get("minutes", self.cfg.minutes))
        pos = self.ask(f"Position 1-{len(m['tracks']) + 1} (1 plays first; Enter = {n_swim}, the end of your swim)", str(n_swim))
        with self.busy("measuring the track's energy"):
            feat = self.act.features(track)
        if feat.energy < self.cfg.min_energy and not self.yes(
                f"Energy {feat.energy:.2f} is below your minimum {self.cfg.min_energy}: a slow track. Add anyway?"):
            return
        try:
            with self.busy("levelling and copying the track"):
                editing.add_track(self.cfg, self.db, track, int(pos) if pos.isdigit() else n_swim, round(feat.energy, 2))
        except (ValueError, RuntimeError) as e:
            self.say(f"[red]{e}[/]")
            return
        self.say(f"[green]added[/] {track.artist} - {track.title} at position {pos}")

    def pick_local(self, query: str) -> Track | None:
        hits = self.search_local(query)
        if self.keys:
            r = self.keys.select([f"{h.artist} - {h.title}  ({fmt_min(h.duration)})" for h in hits],
                                 title=f"{len(hits)} matches in your library" if hits else "nothing in your library matches",
                                 extra=[Item("y", "Search YouTube Music instead")])
            if r == "y":
                return None
            if r is None:
                raise _Cancel
            return hits[r]
        if hits:
            t = Table(show_edge=False, header_style="bold")
            for h in ("#", "artist", "title", "len"):
                t.add_column(h)
            for i, h in enumerate(hits, 1):
                t.add_row(str(i), h.artist, h.title, fmt_min(h.duration))
            self.say(t)
        else:
            self.say("[dim]nothing in your library matches[/]")
        a = self.ask("Number to pick · y = search YouTube Music · Enter cancels")
        if a.isdigit() and 1 <= int(a) <= len(hits):
            return hits[int(a) - 1]
        if a.lower() == "y":
            return None
        raise _Cancel

    def pick_youtube(self, query: str) -> Track | None:
        with self.busy("searching YouTube Music"):
            results = self.act.search_youtube(query)
        if not results:
            self.say("[yellow]no results[/]")
            return None
        if self.keys:
            rows = [f"{', '.join(a['name'] for a in r.get('artists') or [])} - {r.get('title', '')}  "
                    f"[{(r.get('album') or {}).get('name', '')}]  {fmt_min(r.get('duration_seconds') or 0)}" for r in results]
            pick = self.keys.select(rows, title="YouTube Music: Enter downloads and uses it")
            a = str(pick + 1) if isinstance(pick, int) else ""
        else:
            a = None
        t = Table(show_edge=False, header_style="bold")
        for h in ("#", "artist", "title", "album", "len"):
            t.add_column(h)
        for i, r in enumerate(results, 1):
            t.add_row(str(i), ", ".join(a["name"] for a in r.get("artists") or []), r.get("title", ""),
                      (r.get("album") or {}).get("name", ""), fmt_min(r.get("duration_seconds") or 0))
        if a is None:
            self.say(t)
            a = self.ask("Number to download and use · Enter cancels")
        if not (a.isdigit() and 1 <= int(a) <= len(results)):
            return None
        r = results[int(a) - 1]
        with self.busy(f"downloading {r.get('title', '')}"):
            ok = self.act.download_video(r["videoId"])
        if not ok:
            self.say("[red]download failed[/]")
            return None
        lib = self.library(refresh=True)
        artist = ", ".join(x["name"] for x in r.get("artists") or [])
        return LocalIndex(lib).find(artist, r["title"], r.get("duration_seconds"))

    def stats(self) -> None:
        now = time.time()
        for title, rows in (("Top artists, last 30 days", stats.top_artists(self.db, 30, 8, now)),):
            t = Table(title=title, show_edge=False, header_style="bold")
            t.add_column("artist")
            t.add_column("plays", justify="right")
            for a, c in rows:
                t.add_row(a, str(c))
            self.say(t)
        t = Table(title="Top tracks, last 30 days", show_edge=False, header_style="bold")
        t.add_column("track")
        t.add_column("plays", justify="right")
        for a, ti, c in stats.top_tracks(self.db, 30, 8, now):
            t.add_row(f"{a} - {ti}", str(c))
        self.say(t)
        mix = stats.listening_mix(self.db, 30, now)
        if mix:
            self.say(f"Of {int(mix['tracks'])} different tracks in 30 days: [green]{mix['new to you']:.0%} new to you[/], "
                     f"[yellow]{mix['back after 1y+']:.0%} back after a year+[/], {mix['repeat']:.0%} repeats")
        d = stats.dashboard(self.cfg, self.db)
        lib = self.library()
        self.say(f"Library: {len(lib)} tracks, {sum(t.duration for t in lib) / 3600:.1f} h · pool: "
                 + (", ".join(f"[{COLORS.get(b, 'white')}]{b} {n}[/]" for b, n in d.pool.items()) or "empty"))
        s = stats.swim_stats(self.db)
        done = s["sessions"].get("scrobbled", 0)
        last = datetime.fromtimestamp(s["last_swim"]).strftime("%Y-%m-%d") if s["last_swim"] else "never"
        self.say(f"Swims: {done} scrobbled ({s['swim_minutes'] / 60:.1f} h in the water), "
                 f"{s['sessions'].get('discarded', 0)} discarded, last {last}")
        if s["most_swum"]:
            self.say("Most swum: " + " · ".join(f"{a} - {ti} ×{c}" for a, ti, c in s["most_swum"]))

    def wishlist(self) -> None:
        items = stats.wishlist_lines(self.cfg.mix_weights)
        if not items:
            self.say("[yellow]The wishlist is empty.[/] Run a sync (y) to build it.")
            return
        t = Table(title=f"Wishlist ({len(items)}): tracks you play or Last.fm suggests, not in your library",
                  show_edge=False, header_style="bold")
        for h in ("#", "bucket", "track"):
            t.add_column(h)
        for i, (label, bucket, _url) in enumerate(items[:20], 1):
            t.add_row(str(i), f"[{COLORS.get(bucket, 'white')}]{bucket}[/]", label)
        if self.keys:
            shown = items[:20]
            picked = self.keys.multiselect([f"[{b}] {label}" for label, b, _ in shown],
                                           title=f"Wishlist ({len(items)}): tick tracks to download now")
            if not picked:
                return
            idx = [i + 1 for i in picked]
        else:
            self.say(t)
            a = self.ask("Download which? (e.g. 1,3,5 or 'top 5') · Enter back")
            if not a:
                return
            idx = list(range(1, 6)) if a.startswith("top") else [int(x) for x in a.replace(" ", "").split(",") if x.isdigit()]
        got = 0
        idx = [i for i in idx if 1 <= i <= min(20, len(items))]
        for n, i in enumerate(idx, 1):
            label = items[i - 1][0]
            artist, _, title = label.partition(" - ")
            with self.busy(f"downloading {n}/{len(idx)}: {label}"):
                entry = self.act.find_song(artist, title)
                ok = bool(entry) and self.act.download_video(entry.video_id)
            if ok:
                got += 1
                self.say(f"[green]downloaded[/] {label}")
            else:
                self.say(f"[yellow]could not get[/] {label}")
        if got:
            self.library(refresh=True)
            self.say("They are in your library now; add one to the playlist with a, or sync (y) to pool them.")

    def pick_many(self, rows: list[str], title: str) -> list[int]:
        """Tick any number of rows (0-based indexes); nothing ticked = none."""
        if self.keys:
            return self.keys.multiselect(rows, allow_none=True, title=f"{title} (Space ticks, none ticked = none)") or []
        for i, r in enumerate(rows, 1):
            self.say(f"  {i}. {r}")
        return sorted(i - 1 for i in parse_positions(self.ask(f"{title} (numbers, blank = none)")) if 0 < i <= len(rows))

    def podcasts(self) -> None:
        """Mark what you finished as played in Pocket Casts, then sync (finished ones make room for new ones)."""
        eps = self.act.podcast_episodes()
        picked = self.pick_many([e["title"] for e in eps], "Finished any?") if eps else []
        if picked:
            with self.busy("marking them played in Pocket Casts"):
                self.say(self.act.mark_played([eps[i] for i in picked]))
        with self.busy("syncing podcasts"):
            self.say(self.act.podcasts())

    def sync(self) -> None:
        download = self.yes("Also download missing playlist and wishlist tracks?", default=True)
        with self.busy("syncing Last.fm history and the pool (the first run after a long gap takes a while)"):
            self.say(self.act.sync(download))
        self.library(refresh=True)

    def scrobble(self) -> None:
        row, m = self.session()
        if not row:
            self.say("[yellow]Nothing staged, so nothing to scrobble.[/]")
            return
        age_min = (time.time() - row["planned_at"]) / 60
        if age_min < self.cfg.min_swim_gap_minutes and not self.yes(
                f"This was staged only {age_min:.0f} min ago. Scrobble anyway?"):
            return
        now = datetime.now()
        with self.busy("looking for the swim on Garmin Connect"):
            found = flow.lookup_swim(self.act.find_swim, row, now, lambda msg: self.say(f"[yellow]{msg}[/]"))
        if found:
            self.say(f"Garmin: [bold]{found.describe()}[/]")
        default = found.minutes if found else m.get("minutes", self.cfg.minutes)
        swim = float(self.ask("How many minutes did you swim?", f"{default:.0f}") or 0)
        skipped = parse_positions(self.ask("Positions you skipped (comma separated, blank = none)"))
        settled = flow.settle(m, self.cfg, now, found, swim, skipped)
        scrobbles = settled.scrobbles
        t = Table(title=f"{len(scrobbles)} scrobbles", show_edge=False, header_style="bold")
        t.add_column("time")
        t.add_column("track")
        for s in scrobbles:
            t.add_row(f"{s.timestamp:%a %H:%M:%S}", f"{s.artist} - {s.title}")
        self.say(t)
        if not scrobbles or not self.yes("Send these to Last.fm?", default=True):
            return
        try:
            with self.busy("sending to Last.fm"):
                msg = self.act.submit(scrobbles)
        except Exception as e:  # keep the session so nothing is lost
            self.say(f"[red]failed: {e}[/] (the session is still pending)")
            return
        flow.record(self.db, row, settled)
        flow._set_handled(False)
        self.say(f"[green]{msg}[/]")
        if settled.swim and self.act.garmin_sync:
            try:
                with self.busy("timing your tracks against the Garmin lengths"):
                    self.say(f"Garmin: {self.act.garmin_sync()}")
            except Exception as e:
                self.say(f"[yellow]Garmin analysis failed: {e}[/]")
        self.discoveries(settled.plan)
        if self.yes("Stage the next playlist now?", default=True):
            self.reroll()

    def discoveries(self, heard) -> None:
        """New-to-you tracks you heard all the way through: love the ones you liked."""
        new = [(p.track.artist, p.track.title) for p in heard if p.source in NEW_TO_YOU]
        picked = self.pick_many([f"{a} - {t}" for a, t in new], "New to you this swim: liked any?") if new else []
        if picked:
            try:
                with self.busy("loving them on Last.fm"):
                    msg = self.act.love([new[i] for i in picked])
                self.say(f"[green]{msg}[/]")
            except Exception as e:
                self.say(f"[red]could not love them: {e}[/]")

    def recap(self) -> None:
        for line in stats.recap(self.db, 7).lines():
            self.say(line, markup=False)

    def skip(self) -> None:
        row, _ = self.session()
        if not row:
            self.say("Nothing staged.")
        elif self.yes("Forget the staged playlist without scrobbling (you did not swim)?"):
            history.discard_pending(self.db)
            flow._set_handled(False)
            self.say("[green]discarded[/]; nothing was scrobbled")
            if self.cfg.device_path.is_dir() and self.yes("Also remove those files from the headphones?", default=True):
                self.say(f"[green]removed {flow.clear_orphans(self.cfg, self.db)} files[/]")

    def adopt(self) -> None:
        sid = flow.adopt_orphans(self.cfg, self.db)
        if sid is None:
            self.say("[yellow]No leftover staged files on the headphones.[/]")
        else:
            flow._set_handled(True)
            self.say(f"[green]session {sid} is the current playlist again[/]; scrobble it after your swim with c")

    def clear(self) -> None:
        n, sid = flow.orphans(self.cfg, self.db)
        if not n:
            self.say("[yellow]No leftover staged files on the headphones.[/]")
        elif self.yes(f"Remove the {n} leftover tracks of session {sid} from the headphones?", default=True):
            self.say(f"[green]removed {flow.clear_orphans(self.cfg, self.db)} files[/]")

    def on_connect(self) -> None:
        with self.busy("running the on-connect routine"):
            log = self.act.on_connect()
        for line in log:
            self.say(line)

    def agent(self) -> None:
        on = self.act.agent_installed()
        self.say("The agent runs the whole routine automatically whenever the headphones are plugged in "
                 f"(currently [bold]{'installed' if on else 'not installed'}[/]).")
        if self.yes(f"{'Remove' if on else 'Install'} it?"):
            self.act.agent_set(not on)
            self.say("[green]done[/]")

    def run(self) -> None:
        handlers: dict[str, Callable[[], None]] = {
            "p": self.playlist, "r": self.reroll, "a": self.add_track, "s": self.stats, "w": self.wishlist,
            "y": self.sync, "c": self.scrobble, "k": self.skip, "u": self.adopt, "x": self.clear,
            "o": self.podcasts, "v": self.recap, "b": self.blocklist,
            "e": self.on_connect, "g": self.agent,
            "?": lambda: self.say(HELP), "h": lambda: self.say(HELP),
        }
        names = {"playlist": "p", "reroll": "r", "add": "a", "stats": "s", "wishlist": "w", "sync": "y", "scrobble": "c",
                 "skip": "k", "podcasts": "o", "blocklist": "b", "recap": "v", "on-connect": "e", "agent": "g", "help": "?", "quit": "q", "exit": "q"}
        if self.keys:
            return self.run_keys(handlers)
        self.dashboard()
        while True:
            key = self.ask("\nswimscrobble>").lower()
            key = names.get(key, key[:1]) if key else ""
            if key in ("q",):
                return
            if not key:
                self.dashboard()
                continue
            fn = handlers.get(key)
            if fn is None:
                self.say("[yellow]unknown key[/] · ? for help")
                continue
            try:
                fn()
            except _Cancel:
                self.say("[dim]cancelled[/]")
            except KeyboardInterrupt:
                self.say("\n[dim]interrupted[/]")
            except Exception as e:  # a failing action must not kill the shell
                self.say(f"[red]error: {e}[/]")
            self.dashboard()


    MENU = [
        Item("p", "Playlist", "view and edit the staged playlist"),
        Item("r", "Reroll", "stage a new playlist"),
        Item("a", "Add track", "from your library or YouTube Music"),
        Item("s", "Stats", "listening, pool and swim statistics"),
        Item("w", "Wishlist", "tracks you play but do not own"),
        Item("y", "Sync", "Last.fm history, pool, downloads"),
        Item("c", "Scrobble", "settle the last swim"),
        Item("k", "Skip", "forget the staged playlist (no swim)"),
        Item("u", "Use leftovers", "files on the headphones from a discarded session"),
        Item("x", "Remove leftovers", "delete those files"),
        Item("v", "Recap", "the last 7 days of swimming"),
        Item("b", "Blocklist", "artists and tracks you never want to hear"),
        Item("o", "Podcasts", "newest unplayed episodes"),
        Item("e", "On-connect", "run the whole routine now"),
        Item("g", "Agent", "install / remove the plug-in agent"),
        Item("?", "Help", "what everything does"),
        Item("q", "Quit"),
    ]

    def run_keys(self, handlers: dict[str, Callable[[], None]]) -> None:
        """Arrow-key menu: the dashboard stays on top, Enter or a shortcut letter runs an action."""
        cursor = 0
        while True:
            self.console.clear()
            self.dashboard()
            try:
                key = self.keys.menu(self.MENU, start=cursor)
            except EOFError:  # terminal closed / input ended
                return
            if key in (None, "q"):
                return
            cursor = next((i for i, it in enumerate(self.MENU) if it.key == key), 0)
            self._said = 0
            try:
                handlers[key]()
            except _Cancel:
                self.say("[dim]cancelled[/]")
            except KeyboardInterrupt:
                self.say("\n[dim]interrupted[/]")
            except Exception as e:  # a failing action must not kill the shell
                self.say(f"[red]error: {e}[/]")
            if self._said:  # something was printed: let it be read before the screen is cleared
                self.keys.pause("press any key to go back")


class _Cancel(Exception):
    pass


def real_actions(cfg: Config, db, progress: flow.Progress = flow._quiet) -> Actions:
    """The production wiring: the same functions the command line uses. `progress` receives step updates from
    the long ones (the shell's spinner)."""
    from . import acquire, cli, garmin, lastfm, launchd, pocketcasts, ytm
    from .energy import cached_features
    from .library import scan_config

    def sync(download: bool) -> str:
        rep = cli.run_sync(cfg, db, download=download, progress=progress, log=progress)
        p = rep.pool
        return (f"pool +{len(p.added)} -{len(p.retired)} · playlist {rep.playlist_total - len(rep.playlist_missing)}/"
                f"{rep.playlist_total} found · {len(p.missing)} wishlist entries")

    def podcasts() -> str:
        return str(cli.podcast_sync(cfg, db, only_if_logged_in=False, progress=progress)())

    def love(tracks: list[tuple[str, str]]) -> str:
        sk = lastfm.load_session_key()
        for a, t in tracks:
            lastfm.love(a, t, sk)
        msg = f"loved {len(tracks)} on Last.fm"
        if cfg.ytm_auth_path and cfg.playlists:
            ids = [e.video_id for a, t in tracks if (e := acquire.find_song(a, t))]
            if ids:
                ytm.add_to_playlist(cfg.ytm_auth_path, cfg.playlists[0], ids)
                msg += f", added {len(ids)} to your playlist"
        return msg

    def mark_played(eps: list[dict]) -> str:
        pocketcasts.mark_played(eps)
        return pocketcasts.describe_marked(eps)

    def on_connect() -> list[str]:
        return flow.on_connect(cfg, db, notify=cli.notify, pool_sync=lambda: sync(cfg.auto_download), force=True,
                               find_swim=garmin.finder(cfg), progress=progress, garmin_sync=cli.garmin_sync(cfg, db),
                               podcast_sync=cli.podcast_sync(cfg, db, progress=progress))

    def submit(scrobbles: list) -> str:
        return flow.describe_sent(flow.send_scrobbles(scrobbles, cfg), scrobbles, cfg)

    import os

    def agent_set(on: bool) -> None:
        launchd.install(os.getuid()) if on else launchd.uninstall(os.getuid())

    return Actions(
        load_library=lambda: scan_config(cfg),
        features=lambda t: cached_features(db, t),
        sync=sync,
        search_youtube=lambda q: acquire.yt_search(q, 8),
        download_video=lambda vid: acquire.download_video(cfg, vid),
        find_song=lambda a, t: acquire.find_song(a, t),
        podcasts=podcasts, on_connect=on_connect, submit=submit, mark_played=mark_played, love=love,
        podcast_episodes=lambda: pocketcasts.on_device(db), find_swim=garmin.finder(cfg),
        garmin_sync=cli.garmin_sync(cfg, db),
        agent_installed=lambda: Path(launchd.PLIST).exists(), agent_set=agent_set,
    )
