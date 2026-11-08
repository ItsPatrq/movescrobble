import colorsys
import functools
import inspect
import json
import math
import random
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from collage_fixtures import compose, grade, route_layer, text_layer
from PIL import Image, ImageFilter

from movescrobble import cli, collage
from movescrobble.collage import H, Version, W


@functools.lru_cache
def scene(seed=0, size=(900, 1200)):
    """A bright, saturated picture with some structure: hard to make dark and quiet by accident."""
    gen = np.random.default_rng(seed)
    ys, xs = np.mgrid[0 : size[1], 0 : size[0]]
    a = np.stack([xs / size[0], ys / size[1], 1 - xs / size[0]], axis=-1) * 0.7 + 0.3
    a = a + gen.normal(0, 0.03, a.shape)
    return Image.fromarray((a.clip(0, 1) * 255).astype(np.uint8))


@pytest.fixture
def photos(tmp_path):
    paths = []
    for i in range(4):
        paths.append(tmp_path / f"p{i}.jpg")
        scene(i).save(paths[-1])
    return paths


def saturation(a):
    sample = a[::40, ::40].reshape(-1, 3)
    return float(np.mean([colorsys.rgb_to_hsv(*px)[1] for px in sample]))


def version(layout="shards", look="soho", seed=7, text=()):
    return Version("v01", seed, layout, look, [], text=list(text))


def test_runs_differ_unless_you_ask_for_a_seed(photos):
    names = [str(p) for p in photos]
    assert collage.draw_versions(6, 1, names, [], []) != collage.draw_versions(6, 2, names, [], [])
    assert collage.draw_versions(6, 1, names, [], []) == collage.draw_versions(6, 1, names, [], [])


def test_every_calm_layout_comes_up_before_any_comes_up_twice():
    layouts = [v.layout for v in collage.draw_versions(len(collage.CALM_LAYOUTS), 1, ["a", "b"], [], [])]
    assert sorted(layouts) == sorted(collage.CALM_LAYOUTS)


def test_a_run_spreads_over_looks():
    assert len({v.look for v in collage.draw_versions(12, 4, ["a", "b"], [], [])}) >= 4


def test_every_version_has_one_of_the_peek_photos():
    versions = collage.draw_versions(20, 1, ["a", "b"], ["p", "q"], [])
    assert {v.peek_photo for v in versions} == {"p", "q"}
    assert all(v.peek_photo not in v.photos for v in versions)


@pytest.mark.parametrize("layout", list(collage.LAYOUTS))
def test_no_layout_is_a_plain_grid_of_rectangles(layout):
    pieces = collage.LAYOUTS[layout](random.Random(11), 7)
    assert len(pieces) >= 3
    owner = collage._owners(pieces)
    ragged = 0
    for piece in pieces[-3:]:
        ys, xs = np.nonzero(piece.mask > 0.5)
        if len(xs):
            box = (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1)
            ragged += len(xs) / box < 0.97
    assert ragged or layout in ("ghost", "stack")  # `stack` is the plain one, by request
    assert owner.shape == (H, W)


@pytest.mark.parametrize("look", collage.COLOUR)
def test_colour_looks_are_darker_but_keep_their_colour(look):
    image = scene(1).resize((W, H))
    before = np.asarray(image, dtype=np.float32) / 255
    after = grade(before, look, random.Random(2))
    assert collage.luma(after).mean() < collage.luma(before).mean()
    assert saturation(after) > saturation(before) * 0.8


@pytest.mark.parametrize("layout", collage.CALM_LAYOUTS)
@pytest.mark.parametrize("seed", range(6))
def test_the_body_photo_is_one_ordinary_cell_of_the_pattern(layout, seed):
    pieces = collage.LAYOUTS[layout](random.Random(seed), 5)
    kept = [i for i, piece in enumerate(pieces) if piece.body]
    assert len(kept) == 1
    pieces = collage._with_peek(pieces, random.Random(seed), "bold", layout)
    at = kept[0]
    assert pieces[at].peek and at <= 1  # low in the pile: its neighbours arrive over it
    visible = collage._owners(pieces) == at
    whole = (pieces[at].mask > 0.5).sum()
    assert 0.05 < visible.mean() < 0.16
    assert visible.mean() < collage._largest_photo_share(pieces, collage._owners(pieces))
    assert 0.005 < 1 - visible.sum() / whole < 0.5  # something covers its edges, not much
    ys, xs = np.nonzero(visible)
    assert ys.max() <= H - collage.SAFE_BOTTOM + 40
    plain = ("full", "union", "disc")
    others = {piece.shape["type"] for i, piece in enumerate(pieces) if i != at and piece.shape["type"] not in plain}
    assert pieces[at].shape["type"] in others  # cut like its neighbours, not pasted on


def legs_recipe(layout, seed):
    v = version(layout, "natural", seed=seed)
    v.photos, v.peek_photo = ["a", "b", "c", "d"], "legs"
    store = collage.PhotoStore({**{k: scene(i) for i, k in enumerate(v.photos)}, "legs": legs_photo()})
    return collage.draw_recipe(v, store), store


def shows_both_legs(recipe, store):
    """Both legs lie inside what the body cell shows."""
    body = next(p for p in recipe["pieces"] if p["body"])
    m, aspect = body["map"], store.aspect("legs")
    owner = collage._owners([collage.Piece(p["shape"], opacity=p["opacity"]) for p in recipe["pieces"]])
    visible = owner == recipe["pieces"].index(body)
    x0, _, x1, _ = collage._inner_box(visible)
    seen = [(m["cx"] + m["k"] * (x - m["fx"])) / aspect for x in (x0, x1)]
    legs = collage.leg_columns(store.original("legs"))
    return seen[0] <= legs[0][0] + 0.01 and seen[1] >= legs[1][1] - 0.01


@pytest.mark.parametrize("layout", collage.CALM_LAYOUTS)
def test_most_versions_show_both_legs_and_some_show_one(layout):
    both = [shows_both_legs(*legs_recipe(layout, seed)) for seed in range(12)]
    assert sum(both) >= 8


def test_some_versions_frame_one_leg():
    both = [shows_both_legs(*legs_recipe(layout, seed)) for layout in ("slants", "stack") for seed in range(15)]
    assert not all(both)


def test_body_crop_holds_shorts_to_shoes_in_what_the_cell_shows():
    bounds, inner = (0.0, 0.0, 400.0, 800.0), (0.0, 60.0, 400.0, 760.0)
    m = collage.frame_cell(0.75, bounds, inner, [(0.3, 0.45), (0.6, 0.75)], 0.8, True, False, 0.6)
    assert m["cy"] + m["k"] * (760 - m["fy"]) == pytest.approx(0.8, abs=0.01)  # shoes at the bottom of what shows
    assert m["cy"] + m["k"] * (60 - m["fy"]) <= collage.PEEK_SPAN[0] + 0.01  # shorts at the top
    left, right = ((m["cx"] + m["k"] * (x - m["fx"])) / 0.75 for x in (0, 400))
    assert left <= 0.3 and right >= 0.75


def test_a_narrow_cell_widens_upwards_to_keep_both_legs():
    bounds, inner = (0.0, 0.0, 260.0, 700.0), (0.0, 0.0, 260.0, 700.0)
    m = collage.frame_cell(0.75, bounds, inner, [(0.3, 0.45), (0.6, 0.75)], 0.9, True, False, 0.6)
    left, right = ((m["cx"] + m["k"] * (x - m["fx"])) / 0.75 for x in (0, 260))
    assert left <= 0.3 and right >= 0.75
    assert m["cy"] + m["k"] * (0 - m["fy"]) <= 0.13  # it took more of the photo upwards


def test_framing_one_leg_in_a_wide_cell_keeps_the_knee_not_only_the_foot():
    bounds = (0.0, 0.0, 400.0, 500.0)
    m = collage.frame_cell(0.75, bounds, bounds, [(0.3, 0.45), (0.6, 0.75)], 0.85, True, True, 0.6)
    top, bottom = (m["cy"] + m["k"] * (y - m["fy"]) for y in (0, 500))
    assert top < 0.49 < bottom  # the middle of shorts to shoes
    assert bottom - top > 0.35  # not only a sock and a shoe


def test_too_narrow_or_asked_for_one_leg_keeps_the_right_one():
    legs = [(0.3, 0.45), (0.6, 0.75)]
    for bounds, one in (((0.0, 0.0, 100.0, 900.0), False), ((0.0, 0.0, 300.0, 700.0), True)):
        m = collage.frame_cell(0.75, bounds, bounds, legs, 0.9, True, one, 0.6)
        left, right = ((m["cx"] + m["k"] * (x - m["fx"])) / 0.75 for x in (bounds[0], bounds[2]))
        assert left >= 0.45 - 0.01 and left < 0.675 < right  # inside the gap or the leg, centred on the right leg


def test_harmonise_moves_a_photo_part_of_the_way_to_the_others():
    wall = Image.new("RGB", (100, 100), (170, 175, 185))
    warm = Image.new("RGB", (100, 100), (90, 60, 40))
    out = np.asarray(collage.harmonise(wall, [warm], 0.5), dtype=np.float32).mean((0, 1))
    assert 90 < out[0] < 170 and out[0] - out[2] > -15


def test_the_photos_own_colours_come_up_most_unless_narrowed():
    looks = [v.look for v in collage.draw_versions(60, 4, ["a", "b"], [], [])]
    assert looks.count(collage.NATURAL) > max(looks.count(other) for other in set(looks) - {collage.NATURAL})
    assert {v.look for v in collage.draw_versions(12, 4, ["a"], [], [], only_looks=["muted"])} <= set(collage.MUTED)


@pytest.mark.parametrize("look", collage.MUTED)
def test_muted_looks_are_darker_and_less_saturated(look):
    image = scene(1).resize((W, H))
    before = np.asarray(image, dtype=np.float32) / 255
    after = grade(before, look, random.Random(2))
    assert collage.luma(after).mean() < collage.luma(before).mean() * 0.85
    assert saturation(after) < saturation(before) * 0.75
    assert after.min() >= 0 and after.max() <= 1


def peeked(level="subtle", layout="shards", seed=1):
    pieces = collage.LAYOUTS[layout](random.Random(seed), 7)
    with_peek = collage._with_peek(pieces, random.Random(seed), level, layout)
    at = next(i for i, p in enumerate(with_peek) if p.peek)
    return with_peek, at


OPEN_LAYOUTS = [layout for layout in collage.LAYOUTS if layout not in collage.CALM_LAYOUTS]


@pytest.mark.parametrize("layout", OPEN_LAYOUTS)
@pytest.mark.parametrize("level", ["subtle", "bold"])
def test_a_peek_stays_a_detail(layout, level):
    pieces, at = peeked(level, layout)
    visible = collage._owners(pieces) == at
    assert 0 < visible.mean() <= collage.PEEK_SHARE[level][1]
    assert visible.mean() < collage._largest_photo_share(pieces, collage._owners(pieces))


@pytest.mark.parametrize("layout", ["eclipse", "shards", "slashes", "stairs", "blinds", "ghost"])
def test_a_peek_is_off_centre_and_clear_of_instagrams_ui(layout):
    pieces, at = peeked(layout=layout, seed=3)
    ys, xs = np.nonzero(pieces[at].mask > 0.5)
    assert ys.min() >= collage.SAFE_TOP - 60 and ys.max() <= H - collage.SAFE_BOTTOM + 60
    assert xs.min() > W * 0.5 or xs.max() < W * 0.6 or (xs.mean() < W * 0.35 or xs.mean() > W * 0.65)


def test_a_peek_is_partly_hidden_by_other_pieces():
    hidden = []
    for seed in range(6):
        pieces, at = peeked(seed=seed)
        whole = (pieces[at].mask > 0.5).sum()
        hidden.append(1 - (collage._owners(pieces) == at).sum() / whole)
    assert np.median(hidden) >= collage.PEEK_MIN_COVERED - 0.05


def test_a_peek_stays_small_on_a_composed_picture():
    composed = compose(version(), [scene(i) for i in range(3)], scene(9), "subtle")
    assert 0 < composed.peek_share <= collage.PEEK_SHARE["subtle"][1]


def test_in_a_reel_the_peek_shows_whole_then_later_pieces_cover_it():
    composed = compose(version("bands", "natural", seed=3), [Image.new("RGB", (900, 1200), (200, 60, 40))] * 3,
                               Image.new("RGB", (900, 1200), (40, 60, 200)))
    covered = (composed.under_mask > 0.999) & (composed.start > composed.under_start + 0.1)
    assert covered.mean() > 0.005
    progress = float(np.median(composed.under_start[covered])) + 0.1
    ready = covered & (composed.under_start < progress - 0.06) & (composed.start > progress)
    frame = collage.reveal(composed, progress)
    assert np.abs(frame[ready] - composed.under[ready]).max() < 0.01
    assert np.abs(frame[ready] - composed.final[ready]).mean() > 0.1
    assert np.abs(collage.reveal(composed, 1.3) - composed.final).max() < 1e-5


@pytest.mark.parametrize("layout", collage.CALM_LAYOUTS)
def test_in_a_reel_the_body_photo_never_arrives_first_or_last(layout):
    for seed in range(5):
        recipe, _ = legs_recipe(layout, seed)
        order = recipe["reel"]["order"]
        at = order.index(next(i for i, p in enumerate(recipe["pieces"]) if p["body"]))
        assert 2 <= at < len(order) - 1  # after the opening picture and at least one other, with one still to come


def test_text_stays_clear_of_instagrams_ui():
    for seed in range(8):
        color, alpha = text_layer(["6 Nov 2026", "8.04 km"], random.Random(seed))
        left, top, right, bottom = alpha.getbbox()
        assert top >= collage.SAFE_TOP and bottom <= H - collage.SAFE_BOTTOM and right <= W - collage.SAFE_RIGHT


def test_photo_sideways_in_exif_comes_out_upright(tmp_path):
    path = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90
    Image.new("RGB", (400, 200)).save(path, exif=exif)
    assert collage.load_photo(path).size == (200, 400)


def test_heic_from_a_phone_loads(tmp_path):
    from pillow_heif import register_heif_opener

    register_heif_opener()
    path = tmp_path / "phone.heic"
    scene(size=(120, 160)).save(path)
    assert collage.load_photo(path).size == (120, 160)


def test_folders_are_expanded_and_other_files_refused(tmp_path, photos):
    (tmp_path / "notes.txt").write_text("x")
    assert collage.find_photos([tmp_path]) == sorted(photos)
    with pytest.raises(SystemExit, match="not a photo"):
        collage.find_photos([tmp_path / "notes.txt"])


def test_a_collage_needs_a_photo(tmp_path):
    with pytest.raises(SystemExit, match="at least 1"):
        collage.make([], tmp_path / "out", 1, reels=False)


def test_output_carries_no_location_and_the_peek_photo_stays_out_of_the_main_pieces(tmp_path, photos):
    exif = Image.Exif()
    exif.get_ifd(0x8825)[1] = "N"  # a GPS block
    tagged = tmp_path / "gps.jpg"
    scene(5).save(tagged, exif=exif)
    assert Image.open(tagged).getexif().get_ifd(0x8825)
    peek = tmp_path / "peek.jpg"
    scene(6).save(peek)

    sheet, done = collage.make([*photos, tagged], tmp_path / "out", 3, [peek], reels=False, seed=8, workers=1)

    assert sheet.exists() and len(done) == 3
    for v in done:
        still = Image.open(tmp_path / "out" / v.still)
        assert still.size == (W, H) and not still.getexif()
        assert str(peek) not in v.photos
    listed = json.loads((tmp_path / "out" / "versions.json").read_text())
    assert listed["seed"] == 8 and [v["id"] for v in listed["versions"]] == ["v01", "v02", "v03"]


@pytest.mark.ffmpeg
def test_a_reel_is_vertical_silent_and_plays_anywhere(tmp_path):
    composed = compose(version(), [scene(0), scene(1)], None)
    path = tmp_path / "reel.mp4"
    collage._encode(composed, path, {"grain_seed": 1, "seconds": 0.5, "reveal": 0.2, "push": 0.055})
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt",
                            "-of", "json", str(path)], capture_output=True, text=True, check=True)
    streams = json.loads(probe.stdout)["streams"]
    assert len(streams) == 1
    assert (streams[0]["codec_name"], streams[0]["width"], streams[0]["height"], streams[0]["pix_fmt"]) == (
        "h264", W, H, "yuv420p")


RUN = {
    "activityTypeDTO": {"typeKey": "running"},
    "summaryDTO": {"startTimeLocal": "2026-11-06T15:50:56.0", "distance": 8035.73, "elapsedDuration": 2993.67,
                   "movingDuration": 2822.69, "averageSpeed": 2.81, "averageMovingSpeed": 2.85, "averageHR": 149.0,
                   "elevationGain": 28.0},
}


def line_of_points(count=200, step=0.0005):
    return [(52.0 + i * step, 21.0) for i in range(count)]  # about 55 m apart


def details(points):
    return {"geoPolylineDTO": {"polyline": [{"lat": a, "lon": b} for a, b in points]}}


def test_data_reads_as_on_a_watch():
    data = collage.activity_data(RUN)
    assert data.stat("date") == "06 Nov 2026" and data.stat("distance") == "8.04 km" and data.stat("hr") == "149 bpm"
    assert data.stat("speed") == "10.3 km/h" and data.stat("pace") == "5:51 /km" and data.stat("climb") == "28 m up"


def test_time_distance_and_pace_agree_by_using_moving_time():
    assert collage.activity_data(RUN).stat("time") == "47:03"


def test_a_swim_reads_in_metres_with_no_speed_or_route():
    swim = {"activityTypeDTO": {"typeKey": "lap_swimming"}, "summaryDTO": {"distance": 1450.0, "averageSpeed": 0.7}}
    data = collage.activity_data(swim, {"geoPolylineDTO": None})
    assert data.stat("distance") == "1450 m" and data.stat("pace") == "2:23 /100m"
    assert data.stat("speed") is None and "route" not in data.available()


def test_missing_fields_are_simply_not_offered():
    assert collage.activity_data({}).available() == []


def test_route_ends_are_cut_so_it_does_not_point_at_your_door():
    points = line_of_points()
    kept = collage.trim_route(points, 500)
    assert collage._metres(points[0], kept[0]) >= 500 and collage._metres(points[-1], kept[-1]) >= 500
    assert collage.activity_data(RUN, details(points)).route[0] != points[0]


def test_a_route_too_short_to_trim_is_dropped_rather_than_exposed():
    assert collage.trim_route(line_of_points(15), 500) == []


def test_trim_can_be_turned_off():
    points = line_of_points(10)
    assert collage.trim_route(points, 0) == points


def test_heart_rate_shows_only_sometimes_and_some_versions_show_nothing():
    data = collage.activity_data(RUN, details(line_of_points()))
    drawn = collage.draw_versions(200, 3, ["a"], [], [], data=data)
    hr = sum("hr" in v.data for v in drawn)
    assert 30 < hr < 110
    assert any(not v.data for v in drawn) and any("route" in v.data for v in drawn)
    assert any("149 BPM" in line.upper() for v in drawn for line in v.text)


def test_data_list_shows_exactly_that_on_every_version():
    data = collage.activity_data(RUN, details(line_of_points()))
    for v in collage.draw_versions(10, 4, ["a"], [], [], data=data, fields=["speed", "hr", "route"]):
        assert v.data == ["speed", "hr", "route"] and v.text == ["10.3 km/h", "149 bpm"]


def test_cycling_leads_with_speed():
    ride = {"activityTypeDTO": {"typeKey": "cycling"}, "summaryDTO": {"averageSpeed": 3.9}}
    drawn = collage.draw_versions(60, 5, ["a"], [], [], data=collage.activity_data(ride))
    assert sum("speed" in v.data for v in drawn) > sum("pace" in v.data for v in drawn)


def test_route_stays_clear_of_instagrams_ui_and_draws_in_order():
    route = collage.trim_route(line_of_points(), 500)
    for seed in range(6):
        alpha, along = route_layer(route, random.Random(seed))
        ys, xs = np.nonzero(alpha > 0.02)
        assert ys.min() >= collage.SAFE_TOP and ys.max() <= H - collage.SAFE_BOTTOM
        assert xs.max() <= W - collage.SAFE_RIGHT and along.max() > 0.9


def test_versions_json_holds_no_coordinates(tmp_path, photos):
    points = line_of_points()
    data = collage.activity_data(RUN, details(points))
    collage.make(photos[:2], tmp_path / "o", 2, reels=False, seed=2, workers=1, data=data, fields=["route", "distance"])
    listed = (tmp_path / "o" / "versions.json").read_text()
    assert "52.0" not in listed and "21.0" not in listed and "route" in listed


def test_route_appears_on_the_picture():
    route = collage.trim_route(line_of_points(), 500)
    plain = version(layout="shards", seed=9)
    with_route = version(layout="shards", seed=9)
    with_route.data = ["route"]
    shots = [compose(v, [scene(0), scene(1)], None, route=route).final for v in (plain, with_route)]
    assert np.abs(shots[0] - shots[1]).max() > 0.2


@pytest.mark.parametrize("text", [["8.04 km"], ["8.04 km", "5:51 /km", "149 bpm"], ["a very long line that has to shrink " * 3]])
def test_every_type_treatment_stays_clear_of_instagrams_ui(text):
    for seed in range(12):
        _, alpha = text_layer(text, random.Random(seed))
        left, top, right, bottom = alpha.getbbox()
        assert top >= collage.SAFE_TOP and bottom <= H - collage.SAFE_BOTTOM and right <= W - collage.SAFE_RIGHT + 60


@pytest.mark.parametrize("count", [1, 2, 3])
def test_few_photos_make_every_layout(count):
    for layout in collage.LAYOUTS:
        composed = compose(version(layout=layout), [scene(i) for i in range(count)], None)
        assert composed.final.shape == (H, W, 3) and composed.final.std() > 0.01


def test_few_photos_cut_one_continuous_picture():
    """Pieces of one photo share a crop, only slipped."""
    v = version("slashes", "natural", seed=4)
    v.photos = ["a", "b"]
    recipe = collage.draw_recipe(v, collage.PhotoStore({"a": scene(0), "b": scene(1)}))
    by_photo: dict[str, list] = {}
    for piece in recipe["pieces"][1:]:
        by_photo.setdefault(piece["photo"], []).append(piece["map"])
    shared = max(by_photo.values(), key=len)
    assert len(shared) > 1 and len({(m["cx"], m["cy"], m["k"]) for m in shared}) == 1
    assert all(abs(m["fx"] - W / 2) <= collage.SLIP_MARGIN and abs(m["fy"] - H / 2) <= collage.SLIP_MARGIN for m in shared)


def test_command_passes_everything_through(monkeypatch, tmp_path, capsys, photos):
    seen = {}

    def fake_make(found, out, count, peek, level, text, reels, seed, layouts, looks, only=None, data=None, fields=None,
                  define=None, peek_side="either"):
        seen.update(found=found, out=out, count=count, peek=peek, level=level, text=text, reels=reels, layouts=layouts)
        return out / "contact-sheet.jpg", []

    monkeypatch.setattr(collage, "make", fake_make)
    monkeypatch.setattr("sys.argv", ["movescrobble", "collage", str(photos[0]), str(photos[1]), "--peek", str(photos[2]),
                                     "--peek-level", "bold", "-n", "5", "--text", "hello", "--no-reels",
                                     "--layouts", "slashes,ghost", "--out", str(tmp_path / "o")])
    cli.main()
    assert seen["found"] == photos[:2] and seen["peek"] == [photos[2]] and seen["level"] == "bold"
    assert seen["count"] == 5 and seen["text"] == ["hello"] and seen["reels"] is False
    assert seen["layouts"] == ["slashes", "ghost"] and Path(seen["out"]).name == "o"
    assert "contact sheet" in capsys.readouterr().out


def test_only_with_a_seed_repeats_a_chosen_version_exactly(tmp_path, photos):
    _, every = collage.make(photos, tmp_path / "all", 3, reels=False, seed=21, workers=1)
    _, chosen = collage.make(photos, tmp_path / "one", 3, reels=False, seed=21, workers=1, only=["v02"])
    assert [v.id for v in chosen] == ["v02"]
    assert (tmp_path / "one" / chosen[0].still).read_bytes() == (tmp_path / "all" / every[1].still).read_bytes()


def test_only_with_no_match_says_so(tmp_path, photos):
    with pytest.raises(SystemExit, match="none of"):
        collage.make(photos, tmp_path / "o", 2, reels=False, seed=1, workers=1, only=["v99"])


def test_a_route_keeps_clear_of_readable_type():
    route = collage.trim_route(line_of_points(), 500)
    for seed in range(20):
        rng = random.Random(seed)
        _, alpha = text_layer(["8.04 km", "10.3 km/h"], rng)
        words = np.asarray(alpha, dtype=np.float32) / 255
        line, _ = route_layer(route, rng, words)
        assert not ((line > 0.02) & (words > collage.READABLE_TYPE)).any()


def half_bright_busy_picture():
    gen = np.random.default_rng(1)
    picture = np.full((H, W, 3), 0.08, dtype=np.float32)
    picture[:, : W // 2] = gen.random((H, W // 2, 3), dtype=np.float32) * 0.3 + 0.7
    return picture


def test_type_and_route_go_where_the_picture_is_dark_and_calm():
    route = collage.trim_route(line_of_points(), 500)
    picture = half_bright_busy_picture()
    for seed in range(6):
        rng = random.Random(seed)
        _, alpha = text_layer(["8.04 km", "10.3 km/h"], rng, picture)
        words = np.asarray(alpha, dtype=np.float32) / 255
        assert (words[:, W // 2 :] > 0).sum() > (words[:, : W // 2] > 0).sum()  # large type may not fit the dark half whole
        line, _ = route_layer(route, rng, words, picture)
        assert not ((line > 0.02) & (words > collage.READABLE_TYPE)).any()


def test_type_and_route_keep_off_the_body_photo():
    route = collage.trim_route(line_of_points(), 500)
    avoid = np.zeros((H, W), dtype=np.float32)
    avoid[:, :300] = 1
    for seed in range(6):
        rng = random.Random(seed)
        _, alpha = text_layer(["8.04 km"], rng, None, avoid)
        words = np.asarray(alpha, dtype=np.float32) / 255
        assert not (words > 0)[:, :300].any()
        line, _ = route_layer(route, rng, words, None, avoid)
        assert not (line > 0.02)[:, :300].any()


def test_type_reads_on_the_worst_picture():
    picture = np.full((H, W, 3), 0.75, dtype=np.float32)
    _, alpha = text_layer(["8.04 km"], random.Random(2), picture)
    words = np.asarray(alpha, dtype=np.float32) / 255
    under = collage._darken_under(picture, words, 4, 0.7, 34, 0.5)
    ring = (np.asarray(alpha.filter(ImageFilter.MaxFilter(7)), dtype=np.float32) > 0) & (words == 0)
    assert collage.luma(under)[ring].mean() < 0.3


def test_a_translucent_line_shows_the_picture_not_its_dark_edge():
    picture = np.full((H, W, 3), 0.8, dtype=np.float32)
    line = np.zeros((H, W), dtype=np.float32)
    line[900:920, 200:800] = 0.66  # a stroke drawn at 66% opacity
    under = collage._darken_under(picture, line, 3, 0.95, 22, 0.45, opacity=0.66)
    assert np.allclose(under[905:915, 300:700], 0.8)  # the picture under the stroke, untouched
    assert under[897, 500].mean() < 0.4  # the dark edge still around it


def test_stack_is_rows_of_equal_height():
    for seed in range(8):
        pieces = collage.stack(random.Random(seed), 5)
        tops = sorted({p.shape["points"][0][1] for p in pieces if not p.body})
        rows = len(tops)
        assert rows in (3, 4)
        for i, top in enumerate(tops[1:], 1):
            assert abs(top - H * i / rows) < 40
        assert all(y == p.shape["points"][0][1] for p in pieces for _, y in p.shape["points"][:2])  # straight
        assert (collage._owners(collage._with_underlay(pieces, random.Random(seed))) >= 0).all()


@pytest.mark.parametrize("layout", ["orbit", "ripple", "arcade"])
def test_curved_shapes_hold_the_calmest_photo(layout):
    calm = Image.new("RGB", (900, 1200), (200, 120, 130))
    gen = np.random.default_rng(3)
    busy = [Image.fromarray((gen.random((1200, 900, 3)) * 255).astype(np.uint8)) for _ in range(3)]
    assert collage.detail_of(calm) < collage.detail_of(busy[0])
    for seed in range(4):
        v = version(layout, "natural", seed=seed)
        v.photos = ["a", "b", "c", "d"]
        final = compose(v, [busy[0], calm, busy[1], busy[2]], None).final
        pieces = collage.LAYOUTS[layout](random.Random(0), 5)
        assert any(p.calm for p in pieces) and final.std() > 0


def test_arcade_has_no_loose_disc():
    source = inspect.getsource(collage.arcade)
    assert "sun" not in source


def legs_photo(shoes_to=0.8, carpet=True):
    """Two legs in skin colour on a pale wall, black socks and shoes, grey floor below."""
    w, h = 900, 1200
    a = np.zeros((h, w, 3), dtype=np.uint8)
    a[:] = (200, 200, 205)
    a[: int(h * 0.3)] = (20, 20, 22)  # shorts
    for x0, x1 in ((270, 420), (520, 670)):
        a[int(h * 0.3) : int(h * 0.65), x0:x1] = (205, 150, 120)
        a[int(h * 0.65) : int(h * shoes_to), x0:x1] = (12, 12, 12)
    if carpet:
        a[int(h * (shoes_to + 0.02)) :] = (140, 140, 138)
    return Image.fromarray(a)


def test_body_crop_stops_at_the_shoes_where_the_floor_begins():
    assert collage.shoe_line(legs_photo(0.7)) == pytest.approx(0.725, abs=0.02)
    assert collage.shoe_line(legs_photo(1.0, carpet=False)) == 1.0


def test_body_crop_finds_the_legs():
    legs = collage.leg_columns(legs_photo())
    assert len(legs) == 2 and abs(legs[0][0] - 0.3) < 0.02 and abs(legs[1][1] - 0.745) < 0.02


@pytest.mark.parametrize("layout", list(collage.LAYOUTS))
@pytest.mark.parametrize("seed", range(6))
def test_no_layout_leaves_gaps_of_background_between_pictures(layout, seed):
    rng = random.Random(seed)
    pieces = collage._with_underlay(collage.LAYOUTS[layout](rng, 6), rng)
    assert (collage._owners(pieces) >= 0).all()


@pytest.mark.parametrize("layout", list(collage.LAYOUTS))
def test_a_bright_photo_leaves_almost_nothing_black(layout):
    a = compose(version(layout, "noir"), [scene(0), scene(1)], None).final
    assert (collage.luma(a) < 0.05).mean() < 0.02


@pytest.mark.parametrize("layout", list(collage.LAYOUTS))
def test_a_reel_opens_on_a_picture_and_not_on_black(layout):
    base = compose(version(layout, "noir"), [scene(0), scene(1)], None).base
    assert (collage.luma(base) < 0.05).mean() < 0.02


def test_eclipse_shows_one_whole_picture_in_its_disc_and_rings_in_register():
    pieces = collage.eclipse(random.Random(5), 5)
    under, *arcs, disc = pieces
    assert under.full and under.photo != disc.photo
    assert disc.own_crop and (disc.mask > 0.5).mean() > 0.1
    assert {a.photo for a in arcs} == {disc.photo} and len({a.shift for a in arcs}) == 1


def test_only_photo_is_not_laid_exactly_under_itself():
    plain = compose(version("slashes", "chrome"), [scene(0)], None).base
    assert not np.allclose(plain, plain[:, ::-1], atol=0.02)


def flat(rgb, size=(300, 300)):
    return Image.new("RGB", size, rgb)


def hue_of(img):
    r, g, b = np.asarray(img, dtype=np.float32).reshape(-1, 3).mean(0) / 255
    return colorsys.rgb_to_hsv(r, g, b)


def test_unify_turns_a_blue_sky_warm_and_keeps_orange_leaves_strong():
    palette = (28.0, -12.0)
    sky_h, sky_s, _ = hue_of(collage.unify(flat((70, 120, 230)), palette))
    leaf_h, leaf_s, _ = hue_of(collage.unify(flat((220, 120, 30)), palette))
    assert sky_h * 360 < 60 or sky_h * 360 > 320
    assert leaf_h * 360 < 45 and leaf_s > sky_s


def test_unify_makes_a_bright_and_a_dark_photo_land_on_the_same_levels():
    bright = Image.fromarray(np.uint8(np.clip(np.asarray(scene(1), dtype=np.float32) * 1.2, 0, 255)))
    dark = Image.fromarray(np.uint8(np.asarray(scene(2), dtype=np.float32) * 0.25))
    unified = [np.asarray(collage.unify(p, (28.0, -12.0)), dtype=np.float32) / 255 for p in (bright, dark)]
    medians = [np.median(collage.luma(a)) for a in unified]
    assert abs(medians[0] - medians[1]) < 0.03 and all(0.08 < m < 0.15 for m in medians)


def test_unify_gives_a_grey_wall_the_warm_cast():
    h, s, _ = hue_of(collage.unify(flat((150, 150, 152)), (28.0, -12.0)))
    assert 15 < h * 360 < 45 and s > 0.15


def test_palette_comes_from_the_photos_and_stays_in_range():
    warm, dusk = collage.palette_of([flat((230, 120, 30)), flat((225, 160, 190))])
    assert collage.WARM_HUES[0] <= warm <= collage.WARM_HUES[1] and collage.DUSK_HUES[0] <= dusk <= collage.DUSK_HUES[1]
    assert collage.palette_of([flat((10, 10, 10))]) == (28.0, 8.0)


def test_sunset_unifies_the_peek_too():
    composed = compose(version("slashes", "sunset"), [scene(0), scene(1)], flat((60, 120, 230)))
    assert composed.final.shape == (H, W, 3)
    r, g, b = composed.final.reshape(-1, 3).mean(0)
    assert r >= b


def test_unify_lets_strong_warm_light_stand_out_from_a_quiet_photo():
    a = np.full((300, 300, 3), (90, 90, 100), dtype=np.uint8)
    a[140:160, 140:160] = (240, 90, 30)
    out = np.asarray(collage.unify(Image.fromarray(a), (28.0, -12.0)), dtype=np.float32) / 255
    assert collage.luma(out[150, 150]) > collage.luma(out[20, 20]) * 1.5
    assert colorsys.rgb_to_hsv(*out[150, 150])[1] > 0.6


@pytest.mark.parametrize("seed", range(4))
def test_a_peek_is_a_tall_strip_so_it_shows_a_whole_leg(seed):
    pieces, at = peeked(layout="slashes", seed=seed)
    ys, xs = np.nonzero(pieces[at].mask > 0.5)
    assert ys.max() - ys.min() > (xs.max() - xs.min()) * 1.4


def test_a_peek_crop_sits_to_one_side_of_the_focus():
    x, y = collage._beside((0.5, 0.5), random.Random(1))
    assert abs(x - 0.5) == pytest.approx(collage.PEEK_ASIDE) and y == 0.5


def test_sunset_leaves_nothing_grey():
    """A dull blue-grey picture comes out warm in every tone."""
    gradient = np.linspace(40, 220, 300, dtype=np.float32)[:, None]
    grey = np.stack([gradient, gradient * 1.02, gradient * 1.08], axis=-1).repeat(300, axis=1)
    out = np.asarray(collage.unify(Image.fromarray(np.uint8(grey)), (28.0, 8.0)), dtype=np.float32) / 255
    r, g, b = out[..., 0].mean(), out[..., 1].mean(), out[..., 2].mean()
    assert r > g > b and (r - b) / r > 0.3




def test_natural_keeps_the_photos_colours():
    image = np.asarray(scene(1).resize((W, H)), dtype=np.float32) / 255
    before = np.asarray(collage.enhance(scene(1).resize((W, H))), dtype=np.float32) / 255
    after = grade(before, "natural", random.Random(2))
    centre = (slice(600, 1300), slice(250, 830))
    assert np.abs(image[centre].mean((0, 1)) - after[centre].mean((0, 1))).max() < 0.05
    hues = [colorsys.rgb_to_hsv(*a[centre].mean((0, 1)))[0] for a in (image, after)]
    assert abs(hues[0] - hues[1]) < 0.02


def test_natural_is_the_same_in_every_version():
    a = np.full((H, W, 3), 0.5, dtype=np.float32)
    one, other = (grade(a, "natural", random.Random(seed))[960, 540] for seed in (1, 9))
    assert np.array_equal(one, other)


def muscled_leg(size=(800, 1000)):
    """Skin with a soft muscle-sized shadow, beside a grey wall."""
    ys, xs = np.mgrid[0 : size[1], 0 : size[0]].astype(np.float32)
    shade = np.exp(-((xs - 200) ** 2 + (ys - 500) ** 2) / (2 * 60**2)) * 40
    a = np.stack([200 - shade, 150 - shade, 120 - shade], axis=-1)
    a[:, 400:] = 150
    return Image.fromarray(a.astype(np.uint8))


def test_define_muscle_deepens_shading_on_skin_and_leaves_the_wall():
    leg = muscled_leg()
    before, after = (np.asarray(p, dtype=np.float32) / 255 for p in (leg, collage.define_muscle(leg)))
    depth = lambda a: collage.luma(a[100, 200]) - collage.luma(a[500, 200])  # noqa: E731
    assert depth(after) > depth(before) * 1.05
    assert depth(after) < depth(before) * 1.8  # slightly
    assert np.abs(after[:, 500:] - before[:, 500:]).max() < 0.01


def test_define_muscle_moves_nothing():
    leg = muscled_leg()
    after = np.asarray(collage.define_muscle(leg), dtype=np.float32)[..., 0][:, :340]
    darkness = np.clip(after.max() - after - 15, 0, None)  # the shadow, not the slight edge along the wall
    ys, xs = np.mgrid[0 : 1000, 0 : 340]
    centre = (xs * darkness).sum() / darkness.sum(), (ys * darkness).sum() / darkness.sum()
    assert centre == pytest.approx((200, 500), abs=4)


def test_a_photo_to_define_is_cut_like_the_others(tmp_path, photos):
    _, versions = collage.make(photos[:2], tmp_path, count=2, reels=False, define=[photos[2]], workers=1, seed=3)
    assert all(str(photos[2]) in v.photos and v.define == [str(photos[2])] and not v.peek_photo for v in versions)


def test_a_peek_can_be_framed_on_a_chosen_side():
    assert collage._beside((0.5, 0.5), random.Random(1), "right")[0] == pytest.approx(0.5 + collage.PEEK_ASIDE)
    assert collage._beside((0.5, 0.5), random.Random(1), "left")[0] == pytest.approx(0.5 - collage.PEEK_ASIDE)


def test_a_defined_peek_photo_stays_out_of_the_main_pieces(tmp_path, photos):
    only_peek = [photos[2]]
    _, versions = collage.make(photos[:2], tmp_path, count=2, reels=False, peek=only_peek, define=only_peek, workers=1, seed=3)
    assert all(str(photos[2]) not in v.photos and v.peek_photo == str(photos[2]) for v in versions)


def recipe_of(layout="stack", look="natural", seed=7, text=("8.04 km", "5:51 /km"), route=True):
    v = version(layout, look, seed=seed, text=text)
    v.photos, v.peek_photo = ["a", "b", "c", "d"], "legs"
    v.define = ["legs"]
    v.data = ["route"] if route else []
    store = collage.PhotoStore({**{k: scene(i) for i, k in enumerate(v.photos)}, "legs": legs_photo()})
    points = collage.trim_route(line_of_points(), 500)
    return collage.draw_recipe(v, store, "bold", points), store


def test_a_recipe_is_plain_json_and_paints_the_same_after_a_round_trip():
    recipe, store = recipe_of()
    again = json.loads(json.dumps(recipe))
    assert np.array_equal(collage.paint(recipe, store, 0.25).final, collage.paint(again, store, 0.25).final)


def test_a_recipe_holds_no_coordinates():
    recipe, _ = recipe_of()
    assert not re.search(r"(?<![\d.])(52|21)\.\d", json.dumps(recipe["route"]))
    assert all(0 <= u <= 1 and 0 <= v <= 1 for u, v in recipe["route"]["points"])


def test_painting_is_the_same_picture_at_any_scale():
    recipe, store = recipe_of()
    full = collage.paint(recipe, store, 1.0).final
    small = collage.paint(recipe, store, 0.5).final
    size = (small.shape[1], small.shape[0])
    reduced = np.asarray(collage._to_image(full).resize(size, Image.Resampling.BOX), dtype=np.float32) / 255
    assert np.abs(reduced - small).mean() < 0.035


def test_moving_a_crop_moves_the_picture_only_inside_its_piece():
    recipe, store = recipe_of(text=(), route=False)
    before = collage.paint(recipe, store, 0.25)
    at = 2
    recipe["pieces"][at]["map"]["cx"] += 0.1
    after = collage.paint(recipe, store, 0.25)
    changed = np.abs(before.final - after.final).max(axis=2) > 0.02
    assert changed.any()
    assert (before.owner[changed] == at).mean() > 0.9


def test_moving_a_seam_changes_shapes_and_not_the_picture_inside():
    recipe, store = recipe_of(text=(), route=False)
    piece = next(p for p in recipe["pieces"] if p["shape"]["type"] == "poly" and not p["body"])
    keep = dict(piece["map"])
    piece["shape"]["points"] = [[x, y + 40] for x, y in piece["shape"]["points"]]
    assert piece["map"] == keep
    assert collage.paint(recipe, store, 0.25).final.std() > 0


def test_a_crop_past_the_photos_edge_shows_what_lies_below_not_a_stretched_edge():
    recipe, store = recipe_of(text=(), route=False)
    piece = recipe["pieces"][-1]
    piece["map"]["cx"] = 50.0
    composed = collage.paint(recipe, store, 0.25)
    assert (composed.owner != len(recipe["pieces"]) - 1).all()


def test_hiding_a_piece_or_type_removes_it():
    recipe, store = recipe_of()
    full = collage.paint(recipe, store, 0.25).final
    recipe["pieces"][-1]["visible"] = False
    hidden = collage.paint(recipe, store, 0.25)
    assert (hidden.owner != len(recipe["pieces"]) - 1).all()
    recipe["pieces"][-1]["visible"] = True
    recipe["route"]["visible"] = False
    assert np.abs(collage.paint(recipe, store, 0.25).final - full).max() > 0.2


def test_piece_adjustments_apply_to_that_piece():
    recipe, store = recipe_of(text=(), route=False)
    before = collage.paint(recipe, store, 0.25)
    at = 2
    recipe["pieces"][at]["adjust"]["exposure"] = 0.5
    after = collage.paint(recipe, store, 0.25)
    inside, outside = before.owner == at, before.owner != at
    assert after.final[inside].mean() < before.final[inside].mean() * 0.7
    assert np.abs(after.final[outside] - before.final[outside]).mean() < 0.02


def test_leg_colour_and_muscle_are_recipe_settings():
    recipe, store = recipe_of(text=(), route=False)
    at = next(i for i, p in enumerate(recipe["pieces"]) if p["body"])
    base = collage.paint(recipe, store, 0.25)
    inside = base.owner == at
    for field, value in (("match", 0.0), ("muscle", 0.0)):
        changed = json.loads(json.dumps(recipe))
        changed[field] = value
        picture = collage.paint(changed, store, 0.25).final
        if field == "match":
            assert np.abs(picture[inside] - base.final[inside]).mean() > 0.005
        assert np.abs(picture[~inside] - base.final[~inside]).mean() < 0.002  # only its soft edge differs


def test_a_make_writes_each_recipe_and_repaints_its_still(tmp_path, photos):
    _, done = collage.make(photos, tmp_path, 2, reels=False, seed=9, workers=1)
    recipe = json.loads((tmp_path / done[0].recipe).read_text())
    painted = collage.render_still(recipe, collage.PhotoStore())
    still = np.asarray(Image.open(tmp_path / done[0].still), dtype=np.float32)
    assert np.abs(np.asarray(painted, dtype=np.float32) - still).mean() < 3


def test_a_piece_can_fade_and_tint():
    a = np.full((4, 4, 3), 0.0, dtype=np.float32)
    assert collage._adjusted(a, {"fade": 0.2}).min() == pytest.approx(0.2)  # blacks lifted to a matte grey
    grey = np.full((4, 4, 3), 0.5, dtype=np.float32)
    pink = collage._adjusted(grey, {"tint": 0.5})[0, 0]
    assert pink[0] > 0.5 > pink[1] and pink[2] > 0.5
    assert np.array_equal(collage._adjusted(grey, {}), grey)  # recipes from before stay as they were


def test_the_leg_photo_is_told_by_its_photo_wherever_it_goes():
    recipe = {"peek_photo": "legs.jpg"}
    assert collage.shows_legs({"photo": "legs.jpg", "body": False}, recipe)
    assert not collage.shows_legs({"photo": "park.jpg", "body": True}, recipe)  # a stale flag from before
    assert not collage.shows_legs({"photo": "legs.jpg"}, {"peek_photo": None})


# --- the card


def test_a_card_reads_as_the_watch_does():
    garmin = {**RUN, "activityName": "Warsaw Running", "summaryDTO": {**RUN["summaryDTO"], "duration": 2862.0}}
    data = collage.activity_data(garmin)
    assert data.name == "Warsaw Running"
    assert data.card_lines() == ["Distance|8.04|km", "Total time|47:42|", "Avg pace|5:56|/km"]  # timer, not moving time
    swim = collage.ActivityData(kind="lap_swimming", distance_m=1500, total_s=1800, average_speed_ms=1500 / 1800)
    assert swim.card_lines() == ["Distance|1500|m", "Total time|30:00|", "Avg pace|2:00|/100m"]
    assert collage.card_lines_from({"distance": "8.04 km", "time": "47:03", "pace": "5:51 /km", "hr": "149 bpm"}) == [
        "Distance|8.04|km", "Time|47:03|", "Avg pace|5:51|/km"]  # an older run's moving time is not called total


def test_a_card_puts_title_stats_and_route_where_they_belong():
    store = collage.PhotoStore({**{f"#{i}": scene(i) for i in range(4)}, "#legs": legs_photo()})
    version = Version("v01", 7, "orbit", "natural", [f"#{i}" for i in range(4)], "#legs")
    recipe = collage.draw_recipe(version, store)
    route = collage.route_from(line_of_points())
    card = collage.apply_card(recipe, ["Distance|8.04|km", "Total time|47:42|", "Avg pace|5:56|/km"], "Warsaw Running",
                              route, store)
    title, stats = card["text"]
    assert title["lines"] == ["Warsaw Running"] and title["y"] >= collage.SAFE_TOP and stats["style"] == "stats"
    tall = collage._type_image(stats).height
    assert stats["y"] + tall <= H - collage.CARD_BOTTOM
    assert stats["y"] == min(collage._legs_bottom(card) - collage.CARD_STATS_OVERLAP, H - collage.CARD_BOTTOM - tall)
    cx, cy, r = collage._circle_of(card)
    box = card["route"]
    corners = [(box["x"] + u * box["w"], box["y"] + v * box["h"]) for u in (0, 1) for v in (0, 1)]
    assert all(math.hypot(x - cx, y - cy) <= r for x, y in corners)  # the whole route lies in the circle
    assert box["width"] == collage.CARD_ROUTE_WIDTH and box["opacity"] < 1 and box["edge"] > 0.9  # see-through, edged
    picture = collage.render_still(card, store, 0.25)
    assert picture.size == (270, 480)


def test_a_column_of_stats_has_small_labels_over_large_figures():
    font = collage.available_fonts()[0]
    one = collage._stats_block(["Distance|8.04|km"], font, 100)
    three = collage._stats_block(["Distance|8.04|km", "Total time|47:42|", "Avg pace|5:56|/km"], font, 100)
    assert three.height > one.height * 2.4
    text = np.asarray(one) > 0
    rows = np.nonzero(text.any(1))[0]
    gap = np.nonzero(np.diff(rows) > 1)[0]
    label, figure = rows[gap[0]] - rows[0] + 1, rows[-1] - rows[gap[0] + 1] + 1  # the first run of rows, then the rest
    assert len(gap) == 1 and label * 2 < figure  # a small label, a blank row, then a figure more than twice as tall


def test_a_card_badge_is_a_white_tab_on_the_right_edge_with_the_word_down_it():
    recipe = {"pieces": [{"shape": collage.FULL, "photo": "#0", "map": {}, "visible": True}], "peek_photo": None}
    card = collage.apply_card(recipe, [], "", None, None, "GARMIN")
    badge = card["badge"]
    assert badge["x"] + badge["w"] == W and badge["y"] < collage.SAFE_TOP + 200 and badge["h"] > badge["w"] * 2
    tab, ink = collage._badge_layers(badge, 1.0)
    inside = tab[badge["y"]:badge["y"] + badge["h"], badge["x"]:]
    assert inside.min() == 1.0 and tab.sum() == inside.sum()  # the tab, and nothing else
    word = np.nonzero(ink.any(1))[0]
    assert word[-1] - word[0] > ink.any(0).sum() * 3  # the word runs down, not across
    assert "badge" not in collage.apply_card(card, [], "", None, None, "")  # "" takes it away
