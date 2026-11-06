# How the playlist is built

The aim: a 2-hour, high-energy, *varied* playlist that follows what you are into now, still has room for
old favourites and discoveries, and plays like a workout. Every design choice below was either taken from
the recommender-systems literature or measured on a real 13-year Last.fm history (`movescrobble eval`).

## Pipeline

```
Last.fm history ──► affinity (decayed plays, track + artist) ─┐
Last.fm taste graph (similar artists/tracks, tags) ──────────┤
local files ──► audio features (energy, tempo, timbre) ───────┼─► buckets + quality q ─► DPP selection ─► annealed sequence
swim history (plays, recent swims) ───────────────────────────┘
```

1. **Affinity** (`affinity.py`): recency-decayed play counts per track (30-day and 180-day half-lives)
   and per artist (45 days), plus all-time totals and "loyalty" (distinct weeks with a play).
2. **Buckets** (`buckets.py`): every pool track is judged against that history.
   *obsession*: the artist is hot right now (any of their tracks, played or not); *mid*: played in the last
   90 days; *lost*: played before or loved, silent 90+ days; *fresh*: never played, artist known;
   *rec*: never played, artist unknown, scored by the taste graph (below); unconnected ones are left out.
   **Taste graph** (`taste.py`): nodes are artists, tags and tracks; edges are artist.getSimilar matches,
   IDF-weighted top tags and track.getSimilar matches for your current favourite tracks. Personalised PageRank
   (restart 0.3) from your artists weighted by this month's plays, minus the same walk from blocked/skipped
   artists, divided by degree^0.3 (hubs), times a genre-profile cosine. Podcasts in the history (talk-like
   titles, "podcast" tags) never seed it. Discoveries are picked MMR-style (each further one through the same
   seed artist x0.7). The first version pulled `tag.getTopTracks` of broad tags ("electronic"), which is a
   global chart, not taste: that is how Calvin Harris and Zedd got in. Holdout on the real history (graph from
   plays before T, did it rank the artists discovered in the next 90 days?): of those it could reach, 3 of 12
   were in its top 25 of ~700 candidates vs ~0.4 by chance; most discoveries (game soundtracks) were out of
   its reach.
3. **Quality** (`mix.py`): q = taste relevance x energy^1.5, damped by how often you already swam to it,
   plus a content bonus for never-played tracks that sound like what you are into (cosine to a taste vector
   built from audio features of your hot tracks, the content-based half of a hybrid recommender).
4. **Selection** (`dpp.py`): a determinantal point process picks the set. Its log-determinant gain rewards
   quality and penalises similarity (audio features + same artist) to what is already chosen, so diversity
   is built into the objective instead of bolted on with caps. Gumbel noise makes every run a different good
   set; a steering term pulls each bucket towards its share of the time (`mix_weights`).
5. **Sequencing** (`sequence.py`): simulated annealing orders the set. Cost = energy and tempo jumps
   (tempo compared modulo octaves), same artist back to back, runs of one bucket, and distance from an
   energy arc over the *swim window* (warm-up, a few gentle surges, a small taper; the 2x buffer after the
   swim just holds the plateau).

## What the established systems do, and what was taken

| Idea | Used by / source | Here |
|---|---|---|
| Hybrid: collaborative filtering + audio + text | Spotify Discover Weekly (secondary descriptions) | Personalised PageRank over a Last.fm similarity + genre graph (collaborative signal), local audio features (content), listening history (behaviour) |
| Exploration vs exploitation (bandits) | Spotify BaRT | Reserved discovery buckets (*fresh*, *rec*), noise in the selection, novelty bonus for never-swum tracks. No online bandit: a swim gives no skip/like feedback |
| Diversity-aware ranking via DPP | Chen et al., NeurIPS 2018 (YouTube-scale feeds) | Greedy MAP with incremental Cholesky update + Gumbel noise + bucket steering, with a fallback fill when the determinant collapses |
| Repeat consumption: recency + frequency, boredom, comebacks | ACT-R relistening (Reiter-Haas et al.), Anderson/Benson models | Decayed counts; *measured* against ACT-R, see below |
| Mere-exposure inverted U | "Discovery Dynamics" (2022) | Swim-play damping and a no-repeat window; discoveries enter via *fresh*/*rec* |
| Exercise music tempo arcs (120-140+ BPM main work, calmer warm-up/cool-down) | Karageorghis (sports psychology), via secondary sources | Arc over the swim window; the data-driven part is the energy filter |
| Content-based cold start from audio | Spotify audio models | Taste vector from audio features for tracks with no plays |

Not adopted: neural / collaborative models trained on millions of users (no such data here), skip-based
feedback (scrobbles carry none), key / harmonic mixing (no crossfades on the headphones, and the gain over
tempo + energy continuity would be small for the effort).

## Measured on a real history (227k scrobbles, 2014-2026)

Reproduce with `movescrobble eval`. Protocol: for 43 cutoff dates, rank using only the past, hit = played in
the next 30 days.

Repeat listening (NDCG@100):

| model | NDCG@100 |
|---|---|
| **decay, 30-day half-life (used)** | **0.374** |
| decay 14d / 60d | 0.365 / 0.360 |
| hard 30/90-day windows (v1 of this tool) | 0.331 |
| ACT-R d=0.8 / d=0.5 | 0.324 / 0.317 |
| random | 0.025 |

Lost gems (tracks with 4+ plays, silent 90+ days; base rate of returning in 30 days: 1.9%):

| model | precision@50 |
|---|---|
| loyalty (distinct play-weeks) | 0.121 |
| **decay, 180-day half-life (used, with a loyalty term)** | 0.120 |
| total plays (v1) | 0.100 |
| random | 0.023 |

What the history says about *you*: only ~9% of a month's plays are tracks you played the month before, 23%
are tracks silent for over a year, 52% are first-ever plays; and 32% of new-to-you tracks are by an artist
played in the previous 30 days (49% within a year). Hence "obsession" is defined by artist, and a large
share of the playlist is discovery and rediscovery.

## Old vs new generator on your library (20 playlists each, same pool)

| | v1 | v2 |
|---|---|---|
| distinct artists per playlist | 16.6 | 21.2 |
| mean energy during the swim window | 0.714 | 0.723 |
| warm-up to middle energy gain | 0.01 | 0.10 |
| mean energy jump between tracks | 0.115 | 0.057 |
| mean tempo mismatch between tracks (octave-aware) | 0.198 | 0.074 |
| same artist back to back (per playlist) | 0.05 | 0 |
| longest run of one bucket | 4 | 2 |
| audio diversity (mean pairwise distance) | 2.90 | 3.04 |
| distinct tracks used across 20 playlists | 111 | 117 |
| track-set overlap between runs (lower = less predictable) | 0.19 | 0.25 |
| focus on hot artists | 0.45 | 0.40 |
| most tracks by one artist (mean) | 3.2 | 3.3 |

Not an improvement: runs overlap a little more than before (0.25 vs 0.19), focus on hot artists is slightly
lower (the selection deliberately trades some of it for variety) and the per-artist maximum did not fall
(the pool has few artists with many tracks, so the soft cap is sometimes relaxed to fill the time).

`variety` (selection noise) was tuned on this table: at 0.35 the overlap between runs was 0.46 (more
predictable than v1); 2.5 brings it near v1 while keeping the quality gains.

## Known limits

* A swim gives no feedback, and your swim scrobbles feed back into your history (a mild self-reinforcing
  loop). The swim-play damping and the no-repeat window counter it.
* Energy and timbre are signal-processing heuristics, calibrated on one library; tune `min_energy`.
* The pool here is small (a few hundred tracks), so overlap between runs is bounded; the wishlist downloads
  grow it.
