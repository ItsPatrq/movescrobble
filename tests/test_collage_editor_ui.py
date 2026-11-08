"""The editor in a real Chrome: each control does what it says, and the page looks as it should.

Screenshots of the whole page depend on the system's fonts, so they live per platform and a platform without them skips that
comparison. The picture in the page comes from the server, and is compared everywhere.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest
from collage_fixtures import run_folder
from PIL import Image
from test_collage_vrt import GOLDEN, check, differences

from movescrobble import collage, collage_editor

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def chrome():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except playwright.Error:
            pytest.skip("Chrome not installed")
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def shared_run(tmp_path_factory):
    original = collage.FONTS
    collage.FONTS = []  # Pillow's own font, so pictures match on every machine
    yield run_folder(tmp_path_factory.mktemp("uirun"), count=4, seed=21, layouts=("stack", "orbit", "arcade", "ripple"))
    collage.FONTS = original


@pytest.fixture
def run(shared_run, tmp_path):
    copy = tmp_path / "run"
    shutil.copytree(shared_run, copy)
    return copy


class Page:
    """The editor open at its first version."""

    def __init__(self, page, run: Path) -> None:
        self.page, self.run = page, run

    def id(self, name: str):
        return self.page.get_by_test_id(name)

    def idle(self) -> None:
        self.page.wait_for_function("!window.__editor.S.dirty")
        self.page.evaluate("window.__editor.idle()")

    def recipe(self) -> dict:
        return self.page.evaluate("window.__editor.S.recipe")

    def piece_index(self, where) -> int:
        return next(i for i, p in enumerate(self.recipe()["pieces"]) if where(p))

    def saved(self) -> dict:
        self.page.wait_for_function("!window.__editor.S.dirty")
        self.page.evaluate("window.__editor.saved()")
        recipe = self.recipe()
        return json.loads((self.run / "edited" / f"{recipe['id']}.json").read_text())

    def saved_when(self, holds, seconds: float = 15) -> dict:
        """The saved recipe, once it shows a change that reaches the file only after the server answers."""
        path = self.run / "edited" / f"{self.recipe()['id']}.json"
        for _ in range(int(seconds / 0.1)):
            if path.exists() and holds(recipe := json.loads(path.read_text())):
                return recipe
            self.page.wait_for_timeout(100)
        raise AssertionError(f"not saved: {path}")

    def corner(self, name: str) -> tuple[float, float]:
        """Near the top left of an overlay element, read once the overlay has stopped changing."""
        self.idle()
        r = self.page.evaluate(
            "n => { const b = document.querySelector(`[data-testid=${n}]`).getBoundingClientRect(); return [b.x, b.y]; }", name
        )
        return r[0] + 4, r[1] + 4

    def centre(self, name: str) -> tuple[float, float]:
        box = self.id(name).bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def frame_to_screen(self, x: float, y: float) -> tuple[float, float]:
        box = self.id("stage").bounding_box()
        return box["x"] + x / 1080 * box["width"], box["y"] + y / 1920 * box["height"]

    def drag(self, start: tuple[float, float], by: tuple[float, float], steps: int = 6, modifiers: str | None = None) -> None:
        mouse = self.page.mouse
        mouse.move(*start)
        mouse.down()
        for i in range(1, steps + 1):
            mouse.move(start[0] + by[0] * i / steps, start[1] + by[1] * i / steps)
        mouse.up()

    def slide(self, name: str, value: float) -> None:
        self.id(name).evaluate(
            """(n, v) => {
                n.value = v;
                n.dispatchEvent(new Event('input', {bubbles: true}));
                n.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            value,
        )

    def picture(self) -> Image.Image:
        self.idle()
        data = self.id("view").evaluate(
            """async (n) => {
                const b = await (await fetch(n.src)).blob();
                return Array.from(new Uint8Array(await b.arrayBuffer()));
            }"""
        )
        return Image.open(io.BytesIO(bytes(data)))

    def click_piece(self, i: int) -> None:
        self.id("tab-pieces").click()
        self.id(f"layer-{i}").click()


@pytest.fixture
def ed(chrome, run):
    server, address = collage_editor.serve(run, open_browser=False)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    context = chrome.new_context(viewport={"width": 1400, "height": 900})
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on("console", lambda m: problems.append(m.text) if m.type == "error" else None)
    page.goto(address)
    page.wait_for_function("window.__editor && window.__editor.S.recipe && window.__editor.S.boxes")
    page.evaluate("window.__editor.idle()")
    helper = Page(page, run)
    yield helper
    context.close()
    server.shutdown()
    server.server_close()
    assert not problems, problems


def body_of(ed: Page) -> int:
    return ed.piece_index(lambda p: p["body"])


def test_it_opens_on_a_picture_with_every_version_listed(ed):
    versions = ed.id("versions").locator("button")
    assert versions.count() == 4
    assert ed.id("status").inner_text() == "Saved"
    assert ed.id("view").evaluate("n => n.naturalWidth") > 300


def test_dragging_a_picture_pans_only_its_photo_and_saves(ed):
    ed.id("stay").uncheck()
    at = ed.piece_index(lambda p: not p["body"] and p["shape"]["type"] != "full")
    ed.click_piece(at)
    before = ed.recipe()
    box = ed.id(f"piece-{at}").bounding_box()
    start = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    assert ed.page.evaluate("([x, y]) => document.elementFromPoint(x, y).dataset.role", list(start)) in (
        "piece",
        "text",
        "route",
        "handle",
        None,
    )
    ed.drag(start, (30, 12))
    after = ed.recipe()
    moved = [i for i in range(len(before["pieces"])) if before["pieces"][i]["map"] != after["pieces"][i]["map"]]
    assert len(moved) == 1
    i = moved[0]
    assert after["pieces"][i]["map"]["fx"] > before["pieces"][i]["map"]["fx"]
    assert after["pieces"][i]["shape"] == before["pieces"][i]["shape"]
    assert ed.saved()["pieces"][i]["map"] == after["pieces"][i]["map"]


def test_the_photo_cannot_slide_off_its_shape_unless_you_say_so(ed):
    at = body_of(ed)
    ed.click_piece(at)
    box = ed.id(f"piece-{at}").bounding_box()
    centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    ed.drag(centre, (900, 0))
    m = ed.recipe()["pieces"][at]["map"]
    photo_left = m["cx"] + m["k"] * (0 - m["fx"])
    assert photo_left >= -0.02


def test_the_wheel_zooms_about_the_pointer_and_shift_wheel_turns(ed):
    at = body_of(ed)
    ed.click_piece(at)
    box = ed.id(f"piece-{at}").bounding_box()
    centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    ed.page.mouse.move(*centre)
    k = ed.recipe()["pieces"][at]["map"]["k"]
    ed.page.mouse.wheel(0, -200)
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{at}].map.k < {k}")
    ed.id("stay").uncheck()
    ed.page.mouse.move(*centre)
    ed.page.keyboard.down("Shift")
    ed.page.mouse.wheel(0, 100)
    ed.page.keyboard.up("Shift")
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{at}].map.rot !== 0")


def test_undo_and_redo_walk_through_changes(ed):
    at = body_of(ed)
    ed.click_piece(at)
    was = ed.recipe()["pieces"][at]["adjust"]["exposure"]
    ed.slide("exposure", 0.5)
    assert ed.recipe()["pieces"][at]["adjust"]["exposure"] == 0.5
    ed.id("undo").click()
    assert ed.recipe()["pieces"][at]["adjust"]["exposure"] == was
    ed.id("redo").click()
    assert ed.recipe()["pieces"][at]["adjust"]["exposure"] == 0.5
    ed.page.keyboard.press("Control+z")
    assert ed.recipe()["pieces"][at]["adjust"]["exposure"] == was


def test_a_seam_moves_both_pictures_that_share_it(ed):
    ed.id("version-v01").click()
    ed.page.wait_for_function("window.__editor.S.recipe.id === 'v01'")
    at = ed.piece_index(lambda p: p["shape"]["type"] == "poly" and not p["body"] and p["shape"]["points"][0][1] < 0)
    ed.id("tool-shape").click()
    ed.click_piece(at)
    before = ed.recipe()
    edge = ed.page.locator('[data-testid="handle-edge-2"]')  # the bottom edge of a top row
    box = edge.bounding_box()
    ed.drag((box["x"] + box["width"] / 2, box["y"] + box["height"] / 2), (0, 30))
    after = ed.recipe()
    changed = [i for i, p in enumerate(after["pieces"]) if p["shape"] != before["pieces"][i]["shape"]]
    assert at in changed and len(changed) >= 2


def test_seams_can_move_alone(ed):
    at = ed.piece_index(lambda p: p["shape"]["type"] == "poly" and not p["body"] and p["shape"]["points"][0][1] < 0)
    ed.id("tool-shape").click()
    ed.id("link-seams").uncheck()
    ed.click_piece(at)
    before = ed.recipe()
    box = ed.page.locator('[data-testid="handle-edge-2"]').bounding_box()
    ed.drag((box["x"] + box["width"] / 2, box["y"] + box["height"] / 2), (0, 30))
    after = ed.recipe()
    assert [i for i, p in enumerate(after["pieces"]) if p["shape"] != before["pieces"][i]["shape"]] == [at]


def test_dragging_a_shape_takes_its_picture_along_unless_told_not_to(ed):
    at = ed.piece_index(lambda p: p["body"])
    ed.id("tool-shape").click()
    ed.click_piece(at)
    before = ed.recipe()["pieces"][at]
    box = ed.id(f"piece-{at}").bounding_box()
    centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    ed.drag(centre, (20, 0))
    after = ed.recipe()["pieces"][at]
    assert after["shape"] != before["shape"] and after["map"]["fx"] > before["map"]["fx"]
    ed.id("follow").uncheck()
    again = ed.recipe()["pieces"][at]
    ed.drag(centre, (20, 0))
    assert ed.recipe()["pieces"][at]["map"]["fx"] == again["map"]["fx"]


def test_type_and_route_drag_and_snap(ed):
    x0 = ed.recipe()["text"][0]["x"]
    ed.drag(ed.corner("text-box-0"), (-40, 30))
    assert ed.recipe()["text"][0]["x"] != x0
    assert ed.id("tab-type").get_attribute("class") == "on"
    r0 = ed.recipe()["route"]["x"]
    ed.drag(ed.corner("route-box"), (25, 0))
    assert ed.recipe()["route"]["x"] != r0
    assert ed.id("tab-route").get_attribute("class") == "on"


def test_route_controls(ed):
    ed.id("tab-route").click()
    ed.slide("route-width", 16)
    ed.slide("route-size", 300)
    r = ed.recipe()["route"]
    assert r["width"] == 16 and max(r["w"], r["h"]) == 300
    ed.id("route-show").uncheck()
    assert ed.recipe()["route"]["visible"] is False
    assert ed.id("route-box").count() == 0
    ed.id("route-show").check()
    ed.id("place-route").click()
    ed.page.wait_for_function("window.__editor.S.undo.length >= 4")


def test_type_controls(ed):
    ed.id("tab-type").click()
    ed.id("add-block").click()
    assert len(ed.recipe()["text"]) >= 2
    area = ed.id("text-lines")
    area.fill("Hello\nWorld")
    at = len(ed.recipe()["text"]) - 1
    assert ed.recipe()["text"][at]["lines"] == ["Hello", "World"]
    ed.slide("text-size", 80)
    assert ed.recipe()["text"][at]["size"] == 80
    ed.id("text-rotate").check()
    assert ed.recipe()["text"][at]["rotate"] == 90
    ed.id("stat-distance").click()
    assert ed.recipe()["text"][at]["lines"][-1] == "8.04 km"
    ed.id("text-delete").click()
    assert len(ed.recipe()["text"]) == at


def test_layers_hide_reorder_duplicate_add_and_delete(ed):
    count = len(ed.recipe()["pieces"])
    top = count - 1
    ed.id(f"eye-{top}").click()
    assert ed.recipe()["pieces"][top]["visible"] is False
    ed.id(f"eye-{top}").click()
    name = ed.recipe()["pieces"][top]["name"]
    ed.id(f"down-{top}").click()
    assert ed.recipe()["pieces"][top - 1]["name"] == name
    ed.id("add-disc").click()
    assert ed.recipe()["pieces"][-1]["shape"]["type"] == "disc" and len(ed.recipe()["pieces"]) == count + 1
    ed.page.wait_for_function("Math.abs(window.__editor.S.recipe.pieces.at(-1).map.fy - 900) < 3")  # framed for itself
    ed.idle()
    assert ed.id("status").inner_text() == "Saved"
    ed.id("delete").click()
    assert len(ed.recipe()["pieces"]) == count
    assert sorted(ed.recipe()["reel"]["order"]) == list(range(count))
    ed.id(f"layer-{top}").click()
    ed.id("duplicate").click()
    assert len(ed.recipe()["pieces"]) == count + 1
    assert sorted(ed.recipe()["reel"]["order"]) == list(range(count + 1))


def test_photo_can_be_changed_and_swapped(ed):
    at = ed.piece_index(lambda p: not p["body"] and p["shape"]["type"] != "full")
    ed.click_piece(at)
    was = ed.recipe()["pieces"][at]["photo"]
    choices = [p["key"] for p in ed.page.evaluate("window.__editor.S.info.photos")]
    n = next(i for i, key in enumerate(choices) if key != was)
    ed.id(f"photo-{n}").click()
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{at}].photo !== {json.dumps(was)}")
    ed.id("swap").select_option(str(body_of(ed)))
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{body_of(ed)}].photo === {json.dumps(choices[n])}")


def test_the_leg_photo_moved_to_another_piece_takes_its_name_and_share_along(ed):
    legs, at = body_of(ed), ed.piece_index(lambda p: not p["body"] and p["shape"]["type"] != "full")
    peek = ed.recipe()["peek_photo"]
    ed.click_piece(at)
    ed.id("swap").select_option(str(legs))
    ed.page.wait_for_function(f"document.querySelector('[data-testid=layer-{at}] span').textContent === 'legs'")
    ed.saved_when(lambda r: r["pieces"][at]["photo"] == peek)
    ed.idle()
    assert not ed.id(f"layer-{legs}").inner_text().strip().startswith("legs")
    painted = collage_editor.Editor(ed.run).preview(ed.recipe(), 0.25)[1]["share"]
    assert painted > 0.03 and ed.id("share").inner_text() == f"legs {painted * 100:.0f}% of frame"


def test_leg_framing_buttons_and_settings(ed):
    at = body_of(ed)
    ed.click_piece(at)
    was = ed.recipe()["pieces"][at]["map"]["cx"]
    ed.id("frame-right").click()
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{at}].map.cx !== {was}")
    right = ed.recipe()["pieces"][at]["map"]["cx"]
    ed.id("frame-left").click()
    ed.page.wait_for_function(f"window.__editor.S.recipe.pieces[{at}].map.cx < {right}")
    ed.slide("match", 0.2)
    ed.slide("muscle", 1.0)
    assert ed.recipe()["match"] == 0.2 and ed.recipe()["muscle"] == 1.0


def test_colour_controls_change_the_picture(ed):
    before = ed.picture()
    ed.id("tab-colour").click()
    ed.slide("grade-sat", 0.1)
    assert ed.recipe()["grade"]["sat"] == 0.1
    after = ed.picture()
    assert list(before.convert("RGB").resize((8, 8)).getdata()) != list(after.convert("RGB").resize((8, 8)).getdata())
    ed.id("look-noir").click()
    ed.page.wait_for_function("window.__editor.S.recipe.look === 'noir'")


def test_reel_controls_and_scrubbing(ed):
    ed.id("tab-reel").click()
    ed.slide("reel-seconds", 9)
    ed.slide("reel-reveal", 4)
    assert ed.recipe()["reel"]["seconds"] == 9 and ed.recipe()["reel"]["reveal"] == 4
    order = list(ed.recipe()["reel"]["order"])
    ed.id("order-down-1").click()
    assert ed.recipe()["reel"]["order"] != order
    src = ed.id("view").get_attribute("src")
    ed.slide("reel-scrub", 0.1)
    ed.page.wait_for_function("s => document.querySelector('#view').src !== s", arg=src)


def test_new_version_appears_and_opens(ed):
    ed.page.evaluate("window.__editor.S.nextSeed = 5")
    ed.id("new-layout").select_option("slants")
    ed.id("draw").click()
    ed.page.wait_for_function("window.__editor.S.recipe.id === 'n01'")
    assert ed.recipe()["layout"] == "slants"
    assert ed.id("version-n01").count() == 1


def test_a_late_answer_does_not_land_on_another_version(ed):
    def slow(route):
        ed.page.wait_for_timeout(500)
        route.continue_()

    ed.click_piece(0)
    ed.page.route("**/api/frame*", slow)
    ed.id("fit").click()
    target = ed.page.evaluate("window.__editor.S.info.versions[2].id")
    ed.id(f"version-{target}").click()
    ed.page.wait_for_function(f"window.__editor.S.recipe.id === {json.dumps(target)}")
    ed.page.wait_for_timeout(800)
    assert ed.recipe()["id"] == target


def test_revert_goes_back_to_the_drawn_version(ed):
    was = ed.recipe()
    ed.id("tab-colour").click()
    ed.slide("grade-contrast", 1.5)
    ed.saved()
    ed.id("revert").click()
    ed.page.wait_for_function("window.__editor.S.undo.length === 0 && window.__editor.S.recipe.grade.contrast !== 1.5")
    assert ed.recipe() == was
    assert not (ed.run / "edited" / f"{was['id']}.json").exists()


def test_still_export_lists_a_file(ed):
    ed.id("tab-export").click()
    ed.id("export-still").click()
    ed.page.wait_for_function("window.__editor.S.info.exports.length === 1", timeout=30000)
    assert ed.id("exports").locator("a").count() == 1
    assert list((ed.run / "exported").glob("*.jpg"))


def test_keyboard(ed):
    ed.page.keyboard.press("2")
    assert ed.page.evaluate("window.__editor.S.tool") == "shape"
    ed.page.keyboard.press("1")
    ed.page.keyboard.press("Tab")
    assert ed.page.evaluate("window.__editor.S.sel") == 0
    ed.page.keyboard.press("h")
    assert ed.recipe()["pieces"][0]["visible"] is False
    ed.page.keyboard.press("h")
    fx = ed.recipe()["pieces"][0]["map"]["fx"]
    ed.id("stay").uncheck()
    ed.page.keyboard.press("Shift+ArrowLeft")
    assert ed.recipe()["pieces"][0]["map"]["fx"] == fx - 10


# --- photos from disk, the crop view, filters


def photo_file(tmp_path: Path) -> Path:
    path = tmp_path / "harbour.png"
    Image.new("RGB", (600, 800), (30, 140, 200)).save(path)
    return path


def test_a_photo_from_disk_goes_into_the_selected_piece(ed, tmp_path):
    i = ed.piece_index(lambda p: not p["body"])
    ed.click_piece(i)
    with ed.page.expect_file_chooser() as chooser:
        ed.id("add-photos").click()
    chooser.value.set_files(str(photo_file(tmp_path)))
    saved = ed.saved_when(lambda r: "harbour" in r["pieces"][i]["photo"])
    assert "harbour" in saved["pieces"][i]["photo"] and saved["pieces"][i]["photo"] in saved["photos"]
    assert len(list((ed.run / "added").iterdir())) == 1


def test_a_photo_with_nothing_selected_becomes_a_new_piece(ed, tmp_path):
    count = len(ed.recipe()["pieces"])
    ed.id("add-photo-input").set_input_files(str(photo_file(tmp_path)))
    saved = ed.saved_when(lambda r: len(r["pieces"]) == count + 1)
    assert len(saved["pieces"]) == count + 1 and "harbour" in saved["pieces"][-1]["photo"]
    assert count in saved["reel"]["order"]


def test_a_photo_dropped_on_a_picture_goes_into_it(ed):
    i = ed.piece_index(lambda p: not p["body"] and p["shape"]["type"] != "full")
    ed.page.evaluate(
        """async (i) => {
          const canvas = Object.assign(document.createElement("canvas"), { width: 60, height: 80 });
          const ctx = canvas.getContext("2d");
          ctx.fillStyle = "#d04030";
          ctx.fillRect(0, 0, 60, 80);
          const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
          const data = new DataTransfer();
          data.items.add(new File([blob], "dropped.png", { type: "image/png" }));
          const target = document.querySelector(`#over path[data-index="${i}"]`);
          target.dispatchEvent(new DragEvent("drop", { dataTransfer: data, bubbles: true, cancelable: true }));
        }""",
        i,
    )
    ed.saved_when(lambda r: "dropped" in r["pieces"][i]["photo"])


def test_the_crop_view_moves_and_zooms_the_photo(ed):
    ed.id("stay").uncheck()
    i = ed.piece_index(lambda p: not p["body"] and p["shape"]["type"] != "full")
    ed.click_piece(i)
    before = ed.recipe()["pieces"][i]["map"]
    box = ed.id("crop-window").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    ed.page.mouse.move(x, y)
    ed.page.mouse.down()
    ed.page.mouse.move(x + 20, y + 10, steps=4)
    ed.page.mouse.up()
    moved = ed.saved()["pieces"][i]["map"]
    assert moved["cx"] != before["cx"] and moved["cy"] != before["cy"] and moved["k"] == pytest.approx(before["k"])
    corner = ed.id("crop-zoom").bounding_box()
    cx, cy = corner["x"] + corner["width"] / 2, corner["y"] + corner["height"] / 2
    ed.page.mouse.move(cx, cy)
    ed.page.mouse.down()
    ed.page.mouse.move(cx - 15, cy - 15, steps=4)  # a smaller window: closer in
    ed.page.mouse.up()
    assert ed.saved()["pieces"][i]["map"]["k"] < moved["k"]


def test_a_filter_and_its_sliders_change_the_photo_and_can_go_on_every_piece(ed):
    i = ed.piece_index(lambda p: not p["body"])
    ed.click_piece(i)
    ed.id("filter-mono").click()
    ed.idle()
    assert ed.id("filter-mono").get_attribute("class") == "on"
    adjust = ed.saved()["pieces"][i]["adjust"]
    assert adjust["saturation"] == 0 and adjust["contrast"] == pytest.approx(1.15) and adjust["fade"] == 0
    ed.id("fade").fill("0.2")
    ed.id("fade").dispatch_event("change")
    ed.id("adjust-all").click()
    saved = ed.saved()
    assert all(p["adjust"]["fade"] == pytest.approx(0.2) and p["adjust"]["saturation"] == 0 for p in saved["pieces"])


# --- how it looks


TABS = ["pieces", "colour", "type", "route", "reel", "export"]


def test_the_picture_in_the_page_matches_the_final_picture(ed):
    recipe = ed.recipe()
    shown = ed.picture().resize((180, 320), Image.Resampling.BOX).convert("RGB")
    full = collage.render_still(recipe, collage.PhotoStore()).resize((180, 320), Image.Resampling.BOX)
    mean, patch, _ = differences(shown, full)  # the settled preview is 80% size: type's fine edges differ a little
    assert mean < 2.5 and patch < 40


@pytest.mark.parametrize("version", ["v01", "v02", "v03", "v04"])
def test_the_picture_of_each_version(ed, version, update_vrt):
    ed.id(f"version-{version}").click()
    ed.page.wait_for_function(f"window.__editor.S.recipe.id === {json.dumps(version)}")
    ed.page.evaluate("window.__editor.idle()")
    check(f"ui-picture-{version}", ed.picture(), update_vrt)


@pytest.mark.parametrize("tab", TABS)
def test_the_page_on_each_tab(ed, tab, update_vrt):
    folder = GOLDEN / f"ui-{sys.platform}"
    golden = folder / f"tab-{tab}.png"
    if not update_vrt and not golden.exists():
        pytest.skip(f"no page pictures for {sys.platform}")
    ed.page.wait_for_function("document.querySelectorAll('[data-testid^=version-] img[src]').length === 4", timeout=60000)
    ed.id(f"tab-{tab}").click()
    if tab == "pieces":
        ed.click_piece(body_of(ed))
    ed.page.evaluate("window.__editor.idle()")
    ed.page.wait_for_timeout(300)
    shot = Image.open(io.BytesIO(ed.page.screenshot()))
    check(f"tab-{tab}", shot, update_vrt, folder, (700, 450))
