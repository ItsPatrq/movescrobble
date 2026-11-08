"""The collage editor: a page in your browser, served from this machine only, that changes recipes and shows them painted.

Every preview comes from `collage.paint`, the same code that makes the final picture, so what the page shows is what comes out.
The server listens on 127.0.0.1, answers only requests carrying this run's token, and reads only the photos a run already used.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import re
import secrets
import shutil
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image

from . import collage

PAGE = Path(__file__).with_name("collage_editor.html")
MAX_BODY = 8 * 1024 * 1024
MAX_PHOTO = 40 * 1024 * 1024  # a photo straight off a phone
SHAPES = {"full", "poly", "disc", "ring", "arch", "union", "minus", "noise"}
ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PREVIEW_SCALES = (0.2, 1.0)  # what a preview may ask for
REEL_FRAME_SCALE = 0.33


class Refused(ValueError):
    """A request this editor will not act on; the page shows the message."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise Refused(f"{name} is not a number")
    return float(value)


def _check_shape(shape: dict, depth: int = 0) -> None:
    if not isinstance(shape, dict) or shape.get("type") not in SHAPES or depth > 3:
        raise Refused("unknown shape")
    kind = shape["type"]
    if kind == "poly":
        points = shape.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 200:
            raise Refused("a polygon needs 3 to 200 points")
        for x, y in points:
            _finite(x, "point"), _finite(y, "point")
    elif kind == "union":
        for part in shape.get("parts", []):
            _check_shape(part, depth + 1)
    elif kind == "minus":
        _check_shape(shape.get("base"), depth + 1)
        for cut in shape.get("cuts", []):
            _check_shape(cut, depth + 1)
    elif kind != "full":
        for name, value in shape.items():
            if name != "type":
                _finite(value, name)


class Editor:
    """One folder of a run: its recipes, your edits, and what you export."""

    def __init__(self, folder: Path, store: collage.PhotoStore | None = None) -> None:
        self.folder = folder
        self.originals: dict[str, dict] = {}
        self.stats: dict[str, str] = {}
        self.card_info: dict = {}
        listed = folder / "versions.json"
        if listed.exists():
            data = json.loads(listed.read_text())
            self.stats, self.card_info = data.get("stats", {}), data.get("card") or {}
        for path in sorted((folder / "recipes").glob("*.json")):
            self.originals[path.stem] = json.loads(path.read_text())
        self.edits_dir = folder / "edited"
        self.added_dir = folder / "added"
        self.export_dir = folder / "exported"
        self.edits: dict[str, dict] = {}
        for path in sorted(self.edits_dir.glob("*.json")):
            if ID.match(path.stem):
                self.edits[path.stem] = json.loads(path.read_text())
        if not self.originals and not self.edits:
            raise SystemExit(f"no recipes in {folder}: make a run with `movescrobble collage` first")
        self.allowed = self._photos_in_use()
        self.store = store or collage.PhotoStore()
        self.paint_lock = threading.Lock()
        self.add_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self._reel_memo: tuple[str, collage.Composed] | None = None

    def _photos_in_use(self) -> list[str]:
        keys: list[str] = []
        for recipe in [*self.originals.values(), *self.edits.values()]:
            for key in [*recipe["photos"], recipe.get("peek_photo"), *(p["photo"] for p in recipe["pieces"])]:
                if key and key not in keys:
                    keys.append(key)
        if self.added_dir.is_dir():  # photos you added here stay part of the run, used or not
            for path in sorted(self.added_dir.iterdir()):
                if path.suffix.lower() in collage.PHOTO_EXTS and not path.name.startswith(".") and str(path) not in keys:
                    keys.append(str(path))
        return keys

    # --- recipes

    def recipe(self, version: str) -> dict:
        found = self.edits.get(version) or self.originals.get(version)
        if found is None:
            raise Refused(f"no version {version}")
        return found

    def check(self, recipe: dict) -> dict:
        """A recipe from the page may only use this run's photos and this machine's fonts, and must make sense as numbers."""
        if not isinstance(recipe, dict) or recipe.get("format") != collage.RECIPE_FORMAT:
            raise Refused("not a recipe of this format")
        if not ID.match(str(recipe.get("id", ""))):
            raise Refused("bad version id")
        if not isinstance(recipe.get("pieces"), list) or not 1 <= len(recipe["pieces"]) <= 60:
            raise Refused("a recipe needs 1 to 60 pieces")
        allowed = set(self.allowed)
        for key in [*recipe.get("photos", []), *(k for k in [recipe.get("peek_photo")] if k)]:
            if key not in allowed:
                raise Refused("that photo is not part of this run")
        for piece in recipe["pieces"]:
            if piece.get("photo") not in allowed:
                raise Refused("that photo is not part of this run")
            _check_shape(piece.get("shape"))
            m = piece.get("map") or {}
            for name in ("cx", "cy", "k", "fx", "fy", "rot"):
                _finite(m.get(name), name)
            if not 1e-5 <= m["k"] <= 1:
                raise Refused("zoom out of range")
            for name in ("exposure", "contrast", "saturation", "warmth", "fade", "tint"):
                _finite((piece.get("adjust") or {}).get(name, 0.0), name)
            if piece.get("blend") not in ("normal", "screen"):
                raise Refused("unknown blend")
        fonts = set(collage.available_fonts()) | {collage.card_font(), ""}
        for block in recipe.get("text") or []:
            if block.get("font") not in fonts:
                raise Refused("unknown font")
            if not isinstance(block.get("lines"), list) or len(block["lines"]) > 12:
                raise Refused("too many lines of type")
            if any(not isinstance(line, str) or len(line) > 120 for line in block["lines"]):
                raise Refused("a line of type is too long")
            if block.get("style", "lines") not in ("lines", "stats"):
                raise Refused("unknown type style")
            if not 8 <= _finite(block.get("size"), "size") <= 400:
                raise Refused("type size out of range")
            block["size"] = round(block["size"])
            for name in ("x", "y", "rotate", "opacity", "edge", "pool", "spacing"):
                _finite(block.get(name, 1), name)
        route = recipe.get("route")
        if route:
            if not isinstance(route.get("points"), list) or not 2 <= len(route["points"]) <= 5000:
                raise Refused("a route needs points")
            for name in ("x", "y", "w", "h", "width", "opacity", "edge", "pool"):
                _finite(route.get(name, 1), name)
            if route["width"] > 60 or route["w"] > 4000 or route["h"] > 4000:
                raise Refused("route size out of range")
        reel = recipe.get("reel") or {}
        if not 1 <= _finite(reel.get("seconds"), "seconds") <= 30 or not 0 <= _finite(reel.get("reveal"), "reveal") <= 30:
            raise Refused("reel length out of range")
        return recipe

    def save(self, recipe: dict) -> None:
        recipe = self.check(recipe)
        self.edits_dir.mkdir(exist_ok=True)
        path = self.edits_dir / f"{recipe['id']}.json"
        scratch = path.with_suffix(".tmp")
        scratch.write_text(json.dumps(recipe, indent=1, ensure_ascii=False))
        scratch.replace(path)
        self.edits[recipe["id"]] = recipe

    def revert(self, version: str) -> dict:
        if not ID.match(version):
            raise Refused("bad version id")
        if version not in self.originals:
            raise Refused("this version has no original to go back to")
        self.edits.pop(version, None)
        (self.edits_dir / f"{version}.json").unlink(missing_ok=True)
        return self.originals[version]

    def add_photo(self, name: str, data: bytes) -> dict:
        """A photo from your disk, copied into the run's `added/` folder, so the run keeps working when the original moves."""
        ext = Path(name).suffix.lower()
        if ext not in collage.PHOTO_EXTS:
            raise Refused("add JPEG, PNG, HEIC, WebP or TIFF photos")
        if not data or len(data) > MAX_PHOTO:
            raise Refused(f"a photo of up to {MAX_PHOTO // 1024 // 1024} MB")
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-")[:40] or "photo"
        path = self.added_dir / f"{stem}-{hashlib.sha256(data).hexdigest()[:10]}{ext}"
        with self.add_lock:
            if str(path) not in self.allowed:
                self.added_dir.mkdir(exist_ok=True)
                incoming = path.with_name(f".incoming-{path.name}")  # same suffix: HEIC is read by its name
                incoming.write_bytes(data)
                try:
                    collage.load_photo(incoming, 64)
                except Exception as error:  # noqa: BLE001 - whatever the decoder says, the answer is the same
                    incoming.unlink(missing_ok=True)
                    raise Refused("that file is not a photo this can read") from error
                incoming.replace(path)
                self.allowed.append(str(path))
        return {"key": str(path), "name": path.name, "aspect": self.store.aspect(str(path))}

    def _route_points(self, block: dict | None) -> list[tuple[float, float]]:
        """A route's shape as if it were coordinates, for placing it in a new version."""
        if not block:
            return []
        return [(-v * block["h"] / 1e5, u * block["w"] / 1e5) for u, v in block["points"]]

    def card(self, recipe: dict) -> dict:
        """The recipe as a card: title, a column of stats, the route in the circle (see collage.apply_card)."""
        recipe = self.check(recipe)
        saved = self.card_info
        lines = saved.get("lines") or collage.card_lines_from(self.stats)
        route = collage.route_of(recipe) or next(
            (r for r in map(collage.route_of, [*self.originals.values(), *self.edits.values()]) if r), None)
        with self.paint_lock:
            return collage.apply_card(recipe, lines, saved.get("title", ""), route, self.store)

    def draw(self, source: str, layout: str, look: str, seed: int | None = None) -> dict:
        """A new version with the same photos, type and route, in a new composition."""
        base = self.recipe(source)
        if layout not in collage.LAYOUTS:
            raise Refused("unknown layout")
        if look not in collage.LOOKS:
            raise Refused("unknown look")
        taken = set(self.originals) | set(self.edits)
        number = 1
        while f"n{number:02d}" in taken:
            number += 1
        lines = [line for block in base.get("text") or [] for line in block["lines"]]
        version = collage.Version(
            f"n{number:02d}",
            seed if seed is not None else secrets.randbits(32),
            layout,
            look,
            list(base["photos"]),
            base.get("peek_photo"),
            text=lines,
            data=["route"] if base.get("route") else [],
            define=list(base.get("define") or []),
        )
        with self.paint_lock:
            recipe = collage.draw_recipe(version, self.store, "bold", self._route_points(base.get("route")))
        self.save(recipe)
        return recipe

    def with_look(self, recipe: dict, look: str) -> dict:
        """The recipe in another look; pieces, type and route stay."""
        if look not in collage.LOOKS:
            raise Refused("unknown look")
        recipe = self.check(recipe)
        rng = random.Random(recipe.get("seed", 0))
        recipe["look"] = look
        recipe["enhance"] = look == collage.NATURAL
        recipe["grade"] = collage._grade_params(look, rng)
        recipe["unify"] = None
        if look == collage.UNIFIED:
            recipe["unify"] = collage.unified_for(recipe["photos"], self.store, rng)
        return recipe

    def frame_piece(self, recipe: dict, index: int, mode: str) -> dict:
        """Frames a piece again for its current shape: `both`, `left` or `right` leg on a body photo, otherwise fill it."""
        recipe = self.check(recipe)
        if not 0 <= index < len(recipe["pieces"]):
            raise Refused("no such piece")
        piece = recipe["pieces"][index]
        with self.paint_lock:
            pieces = collage.pieces_of(recipe)
            visible = collage._owners(pieces) == index
            if not visible.any():
                visible = pieces[index].mask > 0.5
            if not visible.any():
                raise Refused("that piece has nothing to frame")
            bounds = collage._bounds(pieces[index].mask)
            key = piece["photo"]
            raw = self.store.original(key)
            if mode in ("both", "left", "right"):
                legs = collage.leg_columns(raw)
                piece["map"] = collage.frame_cell(
                    raw.width / raw.height,
                    bounds,
                    collage._inner_box(visible),
                    legs,
                    collage.shoe_line(raw),
                    mode != "left",
                    mode != "both",
                    0.5 + collage.PEEK_ASIDE if mode != "left" else 0.5 - collage.PEEK_ASIDE,
                )
            else:
                seen = collage._bounds(visible.astype(np.float32))
                focus = collage.focus_of(raw)
                piece["map"] = collage._cover_map(raw.width / raw.height, seen, focus, random.Random(0), 1.0, 0.0)
        return recipe

    def place(self, recipe: dict, what: str) -> dict:
        """Places type or the route again, where the current picture is calmest."""
        recipe = self.check(recipe)
        rng = random.Random()
        with self.paint_lock:
            picture = collage._picture_below(recipe, self.store)
            owner = collage._owners(collage.pieces_of(recipe))
            body = next((i for i, p in enumerate(recipe["pieces"]) if collage.shows_legs(p, recipe)), None)
            avoid = (owner == body).astype(np.float32) if body is not None else None
            blocks = recipe.get("text") or []
            if what == "text" and blocks:
                collage._stack_blocks(blocks, rng, picture, avoid)
            elif what == "route" and recipe.get("route"):
                route = recipe["route"]
                words = np.max([collage._type_alpha(b) for b in blocks], axis=0) if blocks else None
                blocked = collage._route_blocked(words, avoid)
                x, y, _ = collage._spot(route["w"] + 16, route["h"] + 16, rng, collage._trouble(picture), blocked, 30)
                route["x"], route["y"] = x + 8, y + 8
        return recipe

    # --- pictures

    def preview(self, recipe: dict, scale: float) -> tuple[bytes, dict]:
        recipe = self.check(recipe)
        scale = min(max(PREVIEW_SCALES[0], _finite(scale, "scale")), PREVIEW_SCALES[1])
        started = time.perf_counter()
        with self.paint_lock:
            composed = collage.paint(recipe, self.store, scale)
        out = io.BytesIO()
        collage.still_of(composed, recipe).save(out, "JPEG", quality=88)
        boxes = []
        for block in recipe.get("text") or []:
            image = collage._type_image(block)
            boxes.append([block["x"], block["y"], image.width, image.height])
        info = {"boxes": boxes, "share": round(composed.peek_share, 4), "ms": round((time.perf_counter() - started) * 1000)}
        return out.getvalue(), info

    def reel_frame(self, recipe: dict, progress: float) -> bytes:
        """One moment of the reel's reveal, without encoding anything."""
        recipe = self.check(recipe)
        key = json.dumps(recipe, sort_keys=True)
        with self.paint_lock:
            memo = self._reel_memo
            if memo is None or memo[0] != key:
                memo = (key, collage.paint(recipe, self.store, REEL_FRAME_SCALE, reel=True))
                self._reel_memo = memo
            frame = collage.reveal(memo[1], _finite(progress, "progress"))
        out = io.BytesIO()
        collage._to_image(frame).save(out, "JPEG", quality=85)
        return out.getvalue()

    def thumb(self, key: str, height: int) -> bytes:
        if key not in self.allowed:
            raise Refused("that photo is not part of this run")
        img = self.store.original(key).copy()
        img.thumbnail((height * 2, height), Image.Resampling.BILINEAR)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=82)
        return out.getvalue()

    # --- exports

    def export(self, recipe: dict, kind: str) -> str:
        recipe = json.loads(json.dumps(self.check(recipe)))
        if kind not in ("still", "reel"):
            raise Refused("export a still or a reel")
        if kind == "reel" and not shutil.which("ffmpeg"):
            raise Refused("ffmpeg is needed for reels")
        job = secrets.token_hex(6)
        self.jobs[job] = {"state": "running", "progress": 0.0, "kind": kind, "file": None, "error": None}
        threading.Thread(target=self._run_export, args=(job, recipe, kind), daemon=True).start()
        return job

    def _run_export(self, job: str, recipe: dict, kind: str) -> None:
        entry = self.jobs[job]
        try:
            self.export_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%H%M%S")
            name = f"{recipe['id']}-{recipe['layout']}-{stamp}"
            if kind == "still":
                with self.paint_lock:
                    still = collage.render_still(recipe, self.store, 1.0)
                path = self.export_dir / f"{name}.jpg"
                still.save(path, "JPEG", quality=95)
            else:
                path = self.export_dir / f"{name}.mp4"
                with self.paint_lock:
                    composed = collage.paint(recipe, self.store, 1.0, reel=True)

                def progress(share: float) -> None:
                    entry["progress"] = share

                collage._encode(composed, path, recipe["reel"], progress)
            entry.update(state="done", progress=1.0, file=path.name)
        except Exception as error:  # noqa: BLE001 - the page shows it; a thread has nobody else to tell
            entry.update(state="failed", error=str(error)[:300])

    def exports(self) -> list[str]:
        if not self.export_dir.exists():
            return []
        found = [p for p in self.export_dir.iterdir() if p.suffix in (".jpg", ".mp4")]
        return [p.name for p in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)]

    def state(self) -> dict:
        ids = [*self.originals, *(v for v in self.edits if v not in self.originals)]
        rows = []
        for version in ids:
            recipe = self.recipe(version)
            rows.append(
                {
                    "id": version,
                    "layout": recipe["layout"],
                    "look": recipe["look"],
                    "edited": version in self.edits,
                }
            )
        return {
            "folder": str(self.folder),
            "versions": rows,
            "stats": self.stats,
            "fonts": [
                {"path": f, "name": Path(f).stem if f else "Built in"}
                for f in dict.fromkeys([*collage.available_fonts(), collage.card_font()])
            ],
            "layouts": list(collage.LAYOUTS),
            "looks": list(collage.LOOKS),
            "photos": [{"key": key, "name": Path(key).name, "aspect": self.store.aspect(key)} for key in self.allowed],
            "frame": {
                "w": collage.W,
                "h": collage.H,
                "top": collage.SAFE_TOP,
                "bottom": collage.SAFE_BOTTOM,
                "right": collage.SAFE_RIGHT,
            },
            "exports": self.exports(),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "movescrobble"
    editor: Editor
    token: str

    def log_message(self, *_: object) -> None:  # a preview per drag would drown the terminal
        pass

    def _send(self, body: bytes, kind: str = "application/json", status: int = 200, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: object, status: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode(), status=status)

    def _allowed(self, query: dict) -> bool:
        """Only this page, on this machine: other sites in the browser cannot read or drive it."""
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        offered = self.headers.get("X-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(offered, self.token)

    def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._allowed(query):
            return self._send(b"not allowed", "text/plain", 403)
        try:
            if url.path == "/":
                page = PAGE.read_text().replace("__TOKEN__", self.token)
                return self._send(page.encode(), "text/html; charset=utf-8")
            if url.path == "/api/state":
                return self._json(self.editor.state())
            if url.path == "/api/recipe":
                version = query.get("id", [""])[0]
                return self._json({"recipe": self.editor.recipe(version), "original": self.editor.originals.get(version)})
            if url.path == "/api/photo":
                height = min(600, int(query.get("h", ["160"])[0]))
                return self._send(self.editor.thumb(query.get("key", [""])[0], height), "image/jpeg")
            if url.path == "/api/job":
                job = self.editor.jobs.get(query.get("id", [""])[0])
                if job is None:
                    raise Refused("no such job")
                return self._json(job)
            if url.path == "/out":
                name = Path(query.get("name", [""])[0]).name
                path = self.editor.export_dir / name
                if name not in self.editor.exports() or not path.is_file():
                    raise Refused("no such file")
                kind = "video/mp4" if path.suffix == ".mp4" else "image/jpeg"
                return self._send(path.read_bytes(), kind)
            self._send(b"not found", "text/plain", 404)
        except Refused as refused:
            self._json({"error": str(refused)}, 400)

    def do_POST(self) -> None:  # noqa: N802 - the name http.server calls
        url = urlparse(self.path)
        if not self._allowed(parse_qs(url.query)):
            return self._send(b"not allowed", "text/plain", 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > (MAX_PHOTO if url.path == "/api/photo" else MAX_BODY):
            return self._json({"error": "too large"}, 413)
        editor = self.editor
        try:
            if url.path == "/api/photo":  # the photo itself is the body
                name = (parse_qs(url.query).get("name") or [""])[0]
                return self._json({"photo": editor.add_photo(name, self.rfile.read(length))})
            body = json.loads(self.rfile.read(length) or b"{}")
            if url.path == "/api/preview":
                data, info = editor.preview(body["recipe"], body.get("scale", 0.5))
                return self._send(data, "image/jpeg", extra={"X-Info": json.dumps(info)})
            if url.path == "/api/reel-frame":
                return self._send(editor.reel_frame(body["recipe"], body.get("progress", 1.0)), "image/jpeg")
            if url.path == "/api/save":
                editor.save(body["recipe"])
                return self._json({"ok": True})
            if url.path == "/api/revert":
                return self._json({"recipe": editor.revert(str(body.get("id")))})
            if url.path == "/api/draw":
                seed = body.get("seed")
                return self._json(
                    {"recipe": editor.draw(str(body.get("source")), str(body.get("layout")), str(body.get("look")), seed)}
                )
            if url.path == "/api/look":
                return self._json({"recipe": editor.with_look(body["recipe"], str(body.get("look")))})
            if url.path == "/api/frame":
                return self._json(
                    {"recipe": editor.frame_piece(body["recipe"], int(body.get("index", -1)), str(body.get("mode")))}
                )
            if url.path == "/api/card":
                return self._json({"recipe": editor.card(body["recipe"])})
            if url.path == "/api/place":
                return self._json({"recipe": editor.place(body["recipe"], str(body.get("what")))})
            if url.path == "/api/export":
                return self._json({"job": editor.export(body["recipe"], str(body.get("kind")))})
            self._send(b"not found", "text/plain", 404)
        except (Refused, KeyError, TypeError, ValueError) as refused:
            self._json({"error": str(refused)}, 400)


def serve(
    folder: Path, port: int = 0, open_browser: bool = True, store: collage.PhotoStore | None = None
) -> tuple[ThreadingHTTPServer, str]:
    """Starts the editor on this machine only: (the server, its address). Call `serve_forever` on the server."""
    editor = Editor(folder, store)
    token = secrets.token_urlsafe(24)
    handler = type("BoundHandler", (Handler,), {"editor": editor, "token": token})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    address = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    if open_browser:
        webbrowser.open(address)
    return server, address


def start(folder: Path, open_browser: bool = True) -> tuple[Callable[[], None], str]:
    """The editor running in the background: (how to stop it, its address)."""
    try:
        server, address = serve(folder, 0, open_browser)
    except SystemExit as refused:  # a caller that stays open wants an error, not an exit
        raise RuntimeError(str(refused)) from None
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return stop, address


def runs(home: Path) -> list[Path]:
    """Runs with recipes under `home`, newest first."""
    if not home.is_dir():
        return []
    found = [p.parent for p in home.rglob("versions.json") if (p.parent / "recipes").is_dir()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def newest_run(home: Path) -> Path | None:
    """The most recent run with recipes under `home`."""
    found = runs(home)
    return found[0] if found else None
