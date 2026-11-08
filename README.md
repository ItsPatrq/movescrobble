# movescrobble

Offline MP3-mode headphones (built for the Shokz OpenSwim Pro) keep **no play history**, so
nothing can be scrobbled from the device. movescrobble makes the session deterministic: it
plans what will play, you swim, then it scrobbles exactly what was planned.

It also follows your other workouts: runs recorded on Garmin get the tracks Last.fm saw during them written onto the
activity. The headphones get one playlist, whatever you use it for, and `garmin-sync` works out what you actually did.

## The flow

1. **Plan** (the *mix*): build a varied, energising 2-hour playlist and copy it to the device as
   `001 - `, `002 - ` ... Headphones play alphabetically, so filename order is play order.
   - **Pool first**: there is one library folder, `music_dir` (default `~/Music/YouTubeMusic`). Everything
     in it is eligible; playlist tracks and wishlist tracks are downloaded into it, and the playlist(s)
     (`ytm_playlist` / `ytm_playlists`) are just another source of downloads. Set `pool = "playlist"` to
     restrict the session to playlist tracks (plus wishlist downloads).
   - **Then check each against your Last.fm history**, mirrored locally (the last `history_days`, then
     updated incrementally). Recency-decayed play counts (chosen by offline evaluation, see
     [docs/ALGORITHM.md](docs/ALGORITHM.md)) put every track in one of five buckets, each with a share of the
     time (`mix_weights`):
     - **obsession** (40%): by an artist you are into right now, any of their tracks, played or not;
     - **mid** (15%): played within the last 90 days;
     - **fresh** (15%): never played, but by an artist you do listen to;
     - **lost** (10%): played or loved long ago, silent for 90+ days;
     - **rec** (20%): never played, by artists you have not played either (discovery), scored by the
       taste graph below; a discovery it does not connect to your listening is left out.
   - **Taste graph** (`taste.py`, see what it suggests with `movescrobble recs`): artists, genres and tracks
     linked by Last.fm similarity (artist.getSimilar, track.getSimilar) and IDF-weighted genre tags. A random
     walk with restart (personalised PageRank) starts from the artists you play *now* (this month counts most)
     and your current favourite tracks; how often it lands on an artist is how close they are to your taste
     through every path at once. A walk from artists you blocked or keep skipping is subtracted, each artist is
     weighted by how well its genres match yours, podcasts in your history are ignored, and discoveries are
     spread over your artists so one tight cluster cannot take every slot. Tracks to try are the top tracks of
     the best artists (unknown ones become **rec**, known ones **fresh**) and tracks similar to your current
     favourites; the wishlist downloads them. Nothing comes from a genre's global chart. Last.fm answers are
     cached for 30 days.
   - **Selection**: a determinantal point process picks relevant tracks that are also *different* from each
     other (sound and artist), different every run (`variety`), steered towards the bucket shares.
   - **Sequence**: an optimiser orders them as a workout: warm-up, a few surges, a small taper over the swim
     window, smooth tempo and energy transitions, no artist twice in a row.
   - *Energy*: every file is analysed locally (tempo, loudness, onset density, pulse clarity; cached).
     Tracks below `min_energy` (default 0.5, i.e. slow ballads and ambient pieces) are dropped, and
     high-energy tracks are drawn more often. This applies to every bucket, including lost gems.
   - *Rest*: tracks you swam to in the last `no_repeat_days` are skipped, and heavily swum ones fade out.
   - *Wishlist, no playlist upkeep needed*: tracks you play a lot (or Last.fm suggests) but have no
     file for are looked up on YouTube Music (songs only; the title and artist must both match, so no
     covers or live versions) and downloaded, up to `auto_fetch` (default 20) per sync, into the same
     library folder (`<album>/<artist> - <title>.mp3`). Only tracks you played 3+ times in the last year are wanted (podcast-looking titles are skipped, the
     list is capped at 300). Misses are remembered, retried after 30 days, and do not use up the quota. The list with search links is also
     written to `~/.config/movescrobble/wishlist.txt`. Adding tracks to your playlist still works too
     and gives you control over what is in the pool.
2. **Swim.**
3. **Scrobble**: the staged list (`playlist_minutes`, default 120) is longer than the swim (`minutes`, default 60) so
   it can't run out. **`movescrobble garmin-sync` (shell `n`) does this at any time, headphones connected or not**, and
   Garmin decides (`garmin = true`, after `movescrobble garmin-auth`): the first swim recorded after staging gives the
   real start and length (elapsed, so rests at the wall count) and the playlist is sent to Last.fm (and optionally
   ListenBrainz). No swim on Garmin, or Garmin unreachable: nothing is sent and the playlist stays pending. A run
   never counts. Plugging in the headphones does not scrobble either.
   **One song, one page** (`canonical.py`): a file tagged `Apparat, Philipp Johann Thimm` is sent to the page the song
   already has, not a new one for that credit. Among your history's spellings (same title, same or fewer of the credited
   artists) the one with the most Last.fm listeners wins; for a song you never played, the credited artist Last.fm
   knows it under. Remixes, live takes and covers stay separate. Your files and local records keep their tags, and
   previews (`--confirm`, the shell, CSV) show those tags, not the page.
4. **Podcasts**: the newest episode of each Pocket Casts subscription, unless already played or
   archived, is mirrored to the device's `PocketCast/` folder (newest first, at most `max_podcasts`; not scrobbled).
   Runs on every connection.
   The headphones cannot change playback speed, so episodes are re-encoded at `podcast_speed` (default 1.3x,
   pitch kept) with pauses over 0.6 s cut to 0.3 s (`podcast_trim_silence`): roughly a quarter shorter and smaller.
   Pocket Casts never learns what you heard on the headphones: tick what you finished in the shell (`o`), or
   `movescrobble podcasts --done 1,3`, and those are marked played and replaced by the next sync.
5. **Loudness**: every staged track is levelled to `loudness_target` LUFS (default -11; two-pass, linear gain, no
   pumping) so quiet masters stay audible underwater. Originals are untouched; levelled copies are cached in
   `~/.config/movescrobble/loudness-cache/` (unused for 60 days: deleted). Tracks already close to the target are
   copied as they are. The first staging takes a few minutes; later ones only level tracks new to the cache.
6. **Space**: before staging, the playlist is checked against the free space on the headphones (keeping
   `space_reserve_mb` free): buffer tracks are dropped from the end until it fits, and if even the swim part does
   not fit nothing is staged and the notification says so. Podcasts leave room for the next playlist: an episode
   that would eat into it is skipped (the oldest go first).
7. **Skips teach the mix**: positions you mark as skipped when scrobbling (shell `c`, or `--skip`) halve that
   track's chance next time and make the artist a little rarer; a track skipped 3 times is not picked again.
8. **Discoveries**: after scrobbling in the shell, the new-to-you tracks you heard are listed; tick the ones you
   liked to love them on Last.fm, and (after `movescrobble ytm-auth`) add them to your first `ytm_playlist`,
   which moves them into your obsession bucket sooner.
9. **Workouts**: the energy curve over the swim. `steady` (warm-up, two surges, taper), `intervals` (3 hard / 3
   easy blocks), `pyramid` (build to a peak half-way, then down), `easy` (calmer tracks, even and low). Default
   `workout` in config; per swim with `reroll --workout intervals` or the shell's reroll.
10. **Recap**: `movescrobble recap [--days 7]` or shell `v`: swims, hours, distance (Garmin), tracks heard,
    skips, most swum artists and the discoveries you heard. With Garmin also: pace per 100 m over your last swims,
    distance per week, average heart rate and time in each zone, and your power songs.
11. **One notification per plug-in**, e.g. `6 podcasts (2 new) · staged 26 tracks, 119 min, 4 new · YouTube playlist
    updated (26/26 tracks)`; anything that failed is in it, in capitals.
12. **Power songs** (Garmin): the watch records every pool length with its start time, so each length belongs to
    the track playing at the time. Each track gets a speed relative to that whole swim (plus heart rate and
    SWOLF); averaged over swims and trusted more as lengths add up, it makes the mix pick tracks you swim faster to
    more often and the ones that slow you down less (up to ±25-30%). Runs with
    `movescrobble garmin-sync`. Caveat: the last minutes of a swim are often a cool-down, so tracks there look slower.
13. **Soundtrack on the activity** (`garmin_soundtrack = true`): the tracks you swam to, with your pace for each,
    are written into the Garmin activity's description. Your own description text is kept; re-running replaces only
    the soundtrack part. This edits Garmin Connect only: Strava and other linked services receive an activity when it
    uploads, and later Garmin edits generally do not reach them.
    **Runs** (`garmin_run_soundtrack = true`): `garmin-sync` also writes what Last.fm says you played during each
    run of the last 30 days (`--run-days`) into that activity. No playlist is made for a run. Scrobbles that arrive
    late update the list; a run with no scrobbles is left alone.
14. **Blocklist**: `movescrobble block "The Weeknd"` (or `block "Artist" "Title"` for one track; no arguments lists,
    `--remove` unblocks), the shell's `b` on a playlist track, or the shell's blocklist menu. Blocked music is never
    picked, pooled, wished for or downloaded; an artist block also catches collaborations and features.

## Install

```
uv tool install --editable .     # puts `movescrobble` on your PATH (~/.local/bin)
uv venv && uv pip install -e '.[dev]'   # only needed for development / running tests
```
Requires Python 3.11+, `yt-dlp` and `ffmpeg` for playlist/download features.

## Config (`~/.config/movescrobble/config.toml`)

```toml
music_dir = "~/Music/YouTubeMusic"   # the single library: scanned recursively, downloaded into, staged to the device from here
exclude_dirs = []
auto_download = true          # on-connect: download playlist tracks missing from music_dir + wishlist tracks
auto_fetch = 20               # wishlist tracks per sync (0 = off)
cookies_file = "~/music.youtube.com_cookies.txt"
device_name = "SWIM PRO"
minutes = 60                 # how long you swim (the part that gets scrobbled)
playlist_minutes = 120       # staged playlist length; the rest is buffer
session_offset_minutes = 0   # gap between finishing the swim and plugging in
ytm_playlist = "https://music.youtube.com/playlist?list=..."
history_days = 365           # first Last.fm history pull goes this far back; later pulls are incremental
ytm_playlists = []           # optional extra playlists, all synced
pool = "library"             # or "playlist"
lastfm_user = "yourname"
listenbrainz = false
arc = false                  # (plain `plan` only) warm-up -> peak -> cool-down by BPM tag
mix_weights = { obsession = 0.40, mid = 0.15, fresh = 0.15, lost = 0.10, rec = 0.20 }
max_per_artist = 4           # soft cap; the selection already spreads artists
variety = 2.5                # randomness of the selection
min_energy = 0.5             # 0..1; raise to cut more mid-tempo tracks
max_podcasts = 10
podcast_speed = 1.3          # 1 = original speed
podcast_trim_silence = true
loudness_target = -11        # LUFS; 0 = copy files untouched
garmin = false               # true after `movescrobble garmin-auth`
garmin_soundtrack = true     # write what you swam to into the Garmin activity's description
garmin_run_soundtrack = true # write what Last.fm says you played on a run into its Garmin description
ytm_mirror = true            # keep a YouTube Music playlist equal to the headphones (needs `movescrobble ytm-auth`)
ytm_mirror_title = "movescrobble: on the headphones"
ytm_mirror_privacy = "PRIVATE"   # or UNLISTED, PUBLIC
warmup_minutes = 8           # with Garmin: music starts this long before the watch (scrobbled too)
cooldown_minutes = 11        # with Garmin: and plays on this long after the swim (scrobbled too)
workout = "steady"           # steady | intervals | pyramid | easy
space_reserve_mb = 200       # always left free on the headphones
ytm_auth_file = "~/.config/movescrobble/ytm_browser.json"   # written by `movescrobble ytm-auth`
no_repeat_days = 7
```

## Commands

| command | what |
|---|---|
| `mix [dir] [--playlist-minutes N] [--min-energy X] [--workout W] [--stage DIR]` | the varied energising session (shows energy and bucket per track) |
| `plan [dir] --stage DIR` | plain shuffle without taste or energy, for comparison |
| `scrobble session.json --start ISO [--played-minutes N] [--skip 3,7] [--confirm] [--submit]` | CSV by default; `--submit` sends to Last.fm |
| `scrobble-pending` | scrobble the last staged session (start inferred from now) |
| `status` | what is staged, history and pool sizes, what is excluded right now |
| `reroll [--sync] [--workout W]` | replace the staged playlist on the connected headphones with a fresh one (old session discarded, not scrobbled) |
| `skip` | forget the staged session without scrobbling (you did not swim) |
| `eval` | replay your Last.fm history and score the models behind the playlist (reproduces docs/ALGORITHM.md) |
| `recs [-n N]` | the taste graph's discoveries and tracks to try, with the artists of yours that lead there |
| `recap [--days N]` | swims, time, distance, tracks, skips, discoveries |
| `ytm-auth [--browser NAME] [--account N] [--cookies FILE]` | sign in to YouTube Music using a browser you are already signed in to: keeps the headphones playlist there, adds liked discoveries to your playlist |
| `garmin-auth` | one-time Garmin Connect login (email, password, MFA code if enabled); only tokens are stored |
| `garmin-sync [--days 60] [--run-days 30]` | anytime, no headphones needed: a swim on Garmin scrobbles the staged playlist (then times its tracks, writes its soundtrack); runs get their tracks from Last.fm |
| `block [artist] [title] [--remove]` | never play an artist or one track; no arguments lists the blocklist |
| `lastfm-auth` | one-time Last.fm authorisation (`LASTFM_API_KEY`/`LASTFM_API_SECRET`) |
| `playlist [url] [--download]` | list which playlist tracks exist under `music_dir`; `--download` fetches the missing ones |
| `sync [--download [--yes]] [--fetch-missing N]` | refresh the swim pool: your Last.fm history and the taste graph, plus the playlist; optionally offer to download N wanted tracks you don't have yet |
| `podcasts [--done 1,3] [--dest DIR]` | mark finished episodes played, then mirror newest unplayed episode per Pocket Casts subscription to the headphones, or to `DIR` (`POCKETCASTS_EMAIL`/`POCKETCASTS_PASSWORD`). Episodes are not kept on the computer: each scratch file is deleted once copied |
| `on-connect` | sync the headphones: podcasts, the next playlist once this one is settled, its YouTube Music copy |
| `agent install` | launchd agent that runs `on-connect` when volumes change |

### Automatic mode

`movescrobble agent install` registers a launchd agent watching `/Volumes`. On each mount:

- device absent: nothing, and the next connection counts as new
- device present, first event: sync podcasts; stage the next playlist if the last one was settled, otherwise leave
  it on the headphones; bring its YouTube Music copy in line
- nothing is scrobbled and Garmin is not read: that is `movescrobble garmin-sync`, whose failed submissions keep
  the session pending, so nothing is lost or double-sent

macOS may ask for permission to access removable volumes for the agent's Python.

## The interactive shell

Run `movescrobble` with no arguments (or `movescrobble shell`). A dashboard shows the headphones (connected, free
space), the staged playlist (tracks, minutes, how many fill your swim, bucket mix), library and pool sizes,
wishlist, your Last.fm history, what is excluded as recently swum, and whether the agent is installed. Then
one key per action:

| key | |
|---|---|
| `p` | the staged playlist with times, energy and bucket per track, a marker where your swim ends; edit in place: `d 3,5` delete, `m 7 2` move, `a` add |
| `r` | reroll: a new playlist (optionally refreshing Last.fm first, optional seed); the old one is discarded, not scrobbled |
| `a` | add a track: search your library, or YouTube Music (downloads it), then pick the position; slow tracks are flagged |
| `s` | statistics: top artists and tracks, how much of your listening is new / returning / repeat, library and pool, swims |
| `d` | discover: the taste graph in four tabs. **Graph**: drawn as a graph: you in the middle, the artists you play now around you, the artists each leads to on the outside, joined by lines (dotted when a second artist of yours leads there too; ♪ = in your files). Arrow keys move between artists, the side panel says why one is there and lists its tracks (♪ yours, ↓ to get); `Enter` steps into that list, `o` opens a draggable version in the browser. **In your files** / **To get**: the same picks as lists. Everywhere: `Enter` plays a 30-second clip (local file, or fetched from YouTube Music; `s` stops), `f` puts the track first in the staged playlist (downloading it first if needed), `d` downloads, `+` / `-` steer by the artist (more / less like it), `b` blocks. **Adjust**: how far from your artists to look, how much genre matters, how much to spread picks over your artists, this month vs the last months; `g` rebuilds, `a` refreshes the pool with it (and offers a re-roll). Settings are saved and every later sync uses them |
| `w` | wishlist (balanced across buckets): download chosen tracks right now |
| `y` | sync: Last.fm history, pool, downloads |
| `c` | scrobble the last swim: how long you swam, which tracks you skipped, preview, send; then stage the next |
| `k` | skip: forget the staged playlist (you did not swim) |
| `u` | leftovers: staged files still on the headphones after a discarded session (the dashboard warns when there are some); asks whether to use them again or remove them |
| `o` / `e` / `g` | podcasts / run the whole automatic routine now / install or remove the launchd agent |

**Keyboard navigation**: on a terminal the menu is driven by `↑`/`↓` (or `j`/`k`), `Enter` and `Esc`; every
item also has a shortcut letter that fires immediately. Lists (search results, YouTube Music results) are picked
with the arrows; the wishlist is a checklist (`Space` ticks, `a` all/none, `Enter` downloads); yes/no questions
are `y`/`n`. In the playlist view the cursor walks the tracks: `Enter` picks a track up, `↑`/`↓` carry it, `Enter`
drops it; `d` deletes (with confirmation), `a` adds, `r` rerolls, `q` goes back. Use `movescrobble shell --plain`
(or `MOVESCROBBLE_PLAIN=1`) for plain line input; that is also used automatically when not on a terminal.

**What "staged" means**: the playlist this tool copied to the headphones (files named `001 - …`) and recorded in
its database, so it knows what to scrobble. If you discard the record (`k`) but leave the files, they still play;
the dashboard then says "none tracked" and offers `u` (use them again, or remove them). `k` now also offers
to remove the files.

The shell and the agent share everything (database, config, device state), so use either, or both: the agent
does the routine when you plug in, the shell is for looking, correcting and rerolling.

## Reel collages

`movescrobble collage PHOTOS... [--peek PHOTO] [--activity latest]` cuts your photos into shapes (straight stacked rows, bands, slants, arches, a moon, and curved rows by
default; slashes, eclipses, shards, stairs, double exposures and blinds with `--layouts`), keeps their own colours with a slight edit (`natural`, the most common), or grades
them: `sunset` pulls every photo to one warm palette sampled from them, plus colour (chrome, neon, golden, teal) and muted
(noir, subway, tungsten, bleach, sodium, soho; `--looks natural`, `sunset`, `colour` or `muted` for one kind), and writes 12 versions (`-n` for more or fewer) to `~/Movies/movescrobble/`: a still and a silent 9:16
reel each, plus `contact-sheet.jpg`. Every run draws new versions. Gaps between pieces show a dimmed photo, not black. A `--peek` photo is on every version as one ordinary cell of the pattern, cut like its neighbours but smaller and quieter, usually with both legs in frame; in a reel it appears early and later pictures arrive over its edges. One photo is enough: up to 3 are cut as one continuous picture.

Garmin activities carry no photos, so you bring them. `--activity latest` adds the activity's data, a different mix per
version and none on some: distance, time, pace, km/h, heart rate (only sometimes), climb, and the route as a line that
draws itself. `--data distance,speed,route` shows exactly those everywhere. Routes lose 500 m at both ends (`--route-trim`),
which are usually your door, but a route still shows where you run. Output has no EXIF and `versions.json` no coordinates.
Written to be driven by an AI agent for now: see `.claude/skills/collage/SKILL.md`.

### A card

`movescrobble collage-card v13 --activity latest` turns one version of the newest run into a card: the activity's name on
top, distance, total time and average pace (by the watch's timer, as Garmin shows them) in a column at the bottom left,
and the route, thick and white, filling the collage's circle. It is saved as `card-v13` beside the version, so
`collage-edit` opens it, and exported as a still and a reel. `--title` sets the title (`""` for none), `--folder` picks
another run; without `--activity` the run's own stats and route are used. In the editor, Type > Card layout does the same
to the version on screen. A route shows where you run, even with its ends trimmed: think before you post it.

### Changing a collage

`movescrobble collage-edit [FOLDER]` opens a page for a run (the newest by default); in the menu, `m` Collage lets you pick a
run and stops the page when you press a key. Each version has a recipe in `recipes/`:
every shape, crop, colour, type block, route and reel timing as plain data, with no coordinates. The page changes a recipe and
shows it painted by the same code as the final picture.

- **Pieces:** drag a photo inside its shape, wheel to zoom, Shift+wheel to turn; or crop on the whole photo, dragging the window
  to move it and its corner to zoom; switch or swap photos, flip, frame both legs or one; reorder, hide, duplicate, add or delete
  pieces.
- **Photos from disk:** "Add photos…" or drop them on the picture. One dropped on a piece goes into it; otherwise it becomes a
  new piece. They are copied into the run's `added/`, so the run keeps working if the originals move.
- **Filters:** per photo, Vivid, Warm, Cool, Fade, Mono, Noir or Rose as a start, then exposure, contrast, saturation, warmth,
  tint, fade, opacity, shadow and blend; "Use on every piece" copies them to the rest.
- **Shapes:** with the Shape tool, drag shapes, corners and seams; pictures that share a seam move together.
- **Colour:** switch look; saturation, exposure, contrast, blacks, glow, vignette, grain and tints; how close the leg photo
  sits to the others, and its muscle shading.
- **Type and route:** drag and resize, edit lines, add stats from the activity, fonts, colours, and the dark edge that keeps them
  readable; "Find a calm place" moves them to where the picture is quietest.
- **Reel:** length, reveal, push-in, order of appearance, and a scrubber to see any moment of the reveal without encoding.
- **Versions:** draw new ones from the same photos, undo and redo, hold Before to compare, revert.

Edits are saved as you go in `edited/`, exports in `exported/`; the run's own files stay as they were. The page is served on
127.0.0.1 with a token for this session, and reads only the photos the run used and the ones you add.

### Visual regression tests

`tests/test_collage_vrt.py` compares every composition, look and kind of edit, and the reel's reveal, with pictures in `tests/vrt/`.
`tests/test_collage_editor_ui.py` drives the page in a real Chrome. After a change you meant to make, look at the pictures in
`tests/vrt/_failed/`, then rewrite them with `pytest --update-vrt`.

## YouTube Music login

`movescrobble ytm-auth`, nothing to copy or paste. YouTube Music has no login for tools like this: it authorises requests
with your browser's session cookies. So this reads them from a browser you are signed in to (Firefox reads without a
prompt; Chrome-based browsers make macOS ask for their "Safe Storage" key; Safari needs Full Disk Access), keeps only the
youtube.com ones, checks them with a read-only call that shows the account name, and only then saves them to
`ytm_browser.json`, readable only by you. That file can act as your Google account on YouTube: never share it, and delete
it to sign out here. When Google ends the session, playlist updates report `YouTube playlist FAILED`: run it again.

## Daily use

1. **After a swim or run**: `movescrobble garmin-sync`. A swim scrobbles the playlist; runs get their tracks.
2. **Plug in** (automatic with `movescrobble agent install`, otherwise `movescrobble on-connect`): podcasts refresh,
   and if the swim settled the playlist, the pool is refreshed and a new 2-hour playlist is staged. Eject.

Plugged in before `garmin-sync`? The playlist stays until a sync settles it. Swam without Garmin recording? Use
`movescrobble scrobble-pending --start ISO [--played-minutes N] [--skip 3,7] --confirm --submit`.

**Every playlist is new.** The selection is randomised (`variety`), history moves on (what you played since
changes the buckets), tracks you swam to in the last `no_repeat_days` (7) are skipped, and the more you swam to
a track the less it is picked. Only the first `minutes` of the 2-hour list count as played; the unplayed
buffer is *not* marked as swum, so it comes back first.

**Rotation / corrections**
- Do not like the list? `movescrobble reroll` (add `--sync` to refresh from Last.fm first) stages another one.
- Staged but do not want it? `movescrobble skip`, then plug in. Plugging in never scrobbles, so it is always harmless.
- Swam a different length? `scrobble-pending --played-minutes 45` (or `minutes` in the config).
- Check where things stand: `movescrobble status`.
- More variety over time: keep running `sync --download` (or let `auto_download` do it) so the library grows.

## Library and downloads

`music_dir` (default `~/Music/YouTubeMusic`) is scanned recursively on every `sync` / `on-connect`. Files under
`exclude_dirs`, hidden files and anything under 60 s are ignored. The playlist and your Last.fm tracks
are matched to those files by title and artist (tolerant of `(Remastered)`, `[MIXED]`, accents and
`A, B` credits), and the swim pool only ever contains files you actually have.

Playlist tracks with no matching file are downloaded with the same command as your `ymp3` alias
(`download_command` in the config, run through `yt-dlp` with your cookies), limited to just the missing
positions via `--playlist-items`, so nothing already in your library is downloaded again. New files land
in `music_dir` and are picked up on the next scan. On connect, the chosen tracks are copied from there to
the device (the library itself is never emptied).

## Caveats

- Scrobbles are estimates: the tool assumes you listened to the whole session in order. Last.fm
  rejects scrobbles older than 14 days.
- Pocket Casts has no public API; the client uses the reverse-engineered endpoints and Pocket Casts
  does not support third-party clients. Use your own account at your own risk.
- `playlist --download` uses yt-dlp; make sure you have the right to download what you fetch.
- Energy is a heuristic from the audio (it catches ballads/ambient well, but fast-but-mellow or slow-but-heavy tracks can misfire). Tune `min_energy`, check with `movescrobble mix`.
- Whether the firmware really plays strictly by filename is unverified, see issue #2.

MIT licensed.
