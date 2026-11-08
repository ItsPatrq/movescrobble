---
name: collage
description: Make an Instagram reel collage from workout photos with `movescrobble collage`. Use when asked for a collage, a reel, or something to post for a Garmin activity.
---

`movescrobble collage --help` lists every option. This covers what it cannot tell you.

## Steps

1. **Collect the photos.** Garmin activities carry no photos, so ask for a folder or files. Done when you have at least 1. Up to 3 photos cut one continuous picture; 5-10 give the widest range.
2. **Ask about a peek photo.** With several `--peek`, every version carries one, as one cell of the pattern: smaller than the others, and in a reel it appears early and neighbours arrive over its edges. Only pass a photo of the person asking, an adult, that they chose for this. `--peek-side` picks the leg kept when a place is too narrow for both; `--define` with the same photo deepens its muscle shading slightly, in light only; `--peek-level subtle` hides it further.
3. **Render stills first.** Add `--no-reels` for 12 versions in about 15 seconds (`-n` for more); `--activity latest` (or an ID) gives each version its own mix of Garmin data.
4. **Judge the contact sheet.** Read `contact-sheet.jpg` from the printed folder. Done when you have named the 6 strongest and why, and flagged any where the peek reads as foreground, or a crop cuts a face badly.
5. **Render 6 reels.** Rerun those 6 with the same arguments, without `--no-reels`, plus `--seed` from `versions.json`, `--only v03,v07,...` and a new `--out` (the contact sheet there holds only those). If nothing landed, start a fresh run: every run without `--seed` draws new versions. A reel takes about 10 seconds. Done when you have given the full path of each `.mp4`.

## Reference

- They want to change a version by hand (crop, zoom, switch or add photos, filters): `movescrobble collage-edit FOLDER`, or `m` in the `movescrobble` menu, opens a page on this machine, for them to drive. Their exports land in `exported/`. Recipes in `recipes/` and `edited/` are plain JSON, so you can also change one and repaint it with `collage.render_still`.
- Reels are silent 1080x1920: music is added in Instagram.
- A route shows where someone runs, even with its ends trimmed. Mention that before they post a version marked `~`.
- Everything stays on this machine, and output has no EXIF. Posting is theirs to do.
- Narrow a run with `--layouts` and `--looks` when they like one direction: `--layouts stack` gives straight rows, 3 or 4 of them; `--looks natural` keeps each photo's own colours with a slight edit and comes up most by default; `sunset` makes all photos match; `colour` and `muted` take a whole group.
