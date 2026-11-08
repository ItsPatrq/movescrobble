"""The editor's server: what it does with recipes, and what it refuses."""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pytest
from collage_fixtures import run_folder
from PIL import Image

from movescrobble import cli, collage, collage_editor


@pytest.fixture(scope="module")
def shared_run(tmp_path_factory):
    return run_folder(tmp_path_factory.mktemp("run"), count=3)


@pytest.fixture
def run(shared_run, tmp_path):
    import shutil

    copy = tmp_path / "run"
    shutil.copytree(shared_run, copy)
    return copy


@pytest.fixture
def server(run):
    srv, address = collage_editor.serve(run, open_browser=False)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    url = urlparse(address)
    yield srv, url.port, url.query.split("=", 1)[1], run
    srv.shutdown()
    srv.server_close()


def ask(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(method, path, json.dumps(body) if body is not None else None, headers or {})
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data, res


def ok(port, token, method, path, body=None):
    status, data, _ = ask(port, method, path, body, {"X-Token": token})
    assert status == 200, data[:200]
    return json.loads(data)


def first(port, token):
    state = ok(port, token, "GET", "/api/state")
    return ok(port, token, "GET", f"/api/recipe?id={state['versions'][0]['id']}")["recipe"]


def test_it_answers_only_with_the_token(server):
    _, port, token, _ = server
    assert ask(port, "GET", "/api/state")[0] == 403
    assert ask(port, "GET", "/", headers={"X-Token": "wrong"})[0] == 403
    assert ask(port, "GET", f"/?t={token}")[0] == 200


def test_it_listens_on_this_machine_only(server):
    srv, *_ = server
    assert srv.server_address[0] == "127.0.0.1"


def test_it_refuses_another_host_or_origin(server):
    _, port, token, _ = server
    assert ask(port, "GET", "/api/state", headers={"X-Token": token, "Host": "evil.example"})[0] == 403
    assert ask(port, "POST", "/api/save", {}, {"X-Token": token, "Origin": "https://evil.example"})[0] == 403


def test_a_recipe_cannot_reach_other_files(server):
    _, port, token, _ = server
    recipe = first(port, token)
    recipe["pieces"][0]["photo"] = "/etc/hosts"
    status, data, _ = ask(port, "POST", "/api/preview", {"recipe": recipe}, {"X-Token": token})
    assert status == 400 and b"not part of this run" in data
    status, *_ = ask(port, "GET", "/api/photo?key=/etc/hosts", headers={"X-Token": token})
    assert status == 400
    status, *_ = ask(port, "GET", "/out?name=../../versions.json", headers={"X-Token": token})
    assert status == 400


@pytest.mark.parametrize(
    "spoil",
    [
        lambda r: r.update(format=99),
        lambda r: r.update(id="../../evil"),
        lambda r: r["pieces"][0].update(shape={"type": "script"}),
        lambda r: r["pieces"][0]["map"].update(k=-1),
        lambda r: r["pieces"][0]["map"].update(cx="x"),
        lambda r: r["text"][0].update(font="/etc/hosts"),
        lambda r: r["text"][0].update(size=100000),
        lambda r: r["reel"].update(seconds=9999),
        lambda r: r["route"].update(width=1e9),
    ],
)
def test_a_spoiled_recipe_is_refused_and_not_saved(server, spoil):
    _, port, token, run = server
    recipe = first(port, token)
    spoil(recipe)
    status, data, _ = ask(port, "POST", "/api/save", {"recipe": recipe}, {"X-Token": token})
    assert status == 400 and json.loads(data)["error"]
    assert not list((run / "edited").glob("*.json")) if (run / "edited").exists() else True


def test_a_preview_is_a_picture_and_says_where_the_type_is(server):
    _, port, token, _ = server
    recipe = first(port, token)
    status, data, res = ask(port, "POST", "/api/preview", {"recipe": recipe, "scale": 0.34}, {"X-Token": token})
    assert status == 200 and res.getheader("Content-Type") == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == collage._size(0.34)
    info = json.loads(res.getheader("X-Info"))
    assert len(info["boxes"]) == len(recipe["text"]) and 0.03 < info["share"] < 0.2


def test_previews_are_fast_enough_to_drag(server):
    _, port, token, _ = server
    recipe = first(port, token)
    ask(port, "POST", "/api/preview", {"recipe": recipe, "scale": 0.34}, {"X-Token": token})
    started = time.perf_counter()
    recipe["pieces"][-1]["map"]["fx"] += 5
    ask(port, "POST", "/api/preview", {"recipe": recipe, "scale": 0.34}, {"X-Token": token})
    assert time.perf_counter() - started < 1.0


def test_saving_keeps_edits_beside_the_original_and_revert_brings_it_back(server):
    _, port, token, run = server
    recipe = first(port, token)
    was = json.loads(json.dumps(recipe))
    recipe["pieces"][-1]["map"]["fx"] += 33
    ok(port, token, "POST", "/api/save", {"recipe": recipe})
    assert (
        json.loads((run / "edited" / f"{recipe['id']}.json").read_text())["pieces"][-1]["map"]["fx"]
        == recipe["pieces"][-1]["map"]["fx"]
    )
    assert json.loads((run / "recipes" / f"{recipe['id']}.json").read_text()) == was
    assert first(port, token)["pieces"][-1]["map"]["fx"] == recipe["pieces"][-1]["map"]["fx"]
    reverted = ok(port, token, "POST", "/api/revert", {"id": recipe["id"]})["recipe"]
    assert reverted == was and not (run / "edited" / f"{recipe['id']}.json").exists()


def test_edits_survive_a_restart(run):
    editor = collage_editor.Editor(run)
    recipe = editor.recipe(next(iter(editor.originals)))
    changed = json.loads(json.dumps(recipe))
    changed["grade"]["contrast"] = 1.4
    editor.save(changed)
    assert collage_editor.Editor(run).recipe(recipe["id"])["grade"]["contrast"] == 1.4


def test_a_new_version_keeps_photos_type_and_route(server):
    _, port, token, run = server
    source = first(port, token)
    recipe = ok(port, token, "POST", "/api/draw", {"source": source["id"], "layout": "ripple", "look": "natural", "seed": 5})[
        "recipe"
    ]
    assert recipe["id"] == "n01" and recipe["layout"] == "ripple"
    assert recipe["photos"] == source["photos"] and recipe["peek_photo"] == source["peek_photo"]
    assert recipe["route"] and [line for b in recipe["text"] for line in b["lines"]] == [
        line for b in source["text"] for line in b["lines"]
    ]
    assert (run / "edited" / "n01.json").exists()
    assert "n01" in [v["id"] for v in ok(port, token, "GET", "/api/state")["versions"]]


def test_a_look_changes_the_grade_and_nothing_else(server):
    _, port, token, _ = server
    recipe = first(port, token)
    changed = ok(port, token, "POST", "/api/look", {"recipe": recipe, "look": "noir"})["recipe"]
    assert changed["look"] == "noir" and changed["grade"]["sat"] == pytest.approx(0.0) and changed["enhance"] is False
    assert changed["pieces"] == recipe["pieces"] and changed["text"] == recipe["text"]
    sunset = ok(port, token, "POST", "/api/look", {"recipe": recipe, "look": "sunset"})["recipe"]
    assert sunset["unify"]["warm"] > 0


def test_framing_again_shows_both_legs_or_one(server):
    _, port, token, _ = server
    recipe = first(port, token)
    at = next(i for i, p in enumerate(recipe["pieces"]) if p["body"])
    both = ok(port, token, "POST", "/api/frame", {"recipe": recipe, "index": at, "mode": "both"})["recipe"]["pieces"][at]["map"]
    right = ok(port, token, "POST", "/api/frame", {"recipe": recipe, "index": at, "mode": "right"})["recipe"]["pieces"][at]["map"]
    left = ok(port, token, "POST", "/api/frame", {"recipe": recipe, "index": at, "mode": "left"})["recipe"]["pieces"][at]["map"]
    assert left["cx"] < both["cx"] < right["cx"] or left["cx"] < right["cx"]


def test_a_calm_place_stays_in_the_safe_area(server):
    _, port, token, _ = server
    recipe = first(port, token)
    placed = ok(port, token, "POST", "/api/place", {"recipe": recipe, "what": "route"})["recipe"]["route"]
    assert collage.SAFE_TOP <= placed["y"] and placed["y"] + placed["h"] <= collage.H - collage.SAFE_BOTTOM + 40
    placed = ok(port, token, "POST", "/api/place", {"recipe": recipe, "what": "text"})["recipe"]["text"][0]
    assert collage.SAFE_TOP <= placed["y"]


def finished(port, token, job):
    for _ in range(600):
        state = ok(port, token, "GET", f"/api/job?id={job}")
        if state["state"] != "running":
            break
        time.sleep(0.05)
    assert state["state"] == "done", state
    return state


def test_a_still_export_is_full_size_without_exif(server):
    _, port, token, run = server
    recipe = first(port, token)
    job = ok(port, token, "POST", "/api/export", {"recipe": recipe, "kind": "still"})["job"]
    state = finished(port, token, job)
    path = run / "exported" / state["file"]
    image = Image.open(path)
    assert image.size == (collage.W, collage.H) and not image.getexif()
    status, data, _ = ask(port, "GET", f"/out?name={state['file']}", headers={"X-Token": token})
    assert status == 200 and data == path.read_bytes()


@pytest.mark.ffmpeg
def test_a_reel_export_is_a_reel(server):
    _, port, token, run = server
    recipe = first(port, token)
    recipe["reel"].update(seconds=1.0, reveal=0.5)
    job = ok(port, token, "POST", "/api/export", {"recipe": recipe, "kind": "reel"})["job"]
    state = finished(port, token, job)
    assert (run / "exported" / state["file"]).stat().st_size > 1000


def test_a_reel_frame_follows_the_reveal(server):
    _, port, token, _ = server
    recipe = first(port, token)
    frames = [ask(port, "POST", "/api/reel-frame", {"recipe": recipe, "progress": p}, {"X-Token": token})[1] for p in (0.0, 1.1)]
    assert frames[0] != frames[1]


def test_no_coordinates_anywhere_the_editor_writes(server):
    _, port, token, run = server
    recipe = first(port, token)
    ok(port, token, "POST", "/api/save", {"recipe": recipe})
    for path in [*run.glob("*.json"), *run.glob("recipes/*.json"), *run.glob("edited/*.json")]:
        words = path.read_text()
        assert '"lat"' not in words and not re.search(r"(?<![\d.])(52|21)\.0\d", words)  # the fixture's latitude and longitude


def test_a_folder_without_recipes_says_so(tmp_path):
    with pytest.raises(SystemExit, match="no recipes"):
        collage_editor.Editor(tmp_path)


def test_newest_run_finds_the_latest_folder(shared_run, tmp_path):
    (tmp_path / "older" / "recipes").mkdir(parents=True)
    (tmp_path / "older" / "versions.json").write_text("{}")
    assert collage_editor.newest_run(tmp_path) == tmp_path / "older"
    assert collage_editor.newest_run(tmp_path / "older" / "recipes") is None


def test_the_command_starts_the_editor(monkeypatch, tmp_path, run, capsys):
    started = {}

    class Fake:
        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            started["stopped"] = True

    def fake_serve(folder, port, browser):
        started.update(folder=folder, port=port, browser=browser)
        return Fake(), "http://127.0.0.1:1/?t=x"

    monkeypatch.setattr(collage_editor, "serve", fake_serve)
    monkeypatch.setattr("sys.argv", ["movescrobble", "collage-edit", str(run), "--port", "7777", "--no-browser"])
    cli.main()
    assert started == {"folder": Path(run), "port": 7777, "browser": False, "stopped": True}
    assert "127.0.0.1" in capsys.readouterr().out


# --- photos from disk


def send_photo(port, token, name, data):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("POST", f"/api/photo?name={name}", data, {"X-Token": token, "Content-Type": "application/octet-stream"})
    res = conn.getresponse()
    body = json.loads(res.read())
    conn.close()
    return res.status, body


def png(colour=(40, 160, 90), size=(600, 800)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def test_an_added_photo_joins_the_run_and_can_be_used(server):
    _, port, token, run = server
    status, body = send_photo(port, token, "My%20park.png", png())
    assert status == 200, body
    photo = body["photo"]
    assert photo["name"].startswith("My-park-") and photo["aspect"] == pytest.approx(0.75)
    assert Path(photo["key"]).parent == run / "added" and Path(photo["key"]).exists()
    assert photo["key"] in [p["key"] for p in ok(port, token, "GET", "/api/state")["photos"]]
    recipe = first(port, token)
    recipe["pieces"][0]["photo"] = photo["key"]
    ok(port, token, "POST", "/api/save", {"recipe": recipe})
    status, data, _ = ask(port, "POST", "/api/preview", {"recipe": recipe, "scale": 0.2}, {"X-Token": token})
    assert status == 200 and Image.open(io.BytesIO(data)).size == (216, 384)
    again = send_photo(port, token, "My%20park.png", png())[1]["photo"]  # the same photo twice is one photo
    assert again["key"] == photo["key"] and len(list((run / "added").iterdir())) == 1


def test_added_photos_stay_after_the_editor_restarts(server):
    _, port, token, run = server
    key = send_photo(port, token, "kept.png", png())[1]["photo"]["key"]
    assert key in collage_editor.Editor(run).allowed


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [("notes.txt", b"hello", "add JPEG"), ("fake.jpg", b"not a photo at all", "not a photo"), ("empty.png", b"", "up to")],
)
def test_what_is_not_a_photo_is_refused_and_not_kept(server, name, data, message):
    _, port, token, run = server
    status, body = send_photo(port, token, name, data)
    assert status == 400 and message in body["error"]
    assert not (run / "added").exists() or not list((run / "added").iterdir())


def test_a_photo_too_large_is_refused(server, monkeypatch):
    _, port, token, _ = server
    monkeypatch.setattr(collage_editor, "MAX_PHOTO", 1000)
    status, body = send_photo(port, token, "big.png", png())
    assert status == 413 and body["error"] == "too large"


def test_adding_a_photo_needs_the_token(server):
    _, port, _, run = server
    status, _, _ = ask(port, "POST", "/api/photo?name=a.png", None, {})
    assert status == 403 and not (run / "added").exists()


def test_runs_are_listed_newest_first_and_start_stops(shared_run, tmp_path):
    import os
    import shutil

    for name, age in (("old", 100), ("new", 0)):
        shutil.copytree(shared_run, tmp_path / name)
        os.utime(tmp_path / name, (time.time() - age, time.time() - age))
    assert collage_editor.runs(tmp_path) == [tmp_path / "new", tmp_path / "old"]
    assert collage_editor.runs(tmp_path / "nothing") == []
    stop, address = collage_editor.start(tmp_path / "new", open_browser=False)
    url = urlparse(address)
    status, _, _ = ask(url.port, "GET", "/api/state", headers={"X-Token": url.query.split("=", 1)[1]})
    stop()
    assert status == 200
    (tmp_path / "empty" / "recipes").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="no recipes"):
        collage_editor.start(tmp_path / "empty", open_browser=False)


# --- the card


def test_a_card_is_saved_beside_the_version_and_exported(run, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["movescrobble", "collage-card", "v01", "--folder", str(run), "--title", "Evening Run",
                                     "--badge", "GARMIN", "--no-reel"])
    cli.main()
    card = json.loads((run / "edited" / "card-v01.json").read_text())
    title, stats = card["text"]
    assert title["lines"] == ["Evening Run"] and stats["style"] == "stats"
    assert stats["lines"][0].startswith("Distance|8.04|km") and card["route"]["width"] == collage.CARD_ROUTE_WIDTH
    assert Image.open(run / "exported" / "card-v01.jpg").size == (collage.W, collage.H)
    with_badge, without = (np.asarray(Image.open(run / "exported" / f"{n}.jpg")) for n in ("card-v01", "card-v01-no-badge"))
    x, y, w, h = collage.BADGE_BOX
    assert with_badge[y + 5 : y + 20, x + 5 : x + 20].min() > 240  # the white tab
    assert without[y + 5 : y + 20, x + 5 : x + 20].min() < 240 and not (run / "edited" / "card-v01-no-badge.json").exists()
    assert not (run / "exported" / "card-v01.mp4").exists()
    assert "card-v01.jpg" in capsys.readouterr().out
    assert json.loads((run / "recipes" / "v01.json").read_text())["text"] != card["text"]  # the version itself stays


def test_a_card_for_a_version_without_a_route_borrows_the_runs(run):
    recipe = json.loads((run / "recipes" / "v01.json").read_text())
    (run / "edited").mkdir(exist_ok=True)
    (run / "edited" / "v01.json").write_text(json.dumps({**recipe, "route": None}))  # an edit took the route away
    card = json.loads(collage.make_card(run, "v01", "", reel=False)[0].read_text())
    assert card["route"] and [b.get("style") for b in card["text"]] == ["stats"] and "badge" not in card
    assert json.loads(collage.make_card(run, "v01", reel=False, badge="GARMIN")[0].read_text())["badge"]["text"] == "GARMIN"


def test_the_editor_makes_a_card_of_what_is_on_screen(server):
    _, port, token, _ = server
    recipe = first(port, token)
    carded = ok(port, token, "POST", "/api/card", {"recipe": recipe})["recipe"]
    assert carded["text"][-1]["style"] == "stats" and carded["pieces"] == recipe["pieces"]
    assert "badge" not in carded and len(carded["text"]) == 1  # no title or badge unless you add one
    carded["badge"] = collage.apply_card({**carded}, [], badge="GARMIN")["badge"]
    carded = ok(port, token, "POST", "/api/card", {"recipe": carded})["recipe"]
    assert carded["badge"]["text"] == "GARMIN"  # a badge it has stays
    ok(port, token, "POST", "/api/save", {"recipe": carded})  # what the card makes, the editor accepts
    status, _, _ = ask(port, "POST", "/api/preview", {"recipe": carded, "scale": 0.2}, {"X-Token": token})
    assert status == 200
    for spoil in ({"text": "x" * 40}, {"w": 5000}, {"tab": [1, 2]}, {"font": "/etc/passwd"}):
        bad = json.loads(json.dumps(carded))
        bad["badge"].update(spoil)
        assert ask(port, "POST", "/api/save", {"recipe": bad}, {"X-Token": token})[0] == 400
    carded["text"][-1]["style"] = "fancy"
    status, data, _ = ask(port, "POST", "/api/save", {"recipe": carded}, {"X-Token": token})
    assert status == 400 and b"style" in data
