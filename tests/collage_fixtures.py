"""Photos and runs for the collage's tests: made here from nothing, so a picture differs only if the code does."""

from __future__ import annotations

import json
import random
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from movescrobble import collage

W, H = 900, 1200
ROUTE = tuple((52.0 + i * 0.0004 + 0.002 * np.sin(i / 9), 21.0 + i * 0.0003) for i in range(120))


def sky() -> Image.Image:
    """Calm: a dusk gradient over a dark treeline."""
    t = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    a = np.array((0.55, 0.42, 0.55), dtype=np.float32) * (1 - t) + np.array((0.95, 0.62, 0.5), dtype=np.float32) * t
    a = np.tile(a, (1, W, 1))
    line = (0.72 + 0.05 * np.sin(np.arange(W) / 40))[None, :] * H
    a[np.arange(H)[:, None] > line] = (0.07, 0.06, 0.07)
    return Image.fromarray((a * 255).astype(np.uint8))


def leaves(seed: int, base: tuple, blob: tuple) -> Image.Image:
    """Busy: blobs of colour over a sky, with dark branches."""
    gen = np.random.default_rng(seed)
    img = Image.new("RGB", (W, H), base)
    draw = ImageDraw.Draw(img)
    for _ in range(420):
        x, y, r = gen.integers(0, W), gen.integers(0, H), gen.integers(10, 34)
        shade = gen.uniform(0.6, 1.15)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=tuple(int(min(255, c * shade)) for c in blob))
    for _ in range(14):
        points = [(int(gen.integers(0, W)), int(gen.integers(0, H))) for _ in range(3)]
        draw.line(points, fill=(30, 22, 20), width=int(gen.integers(4, 16)))
    return img.filter(ImageFilter.GaussianBlur(0.8))


def road() -> Image.Image:
    """A path in perspective under trees."""
    img = leaves(5, (120, 110, 130), (170, 95, 40))
    draw = ImageDraw.Draw(img)
    draw.polygon([(380, 620), (520, 620), (820, H), (120, H)], fill=(120, 116, 112))
    draw.polygon([(0, 620), (380, 620), (120, H), (0, H)], fill=(95, 62, 38))
    return img


@lru_cache
def legs() -> Image.Image:
    """Shorts, two legs, socks and shoes against a pale wall, grey floor below."""
    a = np.zeros((H, W, 3), dtype=np.uint8)
    a[:] = (190, 186, 188)
    a[: int(H * 0.15)] = (60, 120, 170)
    a[int(H * 0.15) : int(H * 0.3), 220:700] = (32, 32, 36)
    for x0, x1 in ((270, 420), (520, 670)):
        a[int(H * 0.3) : int(H * 0.6), x0:x1] = (200, 150, 118)
        a[int(H * 0.6) : int(H * 0.76), x0 + 25 : x1 - 25] = (14, 14, 14)
        a[int(H * 0.76) : int(H * 0.82), x0 - 25 : x1 + 15] = (20, 20, 20)
    a[int(H * 0.86) :] = (126, 126, 124)
    return Image.fromarray(a).filter(ImageFilter.GaussianBlur(1.0))


@lru_cache
def photos() -> dict[str, Image.Image]:
    return {
        "leaves": leaves(1, (95, 130, 205), (140, 125, 40)),
        "lamp": leaves(2, (40, 50, 80), (170, 150, 50)),
        "road": road(),
        "sky": sky(),
    }


def grade(a: np.ndarray, look: str, rng: random.Random) -> np.ndarray:
    return collage.apply_grade(a, collage._grade_params(look, rng))


def text_layer(lines, rng, picture=None, avoid=None) -> tuple[Image.Image, Image.Image] | None:
    """All of `lines` as one picture: (light colour, its alpha)."""
    blocks = collage._draw_type(lines, rng, picture, avoid)
    if not blocks:
        return None
    alpha = np.max([collage._type_alpha(b) for b in blocks], axis=0)
    return Image.new("RGB", (collage.W, collage.H), collage.TYPE_COLOUR), Image.fromarray((alpha * 255).astype(np.uint8))


def route_layer(route, rng, type_alpha=None, picture=None, avoid=None) -> tuple[np.ndarray, np.ndarray] | None:
    block = collage._draw_route(route, rng, type_alpha, picture, avoid)
    return collage._route_alpha(block) if block else None


def compose(version, photos, peek, peek_level="bold", route=()) -> collage.Composed:
    """One version from photos already in hand, ready for a reel."""
    keys = version.photos if len(version.photos) == len(photos) else [f"#{i}" for i in range(len(photos))]
    images = dict(zip(keys, photos, strict=True))
    peek_key = version.peek_photo or ("#peek" if peek is not None else None)
    if peek is not None:
        images[peek_key] = peek
    store = collage.PhotoStore(images)
    recipe = collage.draw_recipe(replace(version, photos=list(keys), peek_photo=peek_key), store, peek_level, route)
    return collage.paint(recipe, store, 1.0, reel=True)


def run_folder(folder: Path, count: int = 3, seed: int = 11, layouts: tuple[str, ...] | None = None) -> Path:
    """A run on disk, as `movescrobble collage` leaves it: real files, real recipes."""
    src = folder / "src"
    src.mkdir(parents=True)
    paths = []
    for name, img in photos().items():
        paths.append(src / f"{name}.png")
        img.save(paths[-1])
    body = src / "legs.png"
    legs().save(body)
    data = collage.ActivityData(
        kind="running",
        date="06 Nov 2026",
        distance_m=8035.0,
        seconds=2823.0,
        speed_ms=2.85,
        hr=149.0,
        route=ROUTE,
    )
    out = folder / "run"
    collage.make(
        paths,
        out,
        count,
        [body],
        "bold",
        None,
        False,
        seed,
        list(layouts) if layouts else None,
        ["natural"],
        1,
        None,
        data,
        ["distance", "pace", "route"],
        [body],
        "right",
    )
    assert json.loads((out / "versions.json").read_text())["stats"]
    return out
