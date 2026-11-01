"""SQLite store: sessions, plays, playlist pool, downloaded podcasts."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import config_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, planned_at REAL, seed INTEGER, minutes REAL,
  status TEXT NOT NULL DEFAULT 'planned', staged_dir TEXT, manifest TEXT, scrobbled_at REAL);
CREATE TABLE IF NOT EXISTS plays(
  session_id INTEGER, artist TEXT, title TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS pool(
  artist TEXT, title TEXT, source TEXT, score REAL, retired INTEGER DEFAULT 0,
  added_at REAL, PRIMARY KEY(artist, title));
CREATE TABLE IF NOT EXISTS scrobbles(
  ts INTEGER, artist TEXT, title TEXT, PRIMARY KEY(ts, artist, title));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS wish_attempts(
  key TEXT PRIMARY KEY, status TEXT, at REAL);
CREATE TABLE IF NOT EXISTS podcasts(
  uuid TEXT PRIMARY KEY, title TEXT, filename TEXT, downloaded_at REAL);
"""  # the baseline every database starts from; everything since is a numbered migration below


def _add_column(table: str, column: str, kind: str):
    def step(db) -> None:
        # tolerant: some databases got these columns before migrations were versioned
        if column not in {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return step


def _sql(statement: str):
    return lambda db: db.execute(statement)


MIGRATIONS = [  # append only; PRAGMA user_version counts how many have run
    _sql("""CREATE TABLE IF NOT EXISTS features_v3(
      path TEXT PRIMARY KEY, mtime REAL, bpm REAL, loudness REAL, onset REAL, pulse REAL, energy REAL,
      bright REAL, bass REAL)"""),
    _sql("CREATE INDEX IF NOT EXISTS scrobbles_track ON scrobbles(artist, title, ts)"),
    _add_column("podcasts", "podcast", "TEXT"),
    _add_column("sessions", "swum_minutes", "REAL"),
    _add_column("sessions", "distance_m", "REAL"),
    _sql("CREATE TABLE IF NOT EXISTS skips(session_id INTEGER, artist TEXT, title TEXT, ts REAL)"),
    _add_column("podcasts", "variant", "TEXT"),  # how the file was processed (speed / trim); refetch on change
    _sql("""CREATE TABLE IF NOT EXISTS blocked(
      artist TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', PRIMARY KEY(artist, title))"""),  # title '' = whole artist
    _sql("""CREATE TABLE IF NOT EXISTS swims(
      activity_id INTEGER PRIMARY KEY, session_id INTEGER, start REAL, kind TEXT, distance_m REAL, elapsed_s REAL,
      moving_s REAL, avg_hr REAL, max_hr REAL, swolf REAL, hr_zones TEXT, analysed INTEGER NOT NULL DEFAULT 0)"""),
    _sql("""CREATE TABLE IF NOT EXISTS swim_tracks(
      activity_id INTEGER, artist TEXT, title TEXT, lengths INTEGER, distance_m REAL, seconds REAL, hr REAL,
      swolf REAL, rel_speed REAL)"""),  # rel_speed: this track's speed over the whole swim's, minus 1
    _sql("CREATE TABLE IF NOT EXISTS lastfm_cache(key TEXT PRIMARY KEY, at REAL, body TEXT)"),  # taste graph data
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config_dir() / "history.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    migrate(db)
    return db


def migrate(db) -> int:
    """Run the migrations this database has not seen yet; returns how many ran."""
    done = db.execute("PRAGMA user_version").fetchone()[0]
    for step in MIGRATIONS[done:]:
        step(db)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    db.commit()
    return max(len(MIGRATIONS) - done, 0)


def add_session(
    db, manifest: dict, minutes: float, seed: int | None, staged_dir: str | None, at: float | None = None
) -> int:
    cur = db.execute(
        "INSERT INTO sessions(planned_at, seed, minutes, staged_dir, manifest) VALUES(?,?,?,?,?)",
        (at if at is not None else time.time(), seed, minutes, staged_dir, json.dumps(manifest, ensure_ascii=False)),
    )
    db.commit()
    return cur.lastrowid


def get_meta(db, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(db, key: str, value) -> None:
    """None deletes the key. Does not commit."""
    if value is None:
        db.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))


def pending_session(db):
    return db.execute(
        "SELECT * FROM sessions WHERE status='planned' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def mark_scrobbled(
    db, session_id: int, plays: list[tuple[str, str, float]], skipped: list[tuple[str, str]] = (),
    minutes: float | None = None, distance_m: float | None = None,
) -> None:
    """Idempotent guard: refuses to scrobble the same session twice. `skipped` tracks count against them in
    later mixes; `minutes` / `distance_m` are what was actually swum (for the recap)."""
    row = db.execute("SELECT status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None or row["status"] == "scrobbled":
        raise ValueError(f"session {session_id} missing or already scrobbled")
    db.executemany(
        "INSERT INTO plays(session_id, artist, title, ts) VALUES(?,?,?,?)",
        [(session_id, a, t, ts) for a, t, ts in plays],
    )
    db.executemany("INSERT INTO skips(session_id, artist, title, ts) VALUES(?,?,?,?)",
                   [(session_id, a, t, time.time()) for a, t in skipped])
    db.execute("UPDATE sessions SET status='scrobbled', scrobbled_at=?, swum_minutes=?, distance_m=? WHERE id=?",
               (time.time(), minutes, distance_m, session_id))
    db.commit()


def recent_plays(db, days: int) -> set[tuple[str, str]]:
    cutoff = time.time() - days * 86400
    rows = db.execute("SELECT artist, title FROM plays WHERE ts>=?", (cutoff,)).fetchall()
    return {(r["artist"].casefold(), r["title"].casefold()) for r in rows}


def play_counts(db) -> dict[tuple[str, str], int]:
    rows = db.execute("SELECT artist, title, COUNT(*) n FROM plays GROUP BY artist, title").fetchall()
    return {(r["artist"].casefold(), r["title"].casefold()): r["n"] for r in rows}


def skip_counts(db) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    """How often each track, and each artist, was skipped on a swim (keys casefolded, like play_counts)."""
    tracks: dict[tuple[str, str], int] = {}
    artists: dict[str, int] = {}
    for r in db.execute("SELECT artist, title, COUNT(*) n FROM skips GROUP BY artist, title"):
        k = (r["artist"].casefold(), r["title"].casefold())
        tracks[k] = tracks.get(k, 0) + r["n"]
        artists[k[0]] = artists.get(k[0], 0) + r["n"]
    return tracks, artists


def discard_pending(db) -> int | None:
    """Drop the staged-but-unsettled session without scrobbling it. Returns its id."""
    row = pending_session(db)
    if row is None:
        return None
    db.execute("UPDATE sessions SET status='discarded' WHERE id=?", (row["id"],))
    db.commit()
    return row["id"]
