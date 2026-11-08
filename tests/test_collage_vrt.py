"""Visual regression: each composition, look and kind of edit, compared with a picture known to be right.

`pytest --update-vrt` rewrites the pictures, after you have looked at what changed: failures leave `actual` and `diff` beside
each other in tests/vrt/_failed. Fonts are Pillow's own here, so that a picture is the same on every machine.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import numpy as np
import pytest
from collage_fixtures import ROUTE, legs, photos
from PIL import Image

from movescrobble import collage

GOLDEN = Path(__file__).parent / "vrt"
SIZE = (180, 320)
MEAN_LIMIT, PATCH_LIMIT = 1.2, 14.0  # of 255: over the picture; over its worst 20 x 20 pixels


def differences(actual: Image.Image, golden: Image.Image) -> tuple[float, float, np.ndarray]:
    a, b = (np.asarray(i.convert("RGB"), dtype=np.float32) for i in (actual, golden))
    diff = np.abs(a - b).mean(axis=2)
    h, w = diff.shape
    total = np.zeros((h + 1, w + 1), dtype=np.float64)
    total[1:, 1:] = diff.cumsum(0).cumsum(1)
    n = 20
    patches = (total[n:, n:] - total[:-n, n:] - total[n:, :-n] + total[:-n, :-n]) / (n * n)
    return float(diff.mean()), float(patches.max()), diff


def same_picture(actual: Image.Image, golden: Image.Image) -> bool:
    mean, patch, _ = differences(actual, golden)
    return mean <= MEAN_LIMIT and patch <= PATCH_LIMIT


def check(name: str, actual: Image.Image, update: bool, folder: Path = GOLDEN, size: tuple[int, int] | None = SIZE) -> None:
    actual = actual.convert("RGB")
    if size:
        actual = actual.resize(size, Image.Resampling.BOX)
    path = folder / f"{name}.png"
    if update:
        folder.mkdir(parents=True, exist_ok=True)
        actual.save(path, optimize=True)
        return
    assert path.exists(), f"no picture for {name}: run pytest --update-vrt"
    golden = Image.open(path).convert("RGB")
    assert golden.size == actual.size, f"{name}: size changed from {golden.size} to {actual.size}"
    mean, patch, diff = differences(actual, golden)
    if mean > MEAN_LIMIT or patch > PATCH_LIMIT:
        failed = GOLDEN / "_failed"
        failed.mkdir(exist_ok=True)
        actual.save(failed / f"{name}-actual.png")
        Image.fromarray(np.clip(diff * 6, 0, 255).astype(np.uint8)).save(failed / f"{name}-diff.png")
        pytest.fail(
            f"{name} changed: mean {mean:.2f} (limit {MEAN_LIMIT}), worst patch {patch:.1f} (limit {PATCH_LIMIT}); see {failed}"
        )


@pytest.fixture(autouse=True)
def builtin_font(monkeypatch):
    monkeypatch.setattr(collage, "FONTS", [])
    monkeypatch.setattr(collage, "CARD_FONTS", [])


def store() -> collage.PhotoStore:
    return collage.PhotoStore({**photos(), "legs": legs()})


def drawn(layout="stack", look="natural", seed=3, text=("8.04 km", "5:51 /km"), route=True) -> dict:
    v = collage.Version(
        "v01",
        seed,
        layout,
        look,
        list(photos()),
        "legs",
        text=list(text),
        data=["route"] if route else [],
        define=["legs"],
        peek_side="right",
    )
    return collage.draw_recipe(v, store(), "bold", ROUTE)


def picture(recipe: dict, scale=0.25) -> Image.Image:
    return collage.render_still(json.loads(json.dumps(recipe)), store(), scale)


@pytest.mark.parametrize("layout", list(collage.LAYOUTS))
def test_every_composition(layout, update_vrt):
    check(f"layout-{layout}", picture(drawn(layout)), update_vrt)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("layout", collage.CALM_LAYOUTS)
def test_compositions_across_seeds(layout, seed, update_vrt):
    check(f"seed-{layout}-{seed}", picture(drawn(layout, seed=seed, text=(), route=False)), update_vrt)


@pytest.mark.parametrize("look", list(collage.LOOKS))
def test_every_look(look, update_vrt):
    check(f"look-{look}", picture(drawn("slants", look)), update_vrt)


def test_full_size_paints_as_the_preview_does(update_vrt):
    check("full-size", picture(drawn("bands"), 1.0), update_vrt)


def edit_crop(r):
    r["pieces"][2]["map"]["fx"] += 80
    r["pieces"][2]["map"]["k"] *= 0.8


def edit_seam(r):
    for piece in r["pieces"]:
        if piece["shape"]["type"] == "poly":
            piece["shape"]["points"] = [[x, y + (70 if 500 < y < 800 else 0)] for x, y in piece["shape"]["points"]]


def edit_adjust(r):
    r["pieces"][2]["adjust"].update(exposure=0.7, contrast=1.3, saturation=0.3, warmth=0.5)


def edit_legs(r):
    r["match"], r["muscle"] = 0.0, 1.2


def edit_text(r):
    r["text"][0].update(x=120, y=1100, size=90, colour=[255, 200, 60], rotate=0, pool=0.9)


def edit_route(r):
    r["route"].update(x=500, y=300, w=420, h=420, width=18, colour=[120, 220, 255], edge=1.0, pool=0.6)


def edit_order(r):
    at = next(i for i, p in enumerate(r["pieces"]) if p["body"])  # brings the legs' hidden edges over their neighbours
    r["pieces"].append(r["pieces"].pop(at))


def edit_hide(r):
    r["pieces"][-1]["visible"] = False


def edit_rotate_flip(r):
    r["pieces"][3]["map"].update(rot=14.0, flip=True)


def edit_grade(r):
    r["grade"].update(sat=0.4, contrast=1.3, vignette=0.7, halation=0.3, grain=0.05, shadow=[0.02, 0.0, 0.05])


def edit_blend(r):
    r["pieces"][-1].update(blend="screen", opacity=0.6, shadow=0.6)


def edit_add_disc(r):
    crop = {"cx": 0.4, "cy": 0.5, "k": 0.001, "fx": 540.0, "fy": 900.0, "rot": 0.0, "flip": False}
    r["pieces"].append({**r["pieces"][-1], "shape": {"type": "disc", "cx": 540, "cy": 900, "r": 260}, "map": crop, "body": False})


def test_type_and_route_up_close(update_vrt):
    """A thin line is too small to count in the smaller pictures, so its colour and width are compared here."""
    check("overlays-close", picture(drawn("slants", seed=2), 0.5), update_vrt, size=(360, 640))


EDITS = [v for k, v in sorted(globals().items()) if k.startswith("edit_")]


@pytest.mark.parametrize("edit", EDITS, ids=lambda f: f.__name__)
def test_every_kind_of_edit(edit, update_vrt):
    recipe = drawn("stack", seed=3)
    edit(recipe)
    check(f"edit-{edit.__name__[5:]}", picture(recipe), update_vrt)


@pytest.mark.parametrize("edit", EDITS, ids=lambda f: f.__name__)
def test_each_edit_changes_the_picture_enough_to_be_noticed(edit):
    """Otherwise a golden above proves nothing about that edit."""
    plain = drawn("stack", seed=3)
    changed = json.loads(json.dumps(plain))
    edit(changed)
    a, b = (picture(r).resize(SIZE, Image.Resampling.BOX) for r in (plain, changed))
    assert not same_picture(a, b)


@pytest.mark.parametrize("progress", [0.0, 0.3, 0.6, 1.1])
def test_reel_reveal(progress, update_vrt):
    composed = collage.paint(drawn("stack"), store(), 0.25, reel=True)
    check(f"reel-{progress}", collage._to_image(collage.reveal(composed, progress)), update_vrt)


def test_the_comparison_notices_a_small_seam_shift_and_ignores_rounding():
    recipe = drawn("stack", text=(), route=False)
    base = picture(recipe).resize(SIZE, Image.Resampling.BOX)
    noisy = np.asarray(base, dtype=np.int16) + random.Random(1).choice((-1, 1))
    assert same_picture(Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8)), base)
    edit_seam(recipe)
    assert not same_picture(picture(recipe).resize(SIZE, Image.Resampling.BOX), base)


def test_the_card_template(update_vrt):
    recipe = drawn("orbit", text=(), route=False)
    card = collage.apply_card(recipe, ["Distance|8.04|km", "Total time|47:42|", "Avg pace|5:56|/km"], "",
                              collage.route_from(ROUTE), store(), "GARMIN")
    check("card", picture(card, 0.5), update_vrt)
