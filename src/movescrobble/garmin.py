"""Swims recorded on a Garmin watch: when the swim started and how long it lasted (unofficial Garmin Connect API).

Authorise once with `movescrobble garmin-auth` (asks for email, password and the MFA code if your account has one);
only the resulting tokens are stored, in ~/.config/movescrobble/garmin/. Later runs, launchd included, reuse them.
"""
from __future__ import annotations

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import config_dir, secret
from .planner import fmt_min

TOKEN_DIR = "garmin"

Fetch = Callable[[str, str], list[dict]]  # (start YYYY-MM-DD, end YYYY-MM-DD) -> activities


@dataclass(frozen=True)
class Swim:
    start: datetime  # local wall-clock time, like the rest of the app
    minutes: float  # elapsed: rests at the wall count, the music keeps playing through them
    kind: str
    distance_m: float | None = None
    activity_id: int | None = None
    utc_offset: timedelta = timedelta(0)  # local minus GMT: per-length times come in GMT
    moving_minutes: float | None = None  # swimming only, rests excluded: what pace is measured on
    avg_hr: float | None = None
    max_hr: float | None = None
    swolf: float | None = None
    hr_zones: tuple[float, ...] = ()  # seconds in heart-rate zones 1-5

    @property
    def pace(self) -> float | None:
        """Seconds per 100 m while swimming."""
        moving = self.moving_minutes or self.minutes
        return moving * 60 / (self.distance_m / 100) if self.distance_m else None

    def describe(self) -> str:
        dist = f", {self.distance_m:.0f} m" if self.distance_m else ""
        pace = f", {fmt_min(round(self.pace))}/100m" if self.pace else ""
        return f"{self.kind.replace('_', ' ')} {self.start:%a %H:%M}, {self.minutes:.0f} min{dist}{pace}"


@dataclass(frozen=True)
class Run:
    activity_id: int
    start_ts: float  # unix time
    seconds: float  # elapsed: the music keeps playing through stops


@dataclass(frozen=True)
class Length:
    """One pool length as the watch recorded it."""

    start: datetime  # local
    seconds: float
    distance_m: float
    hr: float | None = None
    swolf: float | None = None
    strokes: int | None = None
    stroke: str | None = None  # FREESTYLE, BREASTSTROKE, ...


def _store():
    d = config_dir() / TOKEN_DIR
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def authorised() -> bool:
    d = config_dir() / TOKEN_DIR
    return d.is_dir() and any(d.iterdir())


def client(ask: Callable[[str], str] | None = None, ask_secret: Callable[[str], str] | None = None):
    """Logged-in client. With `ask`, missing credentials and the MFA code are prompted for (garmin-auth);
    without it, saved tokens are used (falling back to GARMIN_EMAIL / GARMIN_PASSWORD from secrets)."""
    from garminconnect import Garmin  # heavy; only when Garmin is actually used

    email, password = secret("GARMIN_EMAIL"), secret("GARMIN_PASSWORD")
    if ask:
        email = email or ask("Garmin Connect email: ")
        password = password or (ask_secret or ask)("Garmin Connect password: ")
    g = Garmin(email, password, prompt_mfa=(lambda: ask("Garmin MFA code: ")) if ask else None)
    g.login(str(_store()))  # loads saved tokens, or logs in and saves new ones
    return g


def finder(cfg):
    """swim_between when Garmin is enabled and authorised, else None (callers then ask or use the plan)."""
    return swim_between if cfg.garmin and authorised() else None


@functools.cache
def _saved_client():
    """One login per process: each login also fetches the profile and settings (extra round trips)."""
    return client()


def fetch_swims(start: str, end: str) -> list[dict]:
    return _saved_client().get_activities_by_date(start, end, "swimming")


def parse(activity: dict) -> Swim | None:
    kind = (activity.get("activityType") or {}).get("typeKey", "")
    seconds = activity.get("elapsedDuration") or activity.get("duration")
    if "swim" not in kind or not seconds or not activity.get("startTimeLocal"):
        return None
    start = datetime.fromisoformat(activity["startTimeLocal"])
    gmt = activity.get("startTimeGMT")
    moving = activity.get("movingDuration")
    zones = tuple(activity.get(f"hrTimeInZone_{i}") or 0.0 for i in range(1, 6))
    return Swim(start, seconds / 60, kind, activity.get("distance"), activity.get("activityId"),
                start - datetime.fromisoformat(gmt) if gmt else timedelta(0), moving / 60 if moving else None,
                activity.get("averageHR"), activity.get("maxHR"), activity.get("averageSwolf"),
                zones if any(zones) else ())


def fetch_runs(start: str, end: str) -> list[dict]:
    return _saved_client().get_activities_by_date(start, end, "running")


def parse_run(activity: dict) -> Run | None:
    kind = (activity.get("activityType") or {}).get("typeKey", "")
    seconds = activity.get("elapsedDuration") or activity.get("duration")
    gmt = activity.get("startTimeGMT")
    if "running" not in kind or not seconds or not gmt or not activity.get("activityId"):
        return None
    start = datetime.fromisoformat(gmt).replace(tzinfo=timezone.utc)  # scrobbles are absolute times; a run abroad too
    return Run(activity["activityId"], start.timestamp(), seconds)


def parse_lengths(splits: dict, utc_offset: timedelta) -> list[Length]:
    """Swum lengths from an activity's splits (rests and drills without distance are left out)."""
    return [
        Length(datetime.fromisoformat(n["startTimeGMT"]) + utc_offset, n["duration"], n["distance"],
               n.get("averageHR"), n.get("averageSWOLF"), n.get("totalNumberOfStrokes"), n.get("swimStroke"))
        for lap in splits.get("lapDTOs") or [] for n in lap.get("lengthDTOs") or []
        if n.get("distance") and n.get("duration") and n.get("startTimeGMT")
    ]


def fetch_lengths(swim: Swim) -> list[Length]:
    return parse_lengths(_saved_client().get_activity_splits(swim.activity_id), swim.utc_offset)


SOUNDTRACK_MARK = "♫ Swam to"
RUN_SOUNDTRACK_MARK = "♫ Ran to"


def with_soundtrack(description: str, soundtrack: str) -> str:
    """Your own text stays; an earlier soundtrack (from a re-run) is replaced."""
    own = re.split(f"{SOUNDTRACK_MARK}|{RUN_SOUNDTRACK_MARK}", description, maxsplit=1)[0].rstrip()
    return f"{own}\n\n{soundtrack}" if own else soundtrack


def write_soundtrack(activity_id: int, soundtrack: str) -> None:
    g = _saved_client()
    g.set_activity_description(activity_id, with_soundtrack(g.get_activity(activity_id).get("description") or "",
                                                            soundtrack))


def swim_between(since: datetime, until: datetime, fetch: Fetch = fetch_swims) -> Swim | None:
    """The first swim that started inside the window (the staged playlist plays from its start)."""
    swims = [s for a in fetch(since.date().isoformat(), until.date().isoformat())
             if (s := parse(a)) and since <= s.start <= until]
    return min(swims, key=lambda s: s.start, default=None)
