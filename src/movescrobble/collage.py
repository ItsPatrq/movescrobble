"""Reel collages: your photos cut into shapes and graded dark, in many versions to choose from.

Each run draws new versions: a layout (how the photos are cut), a look (the grade) and, if you gave a peek photo, where
that photo hides. A peek photo is on every version but never the point of it: off-centre, smaller than the others, and in
a reel it appears early, then later pictures partly cover it. Output carries no EXIF, so no location leaves with it.
"""

from __future__ import annotations

import colorsys
import json
import math
import random
import secrets
import shutil
import subprocess
import textwrap
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

W, H = 1080, 1920
FPS = 30
SAFE_TOP, SAFE_BOTTOM, SAFE_RIGHT = 220, 380, 130  # Instagram's reel UI covers these edges
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
# Of the frame, visible part; the peek stays a detail
PEEK_SHARE = {"subtle": (0.03, 0.06), "bold": (0.08, 0.15)}
PEEK_MIN_COVERED, PEEK_MAX_COVERED = 0.2, 0.5  # how much of its own shape other pieces hide
PEEK_DIMMED = 0.74
PEEK_SPAN = (0.13, 1.0)  # of the peek photo's height, what its crop may hold: from the shorts down, at most to its bottom
PEEK_QUIET = 0.92  # a body photo sits a little under the others' brightness, so that it does not lead
PEEK_MATCH = 0.72  # how far a peek photo's colours move towards the other photos
RUNS_HOME = Path.home() / "Movies" / "movescrobble"
PEEK_ASIDE = 0.14  # from the middle of the photo, of its width: where a leg stands on someone facing the camera
SLIP_MARGIN = 100  # spare picture around a frame-sized crop, so a piece can slip without running out of image
CLARITY, S_CURVE, VIBRANCE = 0.1, 0.14, 0.06  # how slight the `natural` edit is
MUSCLE_DEFINITION = 0.6
RECIPE_FORMAT = 1
TYPE_COLOUR, ROUTE_COLOUR = (246, 242, 232), (250, 245, 230)
ACCENT_LIFT = 0.65
WARM_GRADIENT = 0.4  # how much of a photo's tones come from WARM_TONES
WARM_TONES = (  # brightness, then red, green, blue at that brightness: the warm dark of a low sun
    (0.0, 0.12, 0.35, 0.6, 1.0),
    (0.03, 0.2, 0.52, 0.82, 1.0),
    (0.018, 0.115, 0.31, 0.56, 0.88),
    (0.01, 0.065, 0.19, 0.36, 0.72),
)
WARM_HUES, DUSK_HUES = (18.0, 40.0), (-15.0, 15.0)  # degrees a photo set's palette may land in
MAX_PIECES = 6  # more than this reads as clutter, however many photos there are
FEW_PHOTOS = 3  # up to this many: pieces cut one continuous picture, because repeated crops read as wallpaper
UNDERLAY_LIGHT = (0.68, 0.88)  # the picture under the pieces: dimmer than them, so they stay separate, never black
READABLE_TYPE = 0.4  # alpha above this is type to keep clear
ROUTE_TRIM_M = 500  # taken off both ends of a route: they are usually your front door
DATA_FIELDS = ("date", "distance", "time", "pace", "speed", "hr", "climb", "route")
FONTS = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",  # bold: hairlines vanish on a busy picture
    "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
    "/System/Library/Fonts/Supplemental/Futura.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
]
CARD_FONTS = [  # condensed and bold: a column of figures reads at a glance
    "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Narrow Bold.ttf",
    *FONTS,
]

LOOKS = {  # sat: colour kept; exposure and contrast shape the tone; tints push shadows and highlights
    "natural": dict(
        sat=1.0,
        exposure=1.0,
        contrast=1.0,
        lift=0.0,
        shadow=(0.0, 0.0, 0.0),
        high=(0.0, 0.0, 0.0),
        halation=0.0,
        grain=0.008,
        vignette=0.22,
    ),
    "noir": dict(
        sat=0.0,
        exposure=0.80,
        contrast=1.38,
        lift=0.012,
        shadow=(0.0, 0.004, 0.012),
        high=(0.012, 0.008, 0.0),
        halation=0.03,
        grain=0.055,
    ),
    "subway": dict(
        sat=0.3,
        exposure=0.7,
        contrast=1.2,
        lift=0.02,
        shadow=(-0.008, 0.035, 0.03),
        high=(0.015, 0.018, -0.012),
        halation=0.07,
        grain=0.04,
    ),
    "tungsten": dict(
        sat=0.34,
        exposure=0.68,
        contrast=1.16,
        lift=0.024,
        shadow=(0.03, 0.012, -0.004),
        high=(0.05, 0.02, -0.03),
        halation=0.14,
        grain=0.038,
    ),
    "bleach": dict(
        sat=0.18,
        exposure=0.72,
        contrast=1.42,
        lift=0.008,
        shadow=(0.0, 0.006, 0.012),
        high=(0.01, 0.01, 0.0),
        halation=0.05,
        grain=0.05,
    ),
    "sodium": dict(
        sat=0.28,
        exposure=0.66,
        contrast=1.24,
        lift=0.016,
        shadow=(-0.01, 0.008, 0.042),
        high=(0.075, 0.03, -0.04),
        halation=0.16,
        grain=0.042,
    ),
    "soho": dict(
        sat=0.36,
        exposure=0.66,
        contrast=1.04,
        lift=0.06,
        shadow=(-0.012, 0.022, 0.038),
        high=(0.018, 0.008, -0.012),
        halation=0.09,
        grain=0.032,
    ),
    "sunset": dict(
        sat=1.0,
        exposure=1.0,
        contrast=1.0,
        lift=0.006,
        shadow=(0.012, 0.006, -0.004),
        high=(0.02, 0.006, -0.012),
        halation=0.1,
        grain=0.018,
    ),
    "chrome": dict(
        sat=0.95,
        exposure=0.78,
        contrast=1.22,
        lift=0.012,
        shadow=(0.0, 0.006, 0.026),
        high=(0.03, 0.012, -0.018),
        halation=0.08,
        grain=0.03,
    ),
    "neon": dict(
        sat=1.08,
        exposure=0.74,
        contrast=1.24,
        lift=0.014,
        shadow=(0.008, 0.0, 0.05),
        high=(0.035, 0.0, 0.02),
        halation=0.22,
        grain=0.034,
    ),
    "golden": dict(
        sat=0.88,
        exposure=0.78,
        contrast=1.14,
        lift=0.02,
        shadow=(0.022, 0.008, -0.004),
        high=(0.065, 0.028, -0.036),
        halation=0.14,
        grain=0.03,
    ),
    "teal": dict(
        sat=0.84,
        exposure=0.76,
        contrast=1.2,
        lift=0.016,
        shadow=(-0.012, 0.032, 0.044),
        high=(0.06, 0.022, -0.03),
        halation=0.1,
        grain=0.032,
    ),
}
COLOUR = ("chrome", "neon", "golden", "teal")
UNIFIED = "sunset"  # every photo pulled to one palette and one set of levels before the grade
NATURAL = "natural"  # the photo's own colours, only slightly enhanced
MUTED = tuple(look for look in LOOKS if look not in (*COLOUR, UNIFIED, NATURAL))
LOOK_GROUPS = {"colour": COLOUR, "muted": MUTED}


@dataclass
class Piece:
    shape: dict
    photo: int = 0  # index into the photos of this version
    full: bool = False  # crop for the whole frame, so neighbouring pieces continue one picture
    shift: tuple[int, int] = (0, 0)  # how far a `full` piece slips out of register
    blend: str = "normal"  # or "screen": a double exposure
    opacity: float = 1.0
    shadow: float = 0.0
    body: bool = False  # the cell this composition offers to a peek photo
    own_crop: bool = False  # always frames the photo for its own shape, however few photos there are
    calm: bool = False  # shows the calmest photo: a curved shape only reads when its picture is quieter than its surroundings
    one_leg: bool = False  # as a body cell, frames one leg even if both would fit
    peek: bool = False

    @property
    def mask(self) -> np.ndarray:
        return rasterise(self.shape)


@dataclass
class Version:
    id: str
    seed: int
    layout: str
    look: str
    photos: list[str]
    peek_photo: str | None = None
    peek_share: float = 0.0  # of the frame, as it ended up visible
    peek_side: str = "either"  # which side of the peek photo to frame
    text: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)  # which Garmin fields this version shows
    define: list[str] = field(default_factory=list)  # which of `photos` get more visible muscle
    still: str = ""
    reel: str = ""
    recipe: str = ""  # its file: everything about this version, to be edited


@dataclass(frozen=True)
class ActivityData:
    """What a collage can say about a Garmin activity."""

    kind: str = ""
    name: str = ""  # what Garmin calls it, such as "Warsaw Running"
    date: str = ""
    distance_m: float | None = None
    seconds: float | None = None  # moving time when Garmin has it, so that time, distance and pace agree
    speed_ms: float | None = None
    hr: float | None = None
    climb_m: float | None = None
    total_s: float | None = None  # the watch's timer, stops while running included: a card's total time
    average_speed_ms: float | None = None  # over the timer's time: a card's average pace
    route: tuple[tuple[float, float], ...] = ()  # (lat, lon), ends already trimmed

    @property
    def swim(self) -> bool:
        return "swim" in self.kind

    def stat(self, name: str) -> str | None:
        """One field as it reads on the picture; None when this activity does not have it."""
        if name == "date":
            return self.date or None
        if name == "distance" and self.distance_m:
            return f"{self.distance_m:.0f} m" if self.swim else f"{self.distance_m / 1000:.2f} km"
        if name == "time" and self.seconds:
            return clock(self.seconds)
        if name == "pace" and self.speed_ms:
            return f"{clock(100 / self.speed_ms)} /100m" if self.swim else f"{clock(1000 / self.speed_ms)} /km"
        if name == "speed" and self.speed_ms and not self.swim:
            return f"{self.speed_ms * 3.6:.1f} km/h"
        if name == "hr" and self.hr:
            return f"{self.hr:.0f} bpm"
        if name == "climb" and self.climb_m and self.climb_m >= 1:
            return f"{self.climb_m:.0f} m up"
        return None

    def card_lines(self) -> list[str]:
        """The card's stats, as LABEL|figure|unit: total time and average pace by the watch's timer, as Garmin shows them."""
        lines = []
        if self.distance_m:
            lines.append(f"Distance|{self.distance_m:.0f}|m" if self.swim else f"Distance|{self.distance_m / 1000:.2f}|km")
        if total := self.total_s or self.seconds:
            lines.append(f"Total time|{clock(total)}|")
        if speed := self.average_speed_ms or self.speed_ms:
            lines.append(f"Avg pace|{clock(100 / speed)}|/100m" if self.swim else f"Avg pace|{clock(1000 / speed)}|/km")
        return lines

    def available(self) -> list[str]:
        return [f for f in DATA_FIELDS if (bool(self.route) if f == "route" else self.stat(f) is not None)]


def clock(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{rest:02d}" if hours else f"{minutes}:{rest:02d}"


def pick_fields(data: ActivityData, rng: random.Random) -> list[str]:
    """A different selection each version, some with no data at all. Heart rate only sometimes; it is the personal one."""
    has = set(data.available())
    if not has or rng.random() < 0.2:
        return []
    rhythm = "speed" if "cycling" in data.kind or "ride" in data.kind else "pace"
    chances = {"distance": 0.9, rhythm: 0.7, "time": 0.55, "date": 0.4, "hr": 0.35, "route": 0.5, "climb": 0.1}
    if rhythm == "pace":
        chances["speed"] = 0.2
    chosen = [f for f in DATA_FIELDS if f in has and rng.random() < chances.get(f, 0.0)]
    return chosen or [next(f for f in DATA_FIELDS if f in has)]


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def trim_route(points: list[tuple[float, float]], metres: float = ROUTE_TRIM_M) -> list[tuple[float, float]]:
    """A route without its ends. Too short to survive that: no route, rather than one that points at your door."""
    if metres <= 0 or not points:
        return points
    along = [0.0]
    for a, b in zip(points, points[1:], strict=False):
        along.append(along[-1] + _metres(a, b))
    kept = [p for p, d in zip(points, along, strict=True) if metres <= d <= along[-1] - metres]
    return kept if len(kept) >= 8 else []


def activity_data(detail: dict, details: dict | None = None, trim: float = ROUTE_TRIM_M) -> ActivityData:
    """From Garmin's `get_activity` and `get_activity_details` answers."""
    summary = detail.get("summaryDTO") or {}
    local = summary.get("startTimeLocal")
    polyline = ((details or {}).get("geoPolylineDTO") or {}).get("polyline") or []
    points = [(float(q["lat"]), float(q["lon"])) for q in polyline if q.get("lat") is not None and q.get("lon") is not None]
    return ActivityData(
        kind=(detail.get("activityTypeDTO") or {}).get("typeKey", ""),
        name=detail.get("activityName") or "",
        date=datetime.fromisoformat(local).strftime("%d %b %Y") if local else "",
        distance_m=summary.get("distance"),
        seconds=summary.get("movingDuration") or summary.get("elapsedDuration") or summary.get("duration"),
        speed_ms=summary.get("averageMovingSpeed") or summary.get("averageSpeed"),
        hr=summary.get("averageHR"),
        climb_m=summary.get("elevationGain"),
        total_s=summary.get("duration") or summary.get("elapsedDuration"),  # paused time is in elapsed only
        average_speed_ms=summary.get("averageSpeed"),
        route=tuple(trim_route(points, trim)),
    )


# --- photos ------------------------------------------------------------------------------------------------------


def find_photos(paths: list[Path]) -> list[Path]:
    """The photos at `paths`; a folder counts for the photos directly in it."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in PHOTO_EXTS and not f.name.startswith("."))
        elif p.suffix.lower() in PHOTO_EXTS and p.exists():
            out.append(p)
        else:
            raise SystemExit(f"not a photo or a folder: {p}")
    return list(dict.fromkeys(out))


def load_photo(path: Path, longest: int = 2600) -> Image.Image:
    """Upright (phones store orientation apart from the pixels), RGB, no bigger than it needs to be."""
    if path.suffix.lower() in (".heic", ".heif"):
        from pillow_heif import register_heif_opener

        register_heif_opener()
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return img


def focus_of(img: Image.Image) -> tuple[float, float]:
    """Where the detail is, 0..1: crops lean towards it rather than cutting the picture blindly in the middle."""
    a = np.asarray(img.convert("L").resize((64, 64)), dtype=np.float32)
    weight = np.abs(np.diff(a, axis=0, prepend=a[:1])) + np.abs(np.diff(a, axis=1, prepend=a[:, :1])) + 1
    ys, xs = np.mgrid[0:64, 0:64]
    return float((xs * weight).sum() / weight.sum() / 63), float((ys * weight).sum() / weight.sum() / 63)


def detail_of(img: Image.Image) -> float:
    """How busy a photo is: a sky scores low, leaves against a sky high."""
    a = np.asarray(img.convert("L").resize((256, 256), Image.Resampling.BILINEAR), dtype=np.float32) / 255
    return float(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())


# --- shapes ------------------------------------------------------------------------------------------------------
#
# A shape is data, so that a recipe can hold it and the editor can change it: {"type": ...} with the fields rasterise reads.


def _soft(d: np.ndarray) -> np.ndarray:
    return np.clip(d + 0.5, 0, 1)


def _size(scale: float) -> tuple[int, int]:
    return max(1, round(W * scale)), max(1, round(H * scale))


@lru_cache(maxsize=4)
def _frame_grid(scale: float) -> tuple[np.ndarray, np.ndarray]:
    """Frame coordinates of each output pixel's centre."""
    w, h = _size(scale)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    xs, ys = (xs + 0.5) * (W / w) - 0.5, (ys + 0.5) * (H / h) - 0.5
    xs.setflags(write=False), ys.setflags(write=False)
    return xs, ys


def _draw_shape(shape: dict, scale: float) -> np.ndarray:
    kind = shape["type"]
    w, h = _size(scale)
    if kind == "full":
        return np.ones((h, w), dtype=np.float32)
    if kind == "poly":
        over = 1 if scale >= 1 else 3  # smaller than the frame: drawn large, then reduced, so edges stay clean
        img = Image.new("L", (w * over, h * over), 0)
        ImageDraw.Draw(img).polygon([(x * w * over / W, y * h * over / H) for x, y in shape["points"]], fill=255)
        img = img.filter(ImageFilter.GaussianBlur(0.8)) if over == 1 else img.resize((w, h), Image.Resampling.BOX)
        return np.asarray(img, dtype=np.float32) / 255
    if kind == "union":
        return np.max([rasterise(part, scale) for part in shape["parts"]], axis=0)
    if kind == "minus":
        out = rasterise(shape["base"], scale)
        for cut in shape["cuts"]:
            out = out * (1 - rasterise(cut, scale))
        return out
    if kind == "noise":
        gen = np.random.default_rng(shape["seed"])
        small = gen.random((H // shape["cell"] + 2, W // shape["cell"] + 2), dtype=np.float32)
        drift = np.asarray(Image.fromarray(small).resize((w, h), Image.Resampling.BICUBIC), dtype=np.float32).clip(0, 1)
        return np.clip((drift - shape["low"]) * shape["gain"], 0, 1)
    xs, ys = _frame_grid(scale)
    if kind == "disc":
        return _soft((shape["r"] - np.hypot(xs - shape["cx"], ys - shape["cy"])) * scale)
    if kind == "ring":  # r0..r1 around a centre; with `start` and `sweep` (radians, clockwise from east) a sector of it
        radius = np.hypot(xs - shape["cx"], ys - shape["cy"])
        out = _soft((radius - shape["r0"]) * scale) * _soft((shape["r1"] - radius) * scale)
        if "sweep" in shape:
            turn = np.mod(np.arctan2(ys - shape["cy"], xs - shape["cx"]) - shape["start"], 2 * math.pi)
            out = out * _soft((shape["sweep"] - turn) * radius * scale) * _soft(turn * radius * scale)
        return out
    if kind == "arch":
        x0, x1, top, bottom = shape["x0"], shape["x1"], shape["top"], shape["bottom"]
        r, cx = (x1 - x0) / 2, (x0 + x1) / 2
        body = (
            _soft((xs - x0) * scale) * _soft((x1 - xs) * scale) * _soft((ys - (top + r)) * scale) * _soft((bottom - ys) * scale)
        )
        return np.maximum(body, _soft((r - np.hypot(xs - cx, ys - (top + r))) * scale) * _soft((bottom - ys) * scale))
    raise ValueError(f"unknown shape: {kind}")


@lru_cache(maxsize=32)
def _rasterised(key: str, scale: float) -> np.ndarray:
    out = _draw_shape(json.loads(key), scale)
    out.setflags(write=False)
    return out


def rasterise(shape: dict, scale: float = 1.0) -> np.ndarray:
    """A shape as a mask, H x W, 0..1, at `scale` of the frame."""
    return _rasterised(json.dumps(shape, sort_keys=True), scale)


def _poly(points: list[tuple[float, float]]) -> dict:
    return {"type": "poly", "points": [[round(float(x), 1), round(float(y), 1)] for x, y in points]}


FULL = {"type": "full"}


def _clip(poly: list[tuple[float, float]], point: tuple[float, float], normal: tuple[float, float]) -> list:
    """The part of `poly` on the side of the line that `normal` points away from."""
    out = []
    side = lambda p: (p[0] - point[0]) * normal[0] + (p[1] - point[1]) * normal[1]  # noqa: E731
    for a, b in zip(poly, poly[1:] + poly[:1], strict=True):
        sa, sb = side(a), side(b)
        if sa <= 0:
            out.append(a)
        if sa * sb < 0:
            t = sa / (sa - sb)
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    return out


def shards(rng: random.Random, n: int) -> list[Piece]:
    """Shattered glass: a Voronoi split with thin seams, one or two panes missing."""
    sites = [(rng.uniform(0, W), rng.uniform(0, H)) for _ in range(n + 2)]
    gap = rng.uniform(5, 16)
    frame = [(-20.0, -20.0), (W + 20.0, -20.0), (W + 20.0, H + 20.0), (-20.0, H + 20.0)]
    pieces = []
    for i, s in enumerate(sites):
        poly = list(frame)
        for j, o in enumerate(sites):
            if i == j or not poly:
                continue
            dx, dy = o[0] - s[0], o[1] - s[1]
            length = math.hypot(dx, dy) or 1
            nx, ny = dx / length, dy / length
            poly = _clip(poly, ((s[0] + o[0]) / 2 - nx * gap, (s[1] + o[1]) / 2 - ny * gap), (nx, ny))
        if len(poly) >= 3:
            pieces.append(Piece(_poly(poly), i))
    rng.shuffle(pieces)
    return pieces[: max(n, len(pieces) - 2)]


def slashes(rng: random.Random, n: int) -> list[Piece]:
    """Diagonal cuts of uneven width that stop short: the picture slips out of register between them."""
    angle = math.radians(rng.choice((-1, 1)) * rng.uniform(58, 82))
    dx, dy, nx, ny = math.cos(angle), math.sin(angle), -math.sin(angle), math.cos(angle)
    span = abs(W * nx) + abs(H * ny)
    offset = -span / 2
    pieces: list[Piece] = []
    while offset < span / 2:
        width = rng.uniform(0.6, 1.8) * span / n
        cx, cy = W / 2 + nx * (offset + width / 2), H / 2 + ny * (offset + width / 2)
        along = rng.uniform(H * 0.34, H * 0.8)
        slide = rng.uniform(-H * 0.2, H * 0.2)
        cx, cy = cx + dx * slide, cy + dy * slide
        half = width / 2 - rng.uniform(3, 12)
        pts = [
            (cx + dx * along + nx * half, cy + dy * along + ny * half),
            (cx + dx * along - nx * half, cy + dy * along - ny * half),
            (cx - dx * along - nx * half, cy - dy * along - ny * half),
            (cx - dx * along + nx * half, cy - dy * along + ny * half),
        ]
        pieces.append(Piece(_poly(pts), len(pieces), full=rng.random() < 0.3, shift=(rng.randint(-55, 55), rng.randint(-90, 90))))
        offset += width
    return pieces


def eclipse(rng: random.Random, n: int) -> list[Piece]:
    """One picture through a disc and the broken rings around it, over another: both stay recognisable."""
    cx, cy = rng.uniform(W * 0.32, W * 0.68), rng.uniform(H * 0.32, H * 0.58)
    disc = rng.uniform(270, 390)
    register = (rng.randint(8, 40), rng.randint(8, 40))  # the rings share it, so they show one picture
    arcs = []
    r = disc + rng.uniform(14, 34)
    while r < 1100 and len(arcs) < max(2, n - 1):
        width = rng.uniform(70, 200)
        start, sweep = rng.uniform(-math.pi, math.pi), rng.uniform(1.4, 3.8)
        arcs.append(
            Piece(
                {"type": "ring", "cx": cx, "cy": cy, "r0": r, "r1": r + width, "start": start, "sweep": sweep},
                0,
                full=True,
                shift=register,
            )
        )
        r += width + rng.uniform(40, 150)
    under = Piece(FULL, 1, full=True, opacity=rng.uniform(*UNDERLAY_LIGHT))
    return [under, *arcs[::-1], Piece({"type": "disc", "cx": cx, "cy": cy, "r": disc}, 0, own_crop=True)]


def _rows(
    rng: random.Random, n: int, tilt: float = 0.0, slant: float = 0.0, plain: bool = False
) -> list[Piece]:
    """Rows of equal height across the frame, and one or two of them divided into cells. The body photo takes one cell, shaped
    like the others; its hidden margin lies under its neighbours, so that they arrive over it in a reel."""
    rows = 4 if n >= 5 and not plain and rng.random() < 0.5 else 3
    gap = rng.uniform(5, 8)
    cuts = [-40.0, *(H * i / rows + rng.uniform(-22, 22) for i in range(1, rows)), H + 40.0]
    held = rng.choice((1, 2)) if rows == 4 else 1

    def corner(x: float, y: float) -> tuple[float, float]:
        return x + slant * (y - H / 2), y + tilt * (x - W / 2)

    def cell(x0: float, x1: float, y0: float, y1: float) -> dict:
        return _poly([corner(x0, y0), corner(x1, y0), corner(x1, y1), corner(x0, y1)])

    third = W * rng.uniform(0.32, 0.36)
    one_leg = rng.random() < 0.18
    if one_leg:  # a narrower cell, which frames one leg from shorts to shoes
        third = W * rng.uniform(0.22, 0.25)
    if one_leg or plain or rng.random() >= 0.65:
        columns, legs_at = [-40.0, third, W + 40.0], 0
    else:
        columns, legs_at = [-40.0, third, W - third, W + 40.0], rng.choice((0, 0, 0, 1, 2, 2))
    planned = rows - 1 + len(columns) - 2
    spare = planned < n  # dividing another row must not make a photo come up twice
    echo = rng.choice([r for r in range(rows) if r != held and r != 0]) if spare and not plain and rng.random() < 0.6 else None
    pieces, count, body = [], 0, None
    for row in range(rows):
        top, bottom = cuts[row] + (gap if row else 0.0), cuts[row + 1]
        if row == held:
            for c in range(len(columns) - 1):
                x0, x1 = columns[c] + (gap if c else 0.0), columns[c + 1]
                if c == legs_at:
                    tuck = 26.0
                    body = Piece(
                        cell(x0 - (tuck if c else 0), x1 + (tuck if c < len(columns) - 2 else 0), top - tuck, bottom + tuck),
                        99,
                        own_crop=True,
                        body=True,
                        one_leg=one_leg,
                    )
                else:
                    pieces.append(
                        Piece(
                            cell(x0, x1, top, bottom),
                            count,
                            own_crop=True,
                            calm=not plain and c == legs_at + 1 and rng.random() < 0.5,
                        )
                    )
                    count += 1
        elif row == echo:
            middle = W * rng.choice((0.38, 0.62))
            pieces.append(Piece(cell(-40, middle, top, bottom), count, own_crop=True))
            pieces.append(Piece(cell(middle + gap, W + 40, top, bottom), count + 1, own_crop=True))
            count += 2
        else:
            pieces.append(Piece(cell(-40, W + 40, top, bottom), count, own_crop=True))
            count += 1
    return [body, *pieces]


def stack(rng: random.Random, n: int) -> list[Piece]:
    """Straight rows, one picture over another, 3 or 4 of equal height, with cells in one or two of them."""
    return _rows(rng, n)


def bands(rng: random.Random, n: int) -> list[Piece]:
    """The rows of `stack` on a slight tilt, their cells cut on a slant."""
    return _rows(rng, n, rng.choice((-1, 1)) * rng.uniform(0.015, 0.03), rng.choice((-1, 1)) * rng.uniform(0.02, 0.05))


def slants(rng: random.Random, n: int) -> list[Piece]:
    """Three columns on one slant, each broken by cross cuts that lean the other way. The body photo is one of the sections."""
    k, gap = rng.choice((-1, 1)) * rng.uniform(0.07, 0.13), rng.uniform(5, 9)
    held = rng.choice((0, 0, 0, 1))
    one_leg = held == 0 and rng.random() < 0.25  # a narrower column, which frames one leg from shorts to shoes
    a, b = W * (rng.uniform(0.22, 0.25) if one_leg else rng.uniform(0.31, 0.35)), W * rng.uniform(0.65, 0.7)
    edges = [-60.0, a, b, W + 60.0]
    fall = rng.uniform(0.05, 0.11)  # per pixel across

    def x(x0: float, y: float) -> float:
        return x0 + k * (y - H / 2)

    def section(col: int, y0: float, y1: float, grow: float = 0.0) -> dict:
        left, right = edges[col] + (gap if col else 0.0) - grow, edges[col + 1] + grow
        top, bottom = y0 - grow, y1 + grow
        at = lambda y, x0: (x(x0, y), y + fall * (x(x0, y) - edges[col]))  # noqa: E731
        return _poly([at(top, left), at(top, right), at(bottom, right), at(bottom, left)])

    y1, y2 = rng.uniform(470, 560), rng.uniform(1190, 1300)
    other_cut = rng.uniform(760, 1120)
    pieces, count, body = [], 0, None
    for col in range(3):
        if col == held:
            body = Piece(section(col, y1, y2 - gap, 22.0), 99, own_crop=True, body=True, one_leg=one_leg)
            parts = [(-80.0, y1 - gap), (y2, H + 200.0)]
        elif len(pieces) + 1 + (2 - col) + (col < held) > n and n < 6 or (col == 2 and rng.random() < 0.4):
            parts = [(-80.0, H + 200.0)]  # uncut: a further section would only bring a photo back
        else:
            parts = [(-80.0, other_cut - gap), (other_cut, H + 200.0)]
        for y0, y3 in parts:
            pieces.append(Piece(section(col, y0, y3), count, own_crop=True))
            count += 1
    return [body, *pieces]


def arcade(rng: random.Random, n: int) -> list[Piece]:
    """Three arches over the calmest picture, standing on a picture of the ground. The body photo is one of the arches."""
    base = rng.uniform(1390, 1470)
    gap = rng.uniform(16, 26)
    width = (W - 2 * 24 - 2 * gap) / 3
    held = rng.choice((0, 0, 0, 1, 2, 2))
    tops = [rng.uniform(280, 620) for _ in range(3)]
    tops[held] = base - rng.uniform(720, 790)
    pieces: list[Piece] = [Piece(FULL, 3, own_crop=True, calm=True)]
    arches, count = [], 0
    for i in range(3):
        x0 = 24 + i * (width + gap)
        shape = {"type": "arch", "x0": x0, "x1": x0 + width, "top": tops[i], "bottom": base + (30 if i == held else 2)}
        if i == held:
            arches.insert(0, Piece(shape, 99, own_crop=True, body=True))
        else:
            arches.append(Piece(shape, count, own_crop=True))
            count += 1
    ground = _poly([(-20, base), (W + 20, base + rng.uniform(-24, 24)), (W + 20, H + 20), (-20, H + 20)])
    return [*pieces, *arches, Piece(ground, 2, own_crop=True)]


def orbit(rng: random.Random, n: int) -> list[Piece]:
    """Rows of pictures with a moon hanging over them: the calmest photo seen through a disc and the even, thin rings around it.
    The body photo is a cell of the rows, and the moon comes over its corner."""
    pieces = _rows(rng, n, plain=True)
    box = np.nonzero(pieces[0].mask > 0.5)
    edge, mid = float(box[1].max()), float(box[0].mean())
    disc = rng.uniform(330, 400)
    cx, cy = edge + disc - rng.uniform(40, 70), mid + rng.choice((-1, 1)) * rng.uniform(60, 180)
    moon: list[dict] = [{"type": "disc", "cx": cx, "cy": cy, "r": disc}]
    r, seam, ring = disc, rng.uniform(14, 20), rng.uniform(14, 20)
    for _ in range(rng.randint(1, 2)):
        r += seam
        moon.append({"type": "ring", "cx": cx, "cy": cy, "r0": r, "r1": r + ring})
        r, ring = r + ring, ring * 0.5
    return [*pieces, Piece({"type": "union", "parts": moon}, len(pieces) - 1, own_crop=True, calm=True, shadow=0.3)]


def ripple(rng: random.Random, n: int) -> list[Piece]:
    """Curved rows around a centre outside the frame, the calmest photo inside. The outer row is divided into sectors, and the
    body photo is one of them."""
    cx, cy = W + rng.uniform(70, 170), rng.uniform(1020, 1200)
    inner, gap = rng.uniform(520, 600), rng.uniform(6, 9)
    outer = cx - rng.uniform(340, 390)  # the outer row is that wide at the frame's edge
    tall = rng.uniform(570, 640)
    held_y = rng.uniform(840, 980)
    centre = math.atan2(held_y - cy, -cx) % (2 * math.pi)
    half = math.atan2(tall / 2, (outer + cx) / 2)
    far = 4000.0
    sector = lambda r0, r1, a0, a1: {"type": "ring", "cx": cx, "cy": cy, "r0": r0, "r1": r1, "start": a0, "sweep": a1 - a0}  # noqa: E731
    return [
        Piece(
            sector(outer + gap - 24, far, centre - half - 0.03, centre + half + 0.03),
            99,
            own_crop=True,
            body=True,
        ),
        Piece(sector(outer + gap, far, math.pi / 2 - 0.5, centre - half - 0.006), 1, own_crop=True),
        Piece(sector(outer + gap, far, centre + half + 0.006, 3 * math.pi / 2 + 0.5), 2, own_crop=True),
        Piece({"type": "ring", "cx": cx, "cy": cy, "r0": inner + gap, "r1": outer}, 3, own_crop=True),
        Piece({"type": "disc", "cx": cx, "cy": cy, "r": inner}, 0, own_crop=True, calm=True),
    ]


def stairs(rng: random.Random, n: int) -> list[Piece]:
    """Sheared panes zig-zagging down the frame, like a fire escape seen from below."""
    pieces, skew = [], rng.choice((-1, 1)) * rng.uniform(110, 300)
    left = rng.random() < 0.5
    for i in range(n):
        h = H * rng.uniform(0.9, 1.5) / n
        y = H * i / n - h * rng.uniform(0.0, 0.18)
        w = W * rng.uniform(0.5, 0.85)
        x = rng.uniform(-90, 40) if left else W - w + rng.uniform(-40, 90)
        s = skew if left else -skew
        pieces.append(Piece(_poly([(x + s, y), (x + w + s, y), (x + w, y + h), (x, y + h)]), i, shadow=0.4))
        left = not left
    return pieces


def ghost(rng: random.Random, n: int) -> list[Piece]:
    """Double exposures melting into each other, cut through by a few hard slashes."""
    pieces = [Piece(FULL, 0, full=True)]
    for i in range(1, 3):
        drift = {
            "type": "noise",
            "seed": rng.getrandbits(32),
            "cell": rng.randint(280, 620),
            "low": rng.uniform(0.3, 0.5),
            "gain": 3.2,
        }
        pieces.append(Piece(drift, i, full=True, blend="screen", opacity=rng.uniform(0.62, 0.9)))
    for piece in slashes(rng, max(3, n // 2))[: rng.randint(2, 3)]:
        pieces.append(replace(piece, photo=3 + piece.photo))
    return pieces


def blinds(rng: random.Random, n: int) -> list[Piece]:
    """Venetian-blind light: one picture sliced into slipping louvres, another showing between them."""
    tilt = rng.uniform(-0.11, 0.11)
    pieces = [Piece(FULL, 0, full=True, opacity=rng.uniform(*UNDERLAY_LIGHT))]
    y = -abs(tilt) * W
    while y < H + abs(tilt) * W:
        thick, gap = rng.uniform(26, 150), rng.uniform(8, 60)
        pts = [(-20, y - tilt * 540), (W + 20, y + tilt * 540), (W + 20, y + tilt * 540 + thick), (-20, y - tilt * 540 + thick)]
        pieces.append(Piece(_poly(pts), 1, full=True, shift=(rng.randint(-70, 70), 0)))
        y += thick + gap
    return pieces


LAYOUTS: dict[str, Callable[[random.Random, int], list[Piece]]] = {
    "stack": stack,
    "ripple": ripple,
    "bands": bands,
    "slants": slants,
    "arcade": arcade,
    "orbit": orbit,
    "shards": shards,
    "slashes": slashes,
    "eclipse": eclipse,
    "stairs": stairs,
    "ghost": ghost,
    "blinds": blinds,
}


# What a run draws from; the wilder ones are there to ask for
CALM_LAYOUTS = ("stack", "bands", "slants", "arcade", "orbit", "ripple")


def _with_underlay(pieces: list[Piece], rng: random.Random) -> list[Piece]:
    """A whole picture under the pieces, so what shows between them is a photo and not black."""
    if pieces[0].shape["type"] == "full":
        return pieces
    under = Piece(FULL, len(pieces), full=True, opacity=rng.uniform(*UNDERLAY_LIGHT))
    return [under, *pieces]


def _peek_piece(rng: random.Random, level: str) -> Piece:
    """A shape for the peek photo: a tall slit off to one side, clear of Instagram's UI."""
    low, high = PEEK_SHARE[level]
    area = rng.uniform(low, high) * 1.5 * W * H  # a part of it ends up covered
    h = rng.uniform(1000, 1150)
    w = min(area / h, W * 0.42)
    left_side = rng.random() < 0.5
    x = rng.uniform(0, W * 0.06) if left_side else rng.uniform(W * 0.6, max(W * 0.6, W - SAFE_RIGHT - w))
    y = rng.uniform(SAFE_TOP + 10, max(SAFE_TOP + 20, H - SAFE_BOTTOM - h - 10))
    a = rng.uniform(-0.09, 0.09)
    cx, cy, c, s = x + w / 2, y + h / 2, math.cos(a), math.sin(a)
    corners = [
        (cx + px * c - py * s, cy + px * s + py * c)
        for px, py in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))
    ]
    return Piece(_poly(corners), peek=True)


# --- grade -------------------------------------------------------------------------------------------------------


def luma(a: np.ndarray) -> np.ndarray:
    return a @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


def _grade_params(look: str, rng: random.Random) -> dict:
    """A look with this version's small variations, as plain numbers."""
    p = {k: (v * rng.uniform(0.9, 1.1) if isinstance(v, float) else v) for k, v in LOOKS[look].items()}
    if look == NATURAL:  # its colours are the photo's: no variation between versions
        p = dict(LOOKS[look])
    vignette = rng.uniform(0.42, 0.62) * p.get("vignette", 1.0)
    return {**{k: list(v) if isinstance(v, tuple) else v for k, v in p.items()}, "vignette": vignette}


def apply_grade(a: np.ndarray, p: dict, scale: float = 1.0) -> np.ndarray:
    """Dark, low-saturation film: crushed but not dead blacks, split tones, light bleeding from the highlights."""
    y = luma(a)[..., None]
    a = y + (a - y) * p["sat"]
    a = a * p["exposure"]
    a = np.clip((a - 0.36) * p["contrast"] + 0.36, 0, 1)  # pivot below mid-grey: the contrast darkens more than it lights
    y = luma(a)[..., None]
    a = a + (1 - y) ** 2 * np.array(p["shadow"], dtype=np.float32) + y**2 * np.array(p["high"], dtype=np.float32)
    if p["halation"]:
        bright = np.clip((y - 0.62) * 3, 0, 1)[..., 0]
        glow = _blurred(bright, 26 * scale)
        a = a + glow[..., None] * np.array((1.0, 0.42, 0.26), dtype=np.float32) * p["halation"]
    xs, ys = _frame_grid(scale)
    corner = ((xs / W - 0.5) ** 2 + (ys / H - 0.5) ** 2) * 2
    a = a * (1 - np.clip(corner, 0, 1)[..., None] ** 1.3 * p["vignette"])
    return np.clip(a * (1 - p["lift"]) + p["lift"], 0, 1)


def _blurred(y: np.ndarray, radius: float) -> np.ndarray:
    img = _to_image(y).filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(img, dtype=np.float32) / 255


def _relit(a: np.ndarray, y: np.ndarray, new_y: np.ndarray) -> np.ndarray:
    """`a` at new brightness, hues untouched."""
    return np.clip(a * (np.clip(new_y, 0, 1) / np.maximum(y, 1e-3))[..., None], 0, 1)


def enhance(img: Image.Image) -> Image.Image:
    """A slight edit, the kind a phone offers: a little local contrast, a gentle S-curve, a touch more colour where there
    is little. Hues stay as they were shot."""
    a = np.asarray(img, dtype=np.float32) / 255
    y = luma(a)
    clear = y + (y - _blurred(y, max(img.size) * 0.012)) * CLARITY
    curved = clear - S_CURVE * np.sin(2 * np.pi * np.clip(clear, 0, 1)) / (2 * np.pi)
    a = _relit(a, y, curved)
    y = luma(a)[..., None]
    grey = 1 - (a.max(-1, keepdims=True) - a.min(-1, keepdims=True))
    a = y + (a - y) * (1 + VIBRANCE * grey)
    return _to_image(a)


def define_muscle(img: Image.Image, strength: float = MUSCLE_DEFINITION) -> Image.Image:
    """More visible muscle, in light only: nothing moves or changes shape. Deepens the shading at the scale of muscle
    on skin, and leaves finer detail such as hair and pores, and everything that is not skin, as it was."""
    a = np.asarray(img, dtype=np.float32) / 255
    y = luma(a)
    size = max(img.size)
    band = _blurred(y, size * 0.004) - _blurred(y, size * 0.028)
    mask = _blurred(_skin(img).astype(np.float32), size * 0.006)
    out = _relit(a, y, y + band * strength * mask)
    return _to_image(out)


def palette_of(photos: list[Image.Image]) -> tuple[float, float]:
    """The sunset these photos were taken in, as hues in degrees: (its warm light, its dusk sky, negative = below red)."""
    hues, sats, values = [], [], []
    for photo in photos:
        hsv = np.asarray(photo.convert("HSV").resize((128, 128)), dtype=np.float32) / 255
        hues.append(hsv[..., 0].ravel() * 360), sats.append(hsv[..., 1].ravel()), values.append(hsv[..., 2].ravel())
    h, sat, val = np.concatenate(hues), np.concatenate(sats), np.concatenate(values)
    warm = h[(h >= 10) & (h <= 55) & (sat > 0.4) & (val > 0.25)]
    dusk = (h - 360)[(h >= 300) & (sat > 0.12) & (val > 0.4)]
    return (
        float(np.clip(np.median(warm), *WARM_HUES)) if warm.size > 50 else 28.0,
        float(np.clip(np.median(dusk), *DUSK_HUES)) if dusk.size > 50 else 8.0,
    )


def unify(img: Image.Image, palette: tuple[float, float], middle: float = 0.11) -> Image.Image:
    """One photo in the palette and levels of the rest: warm light stays, greens go olive, blue skies go dusky brown and
    grey walls take the warm cast, then the tones land on the same curve whatever the photo's exposure was."""
    warm, dusk = palette
    img = img.copy()
    img.thumbnail((2000, 2000), Image.Resampling.BILINEAR)
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32) / 255
    hue, sat, val = hsv[..., 0] * 360, hsv[..., 1], hsv[..., 2]
    new_hue = np.interp(hue, (0, 30, 60, 100, 170, 220, 270, 330, 360), (8, warm, 38, 48, 50, 18, dusk, dusk + 4, 8)) % 360
    keep = np.interp(hue, (0, 60, 100, 170, 220, 270, 330, 360), (1.0, 1.0, 0.55, 0.5, 0.7, 0.8, 1.0, 1.0))
    kept = np.minimum(sat * keep, 0.9)
    a = (
        np.asarray(
            Image.fromarray((np.stack([new_hue / 360, kept, val], axis=-1) * 255).astype(np.uint8), "HSV").convert("RGB"),
            dtype=np.float32,
        )
        / 255
    )
    cast = np.array(colorsys.hsv_to_rgb(warm / 360, 0.42, 1.0), dtype=np.float32)
    grey = np.clip(1 - sat / 0.22, 0, 1)[..., None] * 0.9
    a = a * (1 - grey) + val[..., None] * cast * grey
    y = luma(a)
    low, mid, high = (float(v) for v in np.percentile(y[::4, ::4], (2, 50, 97)))
    if not low < mid < high:
        low, mid, high = 0.02, max(0.03, mid), max(0.04, mid + 0.01)
    target = np.interp(y, (low, mid, high, max(high + 1e-3, 1.0)), (0.012, middle, 0.5, 0.88))
    a = a * (target / np.maximum(y, 1e-3))[..., None]
    # The one thing that glows: strongly coloured warm light, like a low sun in an otherwise quiet picture
    warm_light = ((new_hue < 48) | (new_hue > 340)) * np.clip((kept - 0.45) / 0.3, 0, 1)
    # Every tone on one warm gradient, so nothing stays grey; strong colour keeps more of itself
    after = luma(np.clip(a, 0, 1))
    tones = np.stack([np.interp(after, WARM_TONES[0], channel) for channel in WARM_TONES[1:]], axis=-1).astype(np.float32)
    weight = (WARM_GRADIENT * (1 - warm_light * 0.7))[..., None]
    a = a * (1 - weight) + tones * weight
    a = a * (1 + warm_light * ACCENT_LIFT)[..., None]
    return _to_image(a)


def add_grain(a: np.ndarray, amount: float, seed: int) -> np.ndarray:
    h, w = a.shape[:2]
    gen = np.random.default_rng(seed)
    grain = gen.normal(0, 1, (h // 2 + 1, w // 2 + 1)).astype(np.float32)
    grain = np.asarray(Image.fromarray(grain).resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32)
    return np.clip(a + grain[..., None] * amount * (0.55 + (1 - luma(a)[..., None]) * 0.6), 0, 1)


# --- composing ---------------------------------------------------------------------------------------------------


def harmonise(img: Image.Image, others: list[Image.Image], amount: float = PEEK_MATCH) -> Image.Image:
    """`img` moved part of the way to the colour and contrast of `others`, so that it belongs with them."""
    sample = lambda p: np.asarray(p.convert("RGB").resize((96, 96)), dtype=np.float32).reshape(-1, 3) / 255  # noqa: E731
    theirs = np.concatenate([sample(p) for p in others])
    mine = sample(img)
    a = np.asarray(img.convert("RGB"), dtype=np.float32) / 255
    moved = theirs.mean(0) + (a - mine.mean(0)) * (theirs.std(0) / np.maximum(mine.std(0), 1e-3))
    out = a * (1 - amount) + moved * amount
    return _to_image(out)


def _skin(img: Image.Image, least_colour: float = 0.12) -> np.ndarray:
    hsv = np.asarray(img.convert("HSV"), dtype=np.float32) / 255
    hue, sat, val = hsv[..., 0] * 360, hsv[..., 1], hsv[..., 2]
    return ((hue < 50) | (hue > 345)) & (sat > least_colour) & (sat < 0.72) & (val > 0.2)


def leg_columns(img: Image.Image) -> list[tuple[float, float]]:
    """Where the legs stand across the photo, left to right, 0..1: the two widest runs of skin between shorts and socks."""
    small = img.resize((200, 267), Image.Resampling.BILINEAR)
    share = _skin(small, 0.33)[80:160].mean(0)  # stricter than for shading: a warm wall must not pass as a leg
    runs, start = [], None
    for x, on in enumerate([*(share > 0.4), False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start >= 10:
                runs.append((start / 200, x / 200))
            start = None
    return sorted(sorted(runs, key=lambda run: run[1] - run[0])[-2:])


def shoe_line(img: Image.Image) -> float:
    """How far down the photo the shoes reach, 0..1: dark rows from the socks on, until the dark stops; below is floor."""
    y = luma(np.asarray(img.convert("RGB").resize((200, 267), Image.Resampling.BILINEAR), dtype=np.float32) / 255)
    dark = (y[:, 30:170] < 0.16).mean(1) > 0.03
    last = None
    for row in range(130, 267):
        if dark[row]:
            last = row
        elif last is not None and row - last > 8:
            break
    return 1.0 if last is None else min(1.0, last / 267 + 0.025)


def _beside(focus: tuple[float, float], rng: random.Random, side: str = "either") -> tuple[float, float]:
    """Off to one side of the photo's middle: on someone facing the camera, that is one whole leg rather than the gap between."""
    step = {"left": -1, "right": 1}.get(side) or rng.choice((-1, 1))
    return 0.5 + step * PEEK_ASIDE, focus[1]


@lru_cache(maxsize=32)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size) if path and Path(path).exists() else ImageFont.load_default(size)


def card_font() -> str:
    return next((f for f in CARD_FONTS if Path(f).exists()), "")


def available_fonts() -> list[str]:
    """Fonts on this machine; "" is Pillow's own, which exists everywhere."""
    return [f for f in FONTS if Path(f).exists()] or [""]


def _block(lines: list[str], font: ImageFont.FreeTypeFont, spacing: float = 1.3) -> Image.Image | None:
    block = Image.new("L", (W * 2, max(300, round(font.size * spacing * 1.1 * len(lines)) + 20)), 0)
    draw = ImageDraw.Draw(block)
    y = 0
    for line in lines:
        draw.text((0, y), line.upper(), fill=255, font=font, stroke_width=max(1, font.size // 40), stroke_fill=255)
        y += round(font.size * spacing)
    box = block.getbbox()
    return block.crop(box) if box else None


def _stats_block(lines: list[str], path: str, size: int, spacing: float = 1.0) -> Image.Image | None:
    """LABEL|figure|unit lines as a column: a small label over each large figure, the unit beside it on the same
    baseline, and a hatched bar down the left that ties the column together."""
    figure, label, unit = _font(path, size), _font(path, max(4, round(size * 0.34))), _font(path, max(4, round(size * 0.5)))
    bar = max(4, round(size * 0.12))
    left = bar * 3
    block = Image.new("L", (W * 2, round(size * 2.2 * max(1, len(lines))) + 40), 0)
    draw = ImageDraw.Draw(block)
    stroke = max(1, size // 40)
    y = 0
    for line in lines:
        name, shown, after = (line.split("|") + ["", ""])[:3]
        if name:
            draw.text((left, y), name.upper(), fill=255, font=label, stroke_width=max(1, stroke // 2), stroke_fill=255)
            y += round(label.size * 1.2)
        baseline = y + figure.getmetrics()[0]
        draw.text((left, baseline), shown, fill=255, font=figure, anchor="ls", stroke_width=stroke, stroke_fill=255)
        if after:
            x = left + draw.textlength(shown, font=figure) + size * 0.14
            draw.text((x, baseline), after, fill=255, font=unit, anchor="ls", stroke_width=stroke, stroke_fill=255)
        y = baseline + round(size * (spacing - 0.62))
    if y <= 0:
        return None
    for k in range(-bar * 2, y + bar * 2, bar * 2):  # the hatch: short diagonals, one bar wide
        draw.line([(0, k + bar * 1.6), (bar, k)], fill=255, width=max(2, bar // 2))
    draw.rectangle([bar + 1, -10, left - 1, y + 10], fill=0)  # nothing of the hatch runs into the figures
    draw.rectangle([0, y, bar, y + bar * 3], fill=0)
    box = block.getbbox()
    return block.crop(box) if box else None


def _fitted_size(path: str, lines: list[str], size: int, width: int) -> int | None:
    """`size`, or smaller if type would not fit `width`."""
    while size > 14:
        block = _block(lines, _font(path, size))
        if block is None or block.width <= width:
            return size
        size = round(size * 0.92)
    return None


def _trouble(picture: np.ndarray) -> np.ndarray:
    """How badly light type or a light line would read at each pixel: bright, or full of detail."""
    y = luma(picture)
    detail = np.abs(np.diff(y, axis=0, prepend=y[:1])) + np.abs(np.diff(y, axis=1, prepend=y[:, :1]))
    return _blurred(detail * 6, 16) + y * 0.8


def _spot(
    w: int,
    h: int,
    rng: random.Random,
    trouble: np.ndarray | None = None,
    blocked: np.ndarray | None = None,
    x_from: int = 42,
) -> tuple[int, int, bool]:
    """The best place in the safe area for a box w x h: (x, y, clear of `blocked`). Among places about as good, a random one."""
    x_to, y_from, y_to = max(x_from, W - SAFE_RIGHT - w), SAFE_TOP + 20, max(SAFE_TOP + 20, H - SAFE_BOTTOM - 20 - h)
    xs, ys = np.meshgrid(np.arange(x_from, x_to + 1, 16), np.arange(y_from, y_to + 1, 16))

    def inside(a: np.ndarray) -> np.ndarray:
        total = np.zeros((H + 1, W + 1), dtype=np.float64)
        total[1:, 1:] = a.cumsum(0).cumsum(1)
        return (
            total[np.minimum(ys + h, H), np.minimum(xs + w, W)]
            - total[ys, np.minimum(xs + w, W)]
            - total[np.minimum(ys + h, H), xs]
            + total[ys, xs]
        )

    score = inside(trouble) if trouble is not None else np.zeros(xs.shape)
    clashes = inside(blocked) if blocked is not None else np.zeros(xs.shape)
    clear = clashes == 0
    score = np.where(clear, score, np.inf) if clear.any() else score + clashes * 1000
    near = np.argwhere(score <= score.min() * 1.08 + 1e-9)
    row, col = near[rng.randrange(len(near))]
    return int(xs[row, col]), int(ys[row, col]), bool(clear.any())


def _draw_type(
    lines: list[str], rng: random.Random, picture: np.ndarray | None = None, avoid: np.ndarray | None = None
) -> list[dict]:
    """Type blocks for these lines, where the picture is calmest and off `avoid`, clear of Instagram's UI.
    Either one small block, sometimes along a side, or one large figure with the rest under it."""
    if not lines:
        return []
    lines = [part for line in lines for part in textwrap.wrap(line, 38)]  # long lines wrap instead of vanishing
    path = rng.choice(available_fonts())
    wide = W - SAFE_RIGHT - 84
    small = rng.randint(46, 58)
    blocks: list[dict] = []
    make = lambda shown, size, rotate=0: {
        "lines": shown,
        "font": path,
        "size": size,
        "x": 0,
        "y": 0,
        "rotate": rotate,
        "colour": list(TYPE_COLOUR),
        "opacity": 1.0,
        "edge": 0.7,
        "pool": 0.5,
        "spacing": 1.3,
    }  # noqa: E731
    if rng.random() < 0.5:
        size = _fitted_size(path, lines, small, wide)
        if size:
            rotate = 90 if rng.random() < 0.25 else 0
            blocks = [make(lines, size, rotate)]
            if rotate and _type_image(blocks[0]).height > H - SAFE_TOP - SAFE_BOTTOM - 40:
                blocks[0]["rotate"] = 0
    else:
        lead = _fitted_size(path, lines[:1], rng.randint(96, 136), wide)
        rest = _fitted_size(path, lines[1:], small, wide) if lines[1:] else None
        blocks = [make(lines[:1], lead)] if lead else []
        if lead and rest:
            blocks.append(make(lines[1:], rest))
    if blocks:
        _stack_blocks(blocks, rng, picture, avoid)
    return blocks


def _stack_blocks(blocks: list[dict], rng: random.Random, picture: np.ndarray | None, avoid: np.ndarray | None) -> None:
    """Puts type blocks one under the other, where the picture is calmest and clear of `avoid`."""
    images = [_type_image(b) for b in blocks]
    width, height = max(i.width for i in images), sum(i.height for i in images) + 20 * (len(images) - 1)
    x, y, _ = _spot(width, height, rng, _trouble(picture) if picture is not None else None, avoid)
    for block, image in zip(blocks, images, strict=True):
        block["x"], block["y"] = x, y
        y += image.height + 20


def _type_image(block: dict, scale: float = 1.0) -> Image.Image:
    return _type_image_of(
        json.dumps(
            [block["lines"], block["font"], block["size"], block.get("rotate", 0), block.get("spacing", 1.3),
             block.get("style", "lines")]
        ),
        scale,
    )


@lru_cache(maxsize=32)
def _type_image_of(key: str, scale: float) -> Image.Image:
    lines, path, size, rotate, spacing, style = json.loads(key)
    shown = max(4, round(size * scale))
    drawn = _stats_block(lines, path, shown, spacing) if style == "stats" else _block(lines, _font(path, shown), spacing)
    image = drawn or Image.new("L", (1, 1), 0)
    return image.rotate(rotate, expand=True) if rotate else image


def _type_alpha(block: dict, scale: float = 1.0) -> np.ndarray:
    w, h = _size(scale)
    alpha = Image.new("L", (w, h), 0)
    alpha.paste(_type_image(block, scale), (round(block["x"] * w / W), round(block["y"] * h / H)))
    return np.asarray(alpha, dtype=np.float32) / 255 * block.get("opacity", 1.0)


def _route_blocked(type_alpha: np.ndarray | None, avoid: np.ndarray | None) -> np.ndarray:
    """Where a route must not go: readable type with some room around it, and `avoid`."""
    blocked = np.zeros((H, W), dtype=np.float32)
    if type_alpha is not None:
        words = Image.fromarray((type_alpha > READABLE_TYPE).astype(np.uint8) * 255)
        box = words.getbbox()
        if box:  # the filter is slow over a whole frame, and type is small; 13 clears its reach of 12
            x0, y0, x1, y1 = max(0, box[0] - 13), max(0, box[1] - 13), min(W, box[2] + 13), min(H, box[3] + 13)
            near = words.crop((x0, y0, x1, y1)).filter(ImageFilter.MaxFilter(25))
            blocked[y0:y1, x0:x1] = np.asarray(near, dtype=np.float32) / 255
    if avoid is not None:
        blocked = np.maximum(blocked, avoid)
    return blocked


def _route_shape(route) -> tuple[list[list[float]], float, float]:
    """A route's shape scaled to 0..1 both ways, and how wide and tall it is (in degrees of latitude): no coordinates."""
    mid = math.cos(math.radians(sum(p[0] for p in route) / len(route)))
    xs = [p[1] * mid for p in route]
    ys = [-p[0] for p in route]
    span_x, span_y = (max(xs) - min(xs)) or 1e-9, (max(ys) - min(ys)) or 1e-9
    points = [[round((x - min(xs)) / span_x, 4), round((y - min(ys)) / span_y, 4)] for x, y in zip(xs, ys, strict=True)]
    return points[:: max(1, len(points) // 900)], span_x, span_y


def _draw_route(
    route,
    rng: random.Random,
    type_alpha: np.ndarray | None = None,
    picture: np.ndarray | None = None,
    avoid: np.ndarray | None = None,
) -> dict | None:
    """A place and size for the route, where the picture is calmest, clear of type and of `avoid`; smaller if it does not fit.
    The result holds its shape only, scaled to 0..1, so that no coordinates leave with a recipe."""
    if len(route) < 2:
        return None
    points, span_x, span_y = _route_shape(route)
    size = rng.randint(560, 680) if rng.random() < 0.4 else rng.randint(380, 500)
    blocked = _route_blocked(type_alpha, avoid)
    trouble = _trouble(picture) if picture is not None else None
    x0, y0, used_w, used_h = 42, SAFE_TOP + 30, 1, 1
    for _ in range(6):
        scale = min(size / max(span_x, span_y), (W - SAFE_RIGHT - 70) / span_x, (H - SAFE_TOP - SAFE_BOTTOM - 70) / span_y)
        used_w, used_h = round(span_x * scale), round(span_y * scale)
        x0, y0, clear = _spot(used_w + 16, used_h + 16, rng, trouble, blocked, 30)
        if clear:
            break
        size = round(size * 0.85)
    return {
        "points": points,
        "x": x0 + 8,
        "y": y0 + 8,
        "w": used_w,
        "h": used_h,
        "width": rng.randint(8, 10),
        "colour": list(ROUTE_COLOUR),
        "opacity": 1.0,
        "edge": 0.75,
        "pool": 0.3,
    }


def _route_alpha(block: dict, scale: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """The line: (alpha, how far along each pixel is, 0..1)."""
    w, h = _size(scale)
    points = [((block["x"] + u * block["w"]) * scale, (block["y"] + v * block["h"]) * scale) for u, v in block["points"]]
    thick = max(1.0, block["width"] * scale)
    line = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(line).line([(x * 2, y * 2) for x, y in points], fill=255, width=max(1, round(thick * 2)), joint="curve")
    alpha = np.asarray(line.resize((w, h), Image.Resampling.LANCZOS), dtype=np.float32) / 255 * block.get("opacity", 1.0)
    progress = Image.new("F", (w, h), 0.0)
    draw = ImageDraw.Draw(progress)
    last = max(1, len(points) - 1)
    for i, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        draw.line([a, b], fill=i / last, width=max(1, round(thick + 3)))
    return alpha, np.asarray(progress, dtype=np.float32)


def _darken_under(
    final: np.ndarray,
    alpha: np.ndarray,
    tight: float,
    tight_strength: float,
    wide: float,
    wide_strength: float,
    scale: float = 1.0,
) -> np.ndarray:
    """A dark edge and a soft dark pool under a line or type, so that it reads on any picture."""
    near, far = _blurred(alpha, tight * scale), _blurred(alpha, wide * scale)
    amount = (1 - np.clip(near * 4, 0, 1) * tight_strength) * (1 - np.clip(far * 3, 0, 1) * wide_strength)
    return final * amount[..., None]


def _owners(pieces: list[Piece]) -> np.ndarray:
    """Which piece is on top at each pixel; -1 where the background shows."""
    owner = np.full((H, W), -1, dtype=np.int16)
    for i, piece in enumerate(pieces):
        owner[piece.mask * piece.opacity > 0.5] = i
    return owner


def pieces_of(recipe: dict) -> list[Piece]:
    """A recipe's pieces, as far as who covers whom goes; a hidden piece covers nothing."""
    return [
        Piece(p["shape"], opacity=p.get("opacity", 1.0) if p.get("visible", True) else 0.0) for p in recipe["pieces"]
    ]


def _largest_photo_share(pieces: list[Piece], owner: np.ndarray) -> float:
    """How much of the frame the most visible main photo takes, however many pieces it is cut into."""
    shares: dict[int, float] = {}
    for i, piece in enumerate(pieces):
        if not piece.peek:
            shares[piece.photo] = shares.get(piece.photo, 0.0) + float((owner == i).mean())
    return max(shares.values(), default=0.0)


def _with_peek(pieces: list[Piece], rng: random.Random, level: str, layout: str, tries: int = 24) -> list[Piece]:
    """Slip a peek piece in among the others, so that it reads as part of the collage rather than on top of it. What gets
    measured is the result: how much of the frame it visibly takes, and how much of it the other pieces cover."""
    kept = next((i for i, piece in enumerate(pieces) if piece.body), None)
    if kept is not None:  # the composition keeps a place for it
        return [replace(piece, peek=True) if i == kept else piece for i, piece in enumerate(pieces)]
    low, high = PEEK_SHARE[level]
    best, best_miss = None, math.inf
    for _ in range(tries):
        attempt = list(pieces)
        at = rng.randint(len(pieces) // 2, max(len(pieces) // 2, len(pieces) - 1))
        piece = _peek_piece(rng, level)
        whole = max(1, int((piece.mask > 0.5).sum()))
        if layout == "blinds":  # seen through the slats: every other louvre passes in front of it
            at = len(pieces)
            for louvre in pieces[1::2]:
                piece = replace(piece, shape={"type": "minus", "base": piece.shape, "cuts": [louvre.shape]})
        attempt.insert(at, piece)
        owner = _owners(attempt)
        visible = owner == at
        share = float(visible.mean())
        covered = 1 - float(visible.sum()) / whole
        if share > high or share >= _largest_photo_share(attempt, owner):
            continue
        miss = max(0.0, low - share) * 10 + max(0.0, PEEK_MIN_COVERED - covered) + max(0.0, covered - PEEK_MAX_COVERED)
        if miss < best_miss:
            best, best_miss = attempt, miss
        if miss == 0:
            break
    return best if best is not None else pieces


@dataclass
class Composed:
    final: np.ndarray  # graded, without grain
    base: np.ndarray | None  # graded picture under the pieces, what shows before they appear
    start: np.ndarray | None  # H x W, when each pixel appears, 0..1 of the reveal
    peek_share: float
    grain: float
    under: np.ndarray | None = None  # graded opening picture with the peek on it, before anything covers it
    under_mask: np.ndarray | None = None
    under_start: np.ndarray | None = None
    owner: np.ndarray | None = None  # which piece shows at each pixel


# --- recipe: what a version is, as data --------------------------------------------------------------------------
#
# draw_recipe decides everything once, from a seed: shapes, crops, grade, type, route, reel order. paint turns a recipe into a
# picture at any scale, and does nothing random. The editor changes recipes and calls paint, so what it shows is what comes out.


class PhotoStore:
    """Photos as they go into a picture, at the sizes asked for. Remembers recent ones: a preview is painted many times."""

    def __init__(self, images: dict[str, Image.Image] | None = None, keep: int = 40) -> None:
        self.images = dict(images or {})
        self.keep = keep
        self.lock = threading.RLock()
        self._done: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._details: dict[str, float] = {}

    def original(self, key: str) -> Image.Image:
        with self.lock:
            if key not in self.images:
                self.images[key] = load_photo(Path(key))
            return self.images[key]

    def aspect(self, key: str) -> float:
        img = self.original(key)
        return img.width / img.height

    def detail(self, key: str) -> float:
        with self.lock:
            if key not in self._details:
                self._details[key] = detail_of(self.original(key))
            return self._details[key]

    def _reduction(self, key: str, height: float) -> int:
        """How many times to halve the original so that one more would go below `height`: a sharp source, and no more work."""
        tall = self.original(key).height
        factor = 1
        while tall / (factor * 2) >= height:
            factor *= 2
        return factor

    def edited(self, key: str, height: float, recipe: dict) -> Image.Image:
        """`key` near `height` pixels tall, with what `recipe` does to photos before they are cut."""
        ops = _photo_ops(recipe, key)
        factor = self._reduction(key, height)
        memo = (key, factor, json.dumps(ops, sort_keys=True))
        with self.lock:
            if memo in self._done:
                self._done.move_to_end(memo)
                return self._done[memo]
            img = self.original(key)
            if factor > 1:
                img = img.reduce(factor)
            if ops["muscle"]:
                img = define_muscle(img, ops["muscle"])
            if ops["enhance"]:
                img = enhance(img)
            if ops["match"]:
                img = harmonise(img, [self.edited(p, 96, recipe) for p in recipe["photos"]], ops["match"])
            if ops["unify"]:
                unified = ops["unify"]
                img = unify(img, (unified["warm"], unified["dusk"]), unified["middle"])
            self._done[memo] = img
            while len(self._done) > self.keep:
                self._done.popitem(last=False)
            return img


def _photo_ops(recipe: dict, key: str) -> dict:
    body = key == recipe.get("peek_photo")
    return {
        "muscle": recipe.get("muscle", MUSCLE_DEFINITION) if key in recipe.get("define", []) else 0.0,
        "enhance": bool(recipe.get("enhance")),
        "match": recipe.get("match", PEEK_MATCH) if body and recipe.get("enhance") and recipe["photos"] else 0.0,
        "unify": recipe.get("unify"),
    }


def _cover_map(
    aspect: float,
    box: tuple[float, float, float, float],
    focus: tuple[float, float],
    rng: random.Random,
    zoom: float = 1.0,
    wander: float = 0.08,
) -> dict:
    """How a photo lies under a box of the frame, filling it, around its focus. In a map, c is a point of the photo (its height
    is 1) lying at frame point f, and k is how much photo one frame pixel covers."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    k = min(aspect / w, 1 / h) / zoom
    half_w, half_h = w * k / 2, h * k / 2
    cx = min(max(half_w, (focus[0] + rng.uniform(-wander, wander)) * aspect), aspect - half_w)
    cy = min(max(half_h, focus[1] + rng.uniform(-wander, wander)), 1 - half_h)
    return {"cx": cx, "cy": cy, "k": k, "fx": (x0 + x1) / 2, "fy": (y0 + y1) / 2, "rot": 0.0, "flip": False}


def _bounds(mask: np.ndarray) -> tuple[float, float, float, float] | None:
    ys, xs = np.nonzero(mask > 0.02)
    if len(xs) == 0:
        return None
    return float(xs.min()), float(ys.min()), float(xs.max() + 1), float(ys.max() + 1)


def _inner_box(visible: np.ndarray) -> tuple[float, float, float, float]:
    """The rectangle a cut shape really shows: rounded tops and slanted sides leave its corners out."""
    rows = visible.sum(1)
    kept = np.nonzero(rows >= rows.max() * 0.75)[0]
    columns = visible[kept[0] : kept[-1] + 1].mean(0)
    wide = np.nonzero(columns >= max(columns.max() * 0.75, 1e-6))[0]
    return float(wide[0]), float(kept[0]), float(wide[-1] + 1), float(kept[-1] + 1)


def _is_narrow(visible: np.ndarray) -> bool:
    """Too narrow to want both legs anyway; a wider cell framing one leg would show only part of it."""
    x0, y0, x1, y1 = _inner_box(visible)
    return (x1 - x0) / (y1 - y0) < 0.48


def frame_cell(
    aspect: float,
    bounds: tuple[float, float, float, float],
    inner: tuple[float, float, float, float],
    legs: list[tuple[float, float]],
    shoes: float,
    right: bool,
    one_leg: bool,
    fallback: float,
) -> dict:
    """A map for the body photo: shorts to shoes inside what the cell shows, with both legs whenever the cell allows it. A cell a
    little narrow for both takes more of the photo upwards; only when that is not enough, or `one_leg` asks, is it one leg."""
    x0, y0, x1, y1 = bounds
    ix0, iy0, ix1, iy1 = inner
    top, bottom = PEEK_SPAN[0], min(PEEK_SPAN[1], shoes)
    most = min(aspect / (x1 - x0), 1 / (y1 - y0))
    k = min(most, (bottom - top) / (iy1 - iy0))
    both = len(legs) == 2 and not one_leg
    if both:
        wanted = (legs[1][1] - legs[0][0] + 0.04) * aspect / (ix1 - ix0)
        if wanted > most:
            both = False
        else:
            k = max(k, wanted)
    if both:
        centre = (legs[0][0] + legs[1][1]) / 2
    elif len(legs) == 2:  # closer in, so that the other leg really is out of the picture
        leg = legs[1] if right else legs[0]
        close = (leg[1] - leg[0] + 0.1) * aspect / (ix1 - ix0)
        close = max(close, k * 0.6)  # closer than this would leave too little of the leg
        if close < k:  # shows less than shorts to shoes: keep the knee in the middle rather than only the foot
            bottom = (top + bottom) / 2 + (iy1 - iy0) * close / 2
            k = close
        between = (legs[0][1] + legs[1][0]) / 2
        wide = (ix1 - ix0) * k / aspect
        centre = between + wide / 2 if right else between - wide / 2
        if wide < leg[1] - leg[0] + 0.02:
            centre = (leg[0] + leg[1]) / 2
    else:
        centre = (legs[0][0] + legs[0][1]) / 2 if legs else fallback
    half_w, half_h = (x1 - x0) * k / 2, (y1 - y0) * k / 2
    fx, fy = (x0 + x1) / 2, (y0 + y1) / 2
    cx = min(max(half_w, centre * aspect - ((ix0 + ix1) / 2 - fx) * k), aspect - half_w)
    cy = min(max(half_h, bottom - (iy1 - fy) * k), 1 - half_h)
    return {"cx": cx, "cy": cy, "k": k, "fx": fx, "fy": fy, "rot": 0.0, "flip": False}


def draw_recipe(version: Version, store: PhotoStore, peek_level: str = "bold", route=()) -> dict:
    """Everything a version will be, decided from its seed."""
    rng = random.Random(version.seed)
    keys = list(version.photos)
    focus = {key: focus_of(store.original(key)) for key in keys}
    aspect = {key: store.aspect(key) for key in keys}
    pieces = _with_underlay(LAYOUTS[version.layout](rng, max(3, min(MAX_PIECES, len(keys) + rng.randint(0, 2)))), rng)
    if version.peek_photo is not None:
        pieces = _with_peek(pieces, rng, peek_level, version.layout)
    order = list(range(len(keys)))
    rng.shuffle(order)
    wants_calm = next((piece for piece in pieces if piece.calm and not piece.peek), None)
    if wants_calm is not None:
        calmest = min(range(len(keys)), key=lambda i: store.detail(keys[i]))
        at, other = wants_calm.photo % len(order), order.index(calmest)
        order[at], order[other] = order[other], order[at]
    few = len(keys) <= FEW_PHOTOS
    frames: dict[int, dict] = {}
    owner = _owners(pieces)
    entries = []
    for i, piece in enumerate(pieces):
        adjust = {"exposure": 1.0, "contrast": 1.0, "saturation": 1.0, "warmth": 0.0}
        bounds = _bounds(piece.mask) or (0.0, 0.0, 1.0, 1.0)
        if piece.peek:
            key = version.peek_photo
            adjust["exposure"] = PEEK_QUIET if version.look in (UNIFIED, NATURAL) else PEEK_DIMMED
            aside = _beside(focus_of(store.original(key)), rng, version.peek_side)
            visible = owner == i
            if piece.body and visible.any():
                raw = store.original(key)
                mapped = frame_cell(
                    store.aspect(key),
                    bounds,
                    _inner_box(visible),
                    leg_columns(raw),
                    shoe_line(raw),
                    aside[0] >= 0.5,
                    piece.one_leg and _is_narrow(visible),
                    aside[0],
                )
            else:
                mapped = _cover_map(store.aspect(key), bounds, aside, rng, rng.uniform(1.0, 1.1), 0.02)
        else:
            j = order[piece.photo % len(order)]
            key = keys[j]
            if (piece.full or few) and not piece.own_crop:  # one picture, out of register from piece to piece
                if j not in frames:
                    frames[j] = _cover_map(
                        aspect[key],
                        (-SLIP_MARGIN, -SLIP_MARGIN, W + SLIP_MARGIN, H + SLIP_MARGIN),
                        focus[key],
                        rng,
                        rng.uniform(1.0, 1.25),
                    )
                slip = piece.shift if piece.shift != (0, 0) else (rng.randint(-45, 45), rng.randint(-70, 70)) if few else (0, 0)
                mapped = dict(
                    frames[j],
                    fx=frames[j]["fx"] + max(-SLIP_MARGIN, min(SLIP_MARGIN, slip[0])),
                    fy=frames[j]["fy"] + max(-SLIP_MARGIN, min(SLIP_MARGIN, slip[1])),
                )
            else:
                mapped = _cover_map(aspect[key], bounds, focus[key], rng, rng.uniform(1.0, 1.35))
            if i == 0 and len(keys) == 1:  # the only photo, mirrored: otherwise the pieces melt into what lies under them
                mapped = dict(mapped, flip=True)
        entries.append(
            {
                "name": f"piece {i + 1}",  # "legs" is told by the photo: see shows_legs
                "shape": piece.shape,
                "photo": key,
                "body": piece.peek,
                "map": {name: float(value) if name != "flip" else bool(value) for name, value in mapped.items()},
                "adjust": adjust,
                "blend": piece.blend,
                "opacity": float(piece.opacity),
                "shadow": float(piece.shadow),
                "visible": True,
            }
        )
    peek_index = next((i for i, entry in enumerate(entries) if entry["body"]), None)
    appear = list(range(len(pieces)))
    rng.shuffle(appear)
    appear.remove(0)  # already showing: it is the opening picture
    appear.insert(0, 0)
    if peek_index is not None:  # one or two pictures first, so that it never stands alone; the rest arrive over its edges
        rest = [i for i in appear[1:] if i != peek_index]
        before = min(rng.randint(1, 2), max(0, len(rest) - 1))
        appear = [0, *rest[:before], peek_index, *rest[before:]]
    unified = unified_for(keys, store, rng) if version.look == UNIFIED else None
    recipe = {
        "format": RECIPE_FORMAT,
        "id": version.id,
        "seed": version.seed,
        "layout": version.layout,
        "look": version.look,
        "photos": keys,
        "peek_photo": version.peek_photo,
        "define": list(version.define),
        "enhance": version.look == NATURAL,
        "match": PEEK_MATCH,
        "muscle": MUSCLE_DEFINITION,
        "unify": unified,
        "grade": _grade_params(version.look, rng),
        "pieces": entries,
        "text": [],
        "route": None,
        "reel": {
            "order": appear,
            "seconds": 7.0,
            "reveal": 3.0,
            "push": 0.055,
            "seed": rng.getrandbits(32),
            "grain_seed": version.seed,
        },
    }
    if version.text or ("route" in version.data and route):
        picture = _picture_below(recipe, store)
        avoid = (owner == peek_index).astype(np.float32) if peek_index is not None else None
        recipe["text"] = _draw_type(version.text, rng, picture, avoid)
        words = np.max([_type_alpha(block) for block in recipe["text"]], axis=0) if recipe["text"] else None
        if "route" in version.data:
            recipe["route"] = _draw_route(route, rng, words, picture, avoid)
    return recipe


def unified_for(keys: list[str], store: PhotoStore, rng: random.Random) -> dict:
    warm, dusk = palette_of([store.original(key) for key in keys])
    return {"warm": warm, "dusk": dusk, "middle": rng.uniform(0.1, 0.15)}


def _picture_below(recipe: dict, store: PhotoStore) -> np.ndarray:
    """The picture without type and route, to find a calm place on, frame-sized."""
    below = paint(recipe, store, 0.5, overlays=False)
    return np.asarray(_to_image(below.final).resize((W, H), Image.Resampling.BILINEAR), dtype=np.float32) / 255


def shows_legs(piece: dict, recipe: dict) -> bool:
    """A piece holds the leg photo: it is told by the photo, so moving that photo to another piece moves this too."""
    return recipe.get("peek_photo") is not None and piece["photo"] == recipe["peek_photo"]


# --- the card: a title, a column of labelled stats, the route in the circle -------------------------------------

CARD_MARGIN = 56
CARD_ROUTE_FILL = 0.79  # of the circle's diameter, along the route's diagonal
CARD_STATS_SIZE = 84
CARD_STATS_OVERLAP = 30  # how far the column may reach up into the leg photo: over the shoes, a little
CARD_BOTTOM = 200  # the column ends at least this far from the bottom edge


def _circle_of(recipe: dict) -> tuple[float, float, float] | None:
    """(cx, cy, r) of the largest visible round piece: a moon, or a disc you added in the editor."""
    found = []
    for piece in recipe["pieces"]:
        shape = piece["shape"]
        disc = shape["parts"][0] if shape["type"] == "union" and shape["parts"] else shape
        if disc.get("type") == "disc" and piece.get("visible", True):
            found.append((disc["cx"], disc["cy"], disc["r"]))
    return max(found, key=lambda c: c[2], default=None)


def _legs_bottom(recipe: dict) -> float | None:
    """Where the leg photo's visible part ends, down the frame: a card's column starts just above it."""
    pieces = pieces_of(recipe)
    legs = [i for i, p in enumerate(recipe["pieces"]) if shows_legs(p, recipe) and p.get("visible", True)]
    if not legs:
        return None
    rows = np.nonzero(np.isin(_owners(pieces), legs).any(1))[0]
    return float(rows[-1] + 1) if len(rows) else None


def card_route(recipe: dict, points: list[list[float]], aspect: float, store: PhotoStore | None = None) -> dict:
    """The route for a card: filling the circle when the collage has one, thick and white with a dark edge; otherwise
    where the picture is calmest."""
    block = {"points": points, "width": 13, "colour": [255, 255, 255], "opacity": 1.0, "edge": 0.95, "pool": 0.45}
    circle = _circle_of(recipe)
    if circle:
        cx, cy, r = circle
        h = 2 * r * CARD_ROUTE_FILL / math.hypot(aspect, 1)
        w = h * aspect
        return {**block, "x": cx - w / 2, "y": cy - h / 2, "w": w, "h": h}
    w, h = (560, 560 / aspect) if aspect >= 1 else (560 * aspect, 560)
    picture = _picture_below(recipe, store) if store is not None else None
    x, y, _ = _spot(round(w) + 16, round(h) + 16, random.Random(recipe.get("seed", 0)),
                    _trouble(picture) if picture is not None else None, None, 30)
    return {**block, "x": x + 8, "y": y + 8, "w": w, "h": h}


def apply_card(recipe: dict, lines: list[str], title: str = "", route: dict | None = None,
               store: PhotoStore | None = None) -> dict:
    """The recipe as a card: `title` at the top, `lines` (LABEL|figure|unit) as a column at the bottom left, and
    `route` ({points, aspect}) in the circle. Pieces, look and reel stay as they were."""
    font = card_font()
    common = {"font": font, "rotate": 0, "colour": list(TYPE_COLOUR), "opacity": 1.0, "edge": 0.8, "pool": 0.55}
    blocks = []
    if title:
        size = _fitted_size(font, [title], 88, W - SAFE_RIGHT - CARD_MARGIN * 2) or 40
        blocks.append({"lines": [title], "size": size, "spacing": 1.0, **common,
                       "x": CARD_MARGIN, "y": SAFE_TOP + 24})
    if lines:
        stats = {"lines": lines, "size": CARD_STATS_SIZE, "spacing": 1.0, "style": "stats", **common, "x": CARD_MARGIN, "y": 0}
        tall = _type_image(stats).height
        legs = _legs_bottom(recipe)
        lowest = H - CARD_BOTTOM - tall
        stats["y"] = min(legs - CARD_STATS_OVERLAP, lowest) if legs is not None else H - SAFE_BOTTOM - tall - 28
        blocks.append(stats)
    recipe["text"] = blocks
    if route and route.get("points"):
        recipe["route"] = card_route(recipe, route["points"], route["aspect"], store)
    return recipe


def card_lines_from(stats: dict[str, str]) -> list[str]:
    """Card lines from a run's stats (`versions.json`), for runs made before cards: their time is moving time."""
    lines = []
    for name, label in (("distance", "Distance"), ("time", "Time"), ("pace", "Avg pace")):
        if stats.get(name):
            figure, _, unit = stats[name].partition(" ")
            lines.append(f"{label}|{figure}|{unit}")
    return lines


def route_of(recipe: dict | None) -> dict | None:
    """A recipe's route as {points, aspect}, to put on another; None if it has none."""
    block = (recipe or {}).get("route")
    return {"points": block["points"], "aspect": block["w"] / max(block["h"], 1e-6)} if block else None


def route_from(coordinates) -> dict | None:
    """A recorded route as {points, aspect}: its shape only."""
    if len(coordinates) < 2:
        return None
    points, span_x, span_y = _route_shape(coordinates)
    return {"points": points, "aspect": span_x / span_y}


def _piece_picture(piece: dict, recipe: dict, store: PhotoStore, scale: float) -> tuple[np.ndarray, np.ndarray | None]:
    """The photo under a piece, frame-sized: (colour, where the photo reaches, if not everywhere)."""
    w, h = _size(scale)
    m = piece["map"]
    key = piece["photo"]
    aspect = store.aspect(key)
    k = m["k"]
    img = store.edited(key, scale / max(k, 1e-6), recipe)
    size = img.height
    # From an output pixel to the photo: c + k R (frame - f), in pixels of this size of the photo
    c, s = math.cos(math.radians(m["rot"])), math.sin(math.radians(m["rot"]))
    per = W / w
    a, b = size * k * c * per, -size * k * s * per
    d, e = size * k * s * per, size * k * c * per
    tx = size * (m["cx"] - k * c * m["fx"] + k * s * m["fy"])
    ty = size * (m["cy"] - k * s * m["fx"] - k * c * m["fy"])
    if m.get("flip"):
        a, b, tx = -a, -b, size * aspect - tx
    picture = img.transform((w, h), Image.Transform.AFFINE, (a, b, tx, d, e, ty), Image.Resampling.BILINEAR)
    corners = [(a * x + b * y + tx, d * x + e * y + ty) for x in (0, w) for y in (0, h)]
    covered = all(-0.5 <= x <= img.width + 0.5 and -0.5 <= y <= img.height + 0.5 for x, y in corners)
    inside = None
    if not covered:
        solid = Image.new("L", img.size, 255).transform(
            (w, h), Image.Transform.AFFINE, (a, b, tx, d, e, ty), Image.Resampling.BILINEAR
        )
        inside = np.asarray(solid, dtype=np.float32) / 255
    out = np.asarray(picture, dtype=np.float32) / 255
    return _adjusted(out, piece.get("adjust") or {}), inside


def _adjusted(a: np.ndarray, adjust: dict) -> np.ndarray:
    """One piece's own exposure, contrast, saturation, warmth, tint and fade."""
    exposure, contrast = adjust.get("exposure", 1.0), adjust.get("contrast", 1.0)
    saturation, warmth = adjust.get("saturation", 1.0), adjust.get("warmth", 0.0)
    fade, tint = adjust.get("fade", 0.0), adjust.get("tint", 0.0)
    if exposure != 1.0:
        a = a * exposure
    if contrast != 1.0:
        a = (a - 0.36) * contrast + 0.36
    if saturation != 1.0:
        y = luma(a)[..., None]
        a = y + (a - y) * saturation
    if warmth:
        a = a + np.array((warmth, 0.0, -warmth), dtype=np.float32) * 0.2
    if tint:  # towards magenta, or below zero towards green
        a = a + np.array((tint, -tint, tint), dtype=np.float32) * 0.12
    a = np.clip(a, 0, 1)
    return a * (1 - fade) + fade if fade else a  # fade: blacks lifted to a matte grey


def _paste(canvas: np.ndarray, mask: np.ndarray, source: np.ndarray, piece: dict, scale: float) -> np.ndarray:
    """One piece onto the canvas, with its shadow."""
    if piece.get("shadow"):
        shade = np.roll(_blurred(mask, 20 * scale), (round(16 * scale), round(10 * scale)), axis=(0, 1))
        canvas = canvas * (1 - shade[..., None] * piece["shadow"])
    amount = (mask * piece.get("opacity", 1.0))[..., None]
    if piece.get("blend") == "screen":
        return canvas + (1 - canvas) * source * amount
    return canvas * (1 - amount) + source * amount


def paint(recipe: dict, store: PhotoStore, scale: float = 1.0, overlays: bool = True, reel: bool = False) -> Composed:
    """A recipe as a picture, at `scale` of the frame. Nothing random happens here: the same recipe gives the same picture."""
    w, h = _size(scale)
    canvas = np.full((h, w, 3), 0.04, dtype=np.float32)
    base = canvas
    owner = np.full((h, w), -1, dtype=np.int16)
    peek_index = None
    peek_source = peek_mask = None
    for i, piece in enumerate(recipe["pieces"]):
        if not piece.get("visible", True):
            continue
        source, inside = _piece_picture(piece, recipe, store, scale)
        mask = rasterise(piece["shape"], scale)
        if inside is not None:
            mask = mask * inside
        canvas = _paste(canvas, mask, source, piece, scale)
        owner[mask * piece.get("opacity", 1.0) > 0.5] = i
        if i == 0:
            base = canvas  # the reel opens on the picture under everything; the other pieces bleed in over it
        if shows_legs(piece, recipe):
            peek_index, peek_source, peek_mask = i, source, mask
    share = float((owner == peek_index).mean()) if peek_index is not None else 0.0
    params = recipe["grade"]
    final = apply_grade(canvas, params, scale)
    start = under = under_mask = under_start = graded_base = None
    if reel:
        pieces = recipe["pieces"]
        begins = np.zeros(len(pieces) + 1, dtype=np.float32)  # the last is for -1, no piece: the background, from the start
        order = recipe["reel"]["order"]
        for rank, i in enumerate(order):
            if i < len(pieces):
                begins[i] = rank / max(1, len(order)) * 0.78
        gen = random.Random(recipe["reel"]["seed"])
        bleed = rasterise({"type": "noise", "seed": gen.getrandbits(32), "cell": 90, "low": 0, "gain": 1}, scale) * 0.22
        start = begins[owner] + bleed
        graded_base = apply_grade(base, params, scale)
        if peek_index is not None:  # it shows whole until other pieces arrive over it
            under_mask = peek_mask
            pasted = base * (1 - under_mask[..., None]) + peek_source * under_mask[..., None]
            under = apply_grade(pasted, params, scale)
            under_start = begins[peek_index] + bleed
    if overlays:
        block = recipe.get("route")
        if block and block.get("visible", True):
            line, along = _route_alpha(block, scale)
            final = _darken_under(final, line, 3, block.get("edge", 0.75), 22, block.get("pool", 0.3), scale)
            final = (
                final * (1 - line[..., None]) + np.array(block["colour"], dtype=np.float32)[None, None] / 255 * line[..., None]
            )
            if start is not None:  # the line draws itself during the reveal
                start = np.where(line > 0.02, 0.5 + along * 0.42, start)
        for block in recipe.get("text") or []:
            if not block.get("visible", True):
                continue
            over = _type_alpha(block, scale)
            final = _darken_under(final, over, 4, block.get("edge", 0.7), 34, block.get("pool", 0.5), scale)
            colour = np.array(block["colour"], dtype=np.float32) / 255
            final = final * (1 - over[..., None]) + colour[None, None] * over[..., None]
    return Composed(final, graded_base, start, share, params.get("grain", 0.0), under, under_mask, under_start, owner)


def still_of(composed: Composed, recipe: dict) -> Image.Image:
    return _to_image(add_grain(composed.final, composed.grain, recipe["reel"].get("grain_seed", 0)))


def render_still(recipe: dict, store: PhotoStore, scale: float = 1.0) -> Image.Image:
    return still_of(paint(recipe, store, scale), recipe)


def make_card(folder: Path, version: str, title: str | None = None, data: ActivityData | None = None,
              reel: bool = True) -> list[Path]:
    """One version of a run as a card, saved as a new version beside it (`card-<version>`, in `edited/`, so the editor
    opens it) and exported as a still and, with ffmpeg, a reel. Returns what was written."""
    listed = json.loads((folder / "versions.json").read_text()) if (folder / "versions.json").exists() else {}
    sources = [folder / "edited" / f"{version}.json", folder / "recipes" / f"{version}.json"]
    found = next((p for p in sources if p.exists()), None)
    if found is None:
        raise SystemExit(f"no version {version} in {folder}")
    recipe = json.loads(found.read_text())
    saved = listed.get("card") or {}
    lines = data.card_lines() if data else saved.get("lines") or card_lines_from(listed.get("stats") or {})
    title = title if title is not None else (data.name if data else saved.get("title", ""))
    route = route_from(data.route) if data and data.route else route_of(recipe)
    if route is None:  # this version drew none: the run's others show the same route
        route = next((r for p in sorted((folder / "recipes").glob("*.json")) if (r := route_of(json.loads(p.read_text())))),
                     None)
    store = PhotoStore()
    recipe = apply_card(recipe, lines, title, route, store)
    recipe["id"] = f"card-{version}"
    (folder / "edited").mkdir(exist_ok=True)
    written = [folder / "edited" / f"{recipe['id']}.json"]
    written[0].write_text(json.dumps(recipe, indent=1, ensure_ascii=False))
    (folder / "exported").mkdir(exist_ok=True)
    composed = paint(recipe, store, 1.0, reel=reel)
    written.append(folder / "exported" / f"{recipe['id']}.jpg")
    still_of(composed, recipe).save(written[-1], quality=95)
    if reel and shutil.which("ffmpeg"):
        written.append(folder / "exported" / f"{recipe['id']}.mp4")
        _encode(composed, written[-1], recipe["reel"])
    return written


# --- output ------------------------------------------------------------------------------------------------------


def _to_image(a: np.ndarray) -> Image.Image:
    return Image.fromarray((np.clip(a, 0, 1) * 255 + 0.5).astype(np.uint8))


def reveal(composed: Composed, progress: float) -> np.ndarray:
    """The picture `progress` of the way through the reveal, 0..1."""
    below = composed.base
    if composed.under is not None:
        seen = (np.clip((progress - composed.under_start) / 0.06, 0, 1) * composed.under_mask)[..., None]
        below = below * (1 - seen) + composed.under * seen
    alpha = np.clip((progress - composed.start) / 0.06, 0, 1)[..., None]
    return below * (1 - alpha) + composed.final * alpha


def _encode(
    composed: Composed,
    path: Path,
    reel: dict,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Pieces bleed in, then a slow push-in with living grain. Silent: Instagram's music goes on top."""
    seed, seconds, reveal_for, push_in = reel.get("grain_seed", 0), reel["seconds"], reel["reveal"], reel["push"]
    grains = [add_grain(np.full((H, W, 3), 0.5, dtype=np.float32), composed.grain, seed + i) - 0.5 for i in range(6)]
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-map_metadata",
        "-1",
        str(path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    frames = round(seconds * FPS)
    flicker = random.Random(seed)
    try:
        for n in range(frames):
            t = n / FPS
            if t < reveal_for + 0.3:
                frame = reveal(composed, t / reveal_for)
            else:
                frame = composed.final
            frame = np.clip(frame * flicker.uniform(0.985, 1.015) + grains[n % len(grains)], 0, 1)
            img = _to_image(frame)
            push = 1 + push_in * (n / frames)
            w, h = W / push, H / push
            img = img.resize((W, H), Image.Resampling.BILINEAR, box=((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2))
            proc.stdin.write(img.tobytes())
            if progress:
                progress((n + 1) / frames)
    finally:
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        proc.wait()
    if proc.returncode:
        raise RuntimeError(f"ffmpeg failed: {err[-300:]}")


def draw_versions(
    n: int,
    base_seed: int,
    photos: list[str],
    peek: list[str],
    text: list[str],
    only_layouts: list[str] | None = None,
    only_looks: list[str] | None = None,
    data: ActivityData | None = None,
    fields: list[str] | None = None,
    define: list[str] | None = None,
    peek_side: str = "either",
) -> list[Version]:
    """`n` versions spread over the layouts and looks, so a run shows real range before anything repeats."""
    rng = random.Random(base_seed)
    layouts = only_layouts or list(CALM_LAYOUTS)
    # The photo's own colours come up most, unless you narrow it down; a group name stands for its looks
    looks = [*[NATURAL] * 10, *[UNIFIED] * 3, *COLOUR, *MUTED]
    if only_looks:
        looks = [one for look in only_looks for one in LOOK_GROUPS.get(look, (look,))]
    pairs = [(a, b) for a in layouts for b in looks]
    rng.shuffle(pairs)
    # Each layout comes up before any layout comes up twice
    seen: dict[str, int] = {}
    pairs.sort(key=lambda pair: seen.__setitem__(pair[0], seen.get(pair[0], 0) + 1) or seen[pair[0]])
    versions = []
    for i in range(n):
        layout, look = pairs[i % len(pairs)]
        shown = list(photos)
        rng.shuffle(shown)
        custom = text if text and rng.random() < 0.65 else []
        chosen = []
        if data:
            drawn = pick_fields(data, rng)  # always drawn, so `fields` does not change the rest of a seeded run
            chosen = [f for f in fields if f in data.available()] if fields else drawn
        versions.append(
            Version(
                f"v{i + 1:02d}",
                rng.getrandbits(32),
                layout,
                look,
                shown,
                rng.choice(peek) if peek else None,
                peek_side=peek_side,
                text=[line for f in chosen if f != "route" and (line := data.stat(f))] + custom if chosen else custom,
                data=chosen,
                define=list(define or []),
            )
        )
    return versions


def _render(args: tuple) -> Version:
    version, out, peek_level, reels, route = args
    store = PhotoStore()
    recipe = draw_recipe(version, store, peek_level, route)
    still = out / f"{version.id}-{version.layout}-{version.look}{'-peek' if version.peek_photo else ''}"
    composed = paint(recipe, store, 1.0, reel=reels)
    still_of(composed, recipe).save(still.with_suffix(".jpg"), quality=93)
    version.still = still.with_suffix(".jpg").name
    version.peek_share = round(composed.peek_share, 4)
    if reels:
        _encode(composed, still.with_suffix(".mp4"), recipe["reel"])
        version.reel = still.with_suffix(".mp4").name
    recipes = out / "recipes"
    recipes.mkdir(exist_ok=True)
    (recipes / f"{version.id}.json").write_text(json.dumps(recipe, indent=1, ensure_ascii=False))
    version.recipe = f"recipes/{version.id}.json"
    return version


def contact_sheet(versions: list[Version], out: Path, columns: int = 4) -> Path:
    tw, th, pad, label = 300, 533, 14, 34
    rows = math.ceil(len(versions) / columns)
    sheet = Image.new("RGB", (columns * (tw + pad) + pad, rows * (th + label + pad) + pad), (14, 14, 14))
    draw = ImageDraw.Draw(sheet)
    font = _font(FONTS[0] if FONTS else "", 20)
    for i, v in enumerate(versions):
        x, y = pad + (i % columns) * (tw + pad), pad + (i // columns) * (th + label + pad)
        sheet.paste(Image.open(out / v.still).resize((tw, th), Image.Resampling.LANCZOS), (x, y))
        draw.text(
            (x, y + th + 6),
            f"{v.id} {v.layout} {v.look}{' *' if v.peek_photo else ''}{' ~' if 'route' in v.data else ''}",
            fill=(205, 205, 200),
            font=font,
        )
    path = out / "contact-sheet.jpg"
    sheet.save(path, quality=90)
    return path


def make(
    photos: list[Path],
    out: Path,
    count: int = 12,
    peek: list[Path] | None = None,
    peek_level: str = "bold",
    text: list[str] | None = None,
    reels: bool = True,
    seed: int | None = None,
    layouts: list[str] | None = None,
    looks: list[str] | None = None,
    workers: int | None = None,
    only: list[str] | None = None,
    data: ActivityData | None = None,
    fields: list[str] | None = None,
    define: list[Path] | None = None,
    peek_side: str = "either",
) -> tuple[Path, list[Version]]:
    """Render `count` new versions into `out`; returns the contact sheet and what was made."""
    if reels and not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is needed for reels: install it, or use --no-reels")
    # A photo to define is a photo like the others, unless it is the peek
    photos = list(dict.fromkeys([*photos, *(p for p in define or [] if p not in (peek or []))]))
    if not photos:
        raise SystemExit("a collage needs at least 1 photo (the peek photo does not count)")
    if fields and (unknown := set(fields) - set(DATA_FIELDS)):
        raise SystemExit(f"unknown --data {sorted(unknown)}: choose from {', '.join(DATA_FIELDS)}")
    out.mkdir(parents=True, exist_ok=True)
    base_seed = seed if seed is not None else secrets.randbits(32)
    drawn = draw_versions(
        count,
        base_seed,
        [str(p) for p in photos],
        [str(p) for p in peek or []],
        text or [],
        layouts,
        looks,
        data,
        fields,
        [str(p) for p in define or []],
        peek_side,
    )
    if only:
        drawn = [v for v in drawn if v.id in only]
        if not drawn:
            raise SystemExit(f"none of {only} among the {count} versions of this run")
    jobs = [(v, out, peek_level, reels, data.route if data else ()) for v in drawn]
    # Runs in other processes: a version is CPU work, and a whole run is minutes of it
    if workers == 1:
        done = [_render(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers or 4) as pool:
            done = list(pool.map(_render, jobs))
    sheet = contact_sheet(done, out)
    (out / "versions.json").write_text(
        json.dumps(
            {
                "seed": base_seed,
                "created": datetime.now().isoformat(timespec="seconds"),
                "stats": {f: data.stat(f) for f in DATA_FIELDS if data.stat(f)} if data else {},
                "card": {"title": data.name, "lines": data.card_lines()} if data else {},
                "versions": [asdict(v) for v in done],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return sheet, done
