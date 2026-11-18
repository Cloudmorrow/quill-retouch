"""Retouch's renderer: a program beside the server, with Pillow.

The core starts it with the Quill and keeps it running. It does three things,
through the record API as the administrator who installed Retouch:

- **the pictures API** (`[[apis]] id = "pictures"`): an HTTP server on
  `$PORT`, which the core forwards `/api/q/retouch/pictures/…` to. `info` is
  a photo's size as taken (the camera's orientation applied), `source` the
  photo itself at a width, `picture` an edit as it would save — turned,
  mirrored, its layers on it, cropped — at a width, which the terminal's
  viewer draws;
- **filling in**: an edit with no width or height gets its photo's;
- **saving**: an edit whose status is *queued* is rendered at its full size
  and written over the photo (the one before goes to the Trash, and the edit
  starts afresh on the saved picture) or put beside it as a new file; the
  edit says *done* and points at it, or *failed* and why.

The sums are the photo kit's (docs/PHOTO.md in the core): the turn, then
the mirror, then the layers, lowest first — words, marker strokes, pieces
copied out of the turned picture (in percent of it) — then the crop (the
same), then the size. The words are drawn in the app's own letters, Barlow,
from the core's web folder, so what the web app showed is what comes out;
without it, Pillow's own letters.

Only the standard library and Pillow are used; Pillow is the server's,
which makes the thumbnails too.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

EDIT, LAYER, FILE = "retouch.edit", "retouch.layer", "file"
EVERY = 3.0
# The most pixels a save is across or down, and the widest a preview is.
LONGEST = 8192
WIDEST = 3840
# What the kit starts a layer with, when a record says nothing.
TEXT_AT = (6.0, 6.0)
TEXT_SIZE = 6.0
TEXT_COLOUR = "#ffffff"
MARK_WIDTH = 1.5
MARK_OPACITY = 1.0
WHOLE = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0}
# What a save can be written as, by the name's suffix; anything else is written as a JPEG.
FORMATS = {
    ".jpg": {"pil": "JPEG", "save": {"quality": 90, "optimize": True}, "alpha": False},
    ".jpeg": {"pil": "JPEG", "save": {"quality": 90, "optimize": True}, "alpha": False},
    ".png": {"pil": "PNG", "save": {"optimize": True}, "alpha": True},
    ".webp": {"pil": "WEBP", "save": {"quality": 90, "method": 4}, "alpha": True},
}
# Pillow is told the biggest picture it may open, so a bomb is refused, not read.
Image.MAX_IMAGE_PIXELS = 200_000_000


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# -- the record API ----------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, said: str, status: int = 0) -> None:
        super().__init__(said)
        self.status = status


class Store:
    """The server's record API, with the Quill's token; and a cache of the files' bytes."""

    def __init__(self, url: str, token: str, cache: Path) -> None:
        self.url, self.token, self.cache = url.rstrip("/"), token, cache
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def api(self, method: str, path: str, *, body=None, query=None, data=None, headers=None, timeout=60):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = json.dumps(body).encode() if body is not None else data
        request = urllib.request.Request(url, method=method, data=payload)
        request.add_header("Authorization", "Bearer " + self.token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                raw = answer.read()
        except urllib.error.HTTPError as exc:
            said = exc.read().decode("utf-8", "replace")[:300]
            raise ApiError(f"{method} {path}: {exc.code} {said}", exc.code) from None
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path}: {exc.reason}") from None
        return json.loads(raw) if raw else None

    def records(self, model: str, **where) -> list[dict]:
        found = self.api("GET", f"/api/records/{model}", query=where or None)
        return found.get("records", []) if isinstance(found, dict) else list(found or [])

    def record(self, model: str, record_id: str) -> dict:
        return self.api("GET", f"/api/records/{model}/{urllib.parse.quote(record_id, safe='')}")

    def create(self, model: str, fields: dict) -> dict:
        return self.api("POST", f"/api/records/{model}", body={"fields": fields})

    def patch(self, model: str, record_id: str, fields: dict) -> dict:
        return self.api(
            "PATCH", f"/api/records/{model}/{urllib.parse.quote(record_id, safe='')}", body={"fields": fields}
        )

    def delete(self, model: str, record_id: str) -> None:
        self.api("DELETE", f"/api/records/{model}/{urllib.parse.quote(record_id, safe='')}")

    def upload(self, share: str, folder: str, name: str, source: Path) -> dict:
        with source.open("rb") as handle:
            return self.api(
                "POST",
                f"/api/records/{FILE}/upload",
                query={"share": share, "folder": folder, "name": name},
                data=handle,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(source.stat().st_size)},
                timeout=600,
            )

    def path_of(self, file: dict) -> Path:
        """The file's bytes, fetched once into the cache, again when it changed."""
        fields = file["fields"]
        key = hashlib.sha1(file["id"].encode()).hexdigest()[:16]
        stamp = re.sub(r"[^0-9A-Za-z]+", "", f"{fields.get('size', 0)}{fields.get('modified', '')}")
        suffix = Path(str(fields.get("name") or "")).suffix or ".bin"
        target = self.cache / f"{key}-{stamp}{suffix}"
        if target.exists():
            return target
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if target.exists():
                return target
            self.cache.mkdir(parents=True, exist_ok=True)
            for old in self.cache.glob(f"{key}-*"):
                old.unlink(missing_ok=True)
            part = target.with_name(target.name + ".part")
            request = urllib.request.Request(
                f"{self.url}/api/records/{FILE}/{urllib.parse.quote(file['id'], safe='')}/content"
            )
            request.add_header("Authorization", "Bearer " + self.token)
            log("fetching", fields.get("path") or fields.get("name"))
            try:
                with urllib.request.urlopen(request, timeout=600) as answer, part.open("wb") as out:
                    shutil.copyfileobj(answer, out, 1 << 20)
            except urllib.error.HTTPError as exc:
                part.unlink(missing_ok=True)
                raise ApiError(f"fetching {fields.get('name')}: {exc.code}", exc.code) from None
            part.replace(target)
        return target


# -- the sums ----------------------------------------------------------------------------
def number(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def rotation_of(fields: dict) -> int:
    return (round(int(number(fields.get("rotate"))) / 90) * 90) % 360


def crop_of(fields: dict) -> dict:
    """The part of the turned picture kept, never less than a twentieth each way."""
    crop = fields.get("crop")
    if not isinstance(crop, dict):
        return dict(WHOLE)
    x = min(max(number(crop.get("x")), 0.0), 95.0)
    y = min(max(number(crop.get("y")), 0.0), 95.0)
    w = min(max(number(crop.get("w"), 100.0), 5.0), 100.0 - x)
    h = min(max(number(crop.get("h"), 100.0), 5.0), 100.0 - y)
    return {"x": x, "y": y, "w": w, "h": h}


def box_of(value, fallback: dict) -> dict:
    if not isinstance(value, dict):
        return dict(fallback)
    x = min(max(number(value.get("x")), 0.0), 100.0)
    y = min(max(number(value.get("y")), 0.0), 100.0)
    w = min(max(number(value.get("w"), 10.0), 0.5), 100.0 - x)
    h = min(max(number(value.get("h"), 10.0), 0.5), 100.0 - y)
    return {"x": x, "y": y, "w": w, "h": h}


def output_size(fields: dict, cropped: tuple[int, int]) -> tuple[int, int]:
    """What the save is: the cropped size, or what out_width and out_height say."""
    w, h = cropped
    ww, wh = int(number(fields.get("out_width"))), int(number(fields.get("out_height")))
    if ww > 0 and wh > 0:
        return min(ww, LONGEST), min(wh, LONGEST)
    if ww > 0:
        ww = min(ww, LONGEST)
        return ww, max(1, round(h * ww / w))
    if wh > 0:
        wh = min(wh, LONGEST)
        return max(1, round(w * wh / h)), wh
    return w, h


def colour_of(value) -> tuple[int, int, int]:
    said = str(value or "").strip().lower()
    if m := re.fullmatch(r"#?([0-9a-f]{3})", said):
        said = "".join(ch * 2 for ch in m.group(1))
    elif m := re.fullmatch(r"#?([0-9a-f]{6})", said):
        said = m.group(1)
    else:
        said = TEXT_COLOUR[1:]
    return int(said[0:2], 16), int(said[2:4], 16), int(said[4:6], 16)


def points_of(value) -> list[tuple[float, float]]:
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                out.append((min(max(float(item[0]), 0.0), 100.0), min(max(float(item[1]), 0.0), 100.0)))
            except (TypeError, ValueError):
                continue
    return out


def layer_of(fields: dict) -> dict:
    """A layer's fields as the kit draws them, by its kind."""
    kind = str(fields.get("kind") or "text")
    kind = kind if kind in ("text", "mark", "piece") else "text"
    layer = {
        "kind": kind,
        "x": min(max(number(fields.get("x"), TEXT_AT[0]), 0.0), 100.0),
        "y": min(max(number(fields.get("y"), TEXT_AT[1]), 0.0), 100.0),
        "hidden": bool(fields.get("hidden")),
        "colour": colour_of(fields.get("colour")),
    }
    if kind == "text":
        layer["text"] = str(fields.get("text") or "")
        layer["size"] = min(max(number(fields.get("size"), TEXT_SIZE), 0.5), 100.0)
    elif kind == "mark":
        layer["points"] = points_of(fields.get("points"))
        layer["width"] = min(max(number(fields.get("width"), MARK_WIDTH), 0.1), 25.0)
        layer["opacity"] = min(max(number(fields.get("opacity"), MARK_OPACITY), 0.05), 1.0)
    else:
        layer["source"] = box_of(fields.get("source"), {"x": 0, "y": 0, "w": 25, "h": 25})
        layer["w"] = min(max(number(fields.get("w"), layer["source"]["w"]), 0.5), 100.0)
        layer["h"] = min(max(number(fields.get("h"), layer["source"]["h"]), 0.5), 100.0)
    return layer


def in_order(records: list[dict]) -> list[dict]:
    """Layer records, lowest first: by order, then by when they were made."""
    return sorted(
        records,
        key=lambda r: (
            int(number((r.get("fields") or {}).get("order"))),
            str(r.get("created_at") or ""),
            str(r.get("id") or ""),
        ),
    )


# -- letters -----------------------------------------------------------------------------
def font_file() -> Path | None:
    """The app's own letters, Barlow semibold, from the core's web folder; None without it."""
    try:
        from cloudmorrow.server.routes.web import WEB
    except Exception:  # noqa: BLE001 — an older core, or none on this Python
        return None
    found = Path(WEB) / "barlow-600-latin.woff2"
    return found if found.is_file() else None


_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def font(px: int) -> ImageFont.FreeTypeFont:
    px = max(4, int(px))
    if px not in _fonts:
        file = font_file()
        try:
            _fonts[px] = ImageFont.truetype(str(file), px) if file else ImageFont.load_default(size=px)
        except Exception:  # noqa: BLE001 — a font Pillow cannot read: its own, then
            _fonts[px] = ImageFont.load_default(size=px)
    return _fonts[px]


# -- drawing -----------------------------------------------------------------------------
class Refused(Exception):
    """A request that cannot be answered: (status, what to say)."""

    def __init__(self, status: int, said: str) -> None:
        super().__init__(said)
        self.status = status


def opened(path: Path, *, reduce_to: int = 0) -> Image.Image:
    """The photo as taken: the camera's orientation applied. *reduce_to* is a
    width a JPEG may be decoded down towards, for speed, never below it."""
    try:
        image = Image.open(path)
        if reduce_to and image.format == "JPEG":
            image.draft(None, (reduce_to, reduce_to))
        image.load()
    except Exception as exc:  # noqa: BLE001 — not a picture Pillow reads
        raise Refused(422, f"not a picture the server can read: {exc}") from None
    return ImageOps.exif_transpose(image) or image


def turned(image: Image.Image, rotate: int, mirror: bool) -> Image.Image:
    """The picture *rotate* degrees clockwise, then mirrored if asked."""
    turn = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}.get(rotate)
    if turn is not None:
        image = image.transpose(turn)
    return ImageOps.mirror(image) if mirror else image


def draw_text(base: Image.Image, layer: dict) -> None:
    """Words at their place, in their size and colour, with a dark edge, onto *base* (RGBA)."""
    px = max(1, round(layer["size"] / 100 * base.height))
    layer_img = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer_img)
    x = layer["x"] / 100 * base.width
    y = layer["y"] / 100 * base.height
    letters = font(px)
    for i, line in enumerate(layer["text"].split("\n")):
        draw.text(
            (x, y + i * px * 1.2),
            line,
            font=letters,
            fill=(*layer["colour"], 255),
            stroke_width=max(1, round(px * 0.08)),
            stroke_fill=(0, 0, 0, 140),
        )
    base.alpha_composite(layer_img)


def draw_mark(base: Image.Image, layer: dict) -> None:
    """A marker stroke: a round line through its points, as wide and as solid as it says."""
    pts = [(x / 100 * base.width, y / 100 * base.height) for x, y in layer["points"]]
    if not pts:
        return
    width = max(1, round(layer["width"] / 100 * base.height))
    alpha = round(layer["opacity"] * 255)
    layer_img = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer_img)
    colour = (*layer["colour"], 255)
    half = width / 2
    if len(pts) > 1:
        draw.line(pts, fill=colour, width=width, joint="curve")
    for x, y in (pts[0], pts[-1]) if len(pts) > 1 else pts:
        draw.ellipse((x - half, y - half, x + half, y + half), fill=colour)
    if alpha < 255:
        # The stroke as one shape, made see-through all at once: no darker where it crosses itself.
        layer_img.putalpha(layer_img.getchannel("A").point(lambda a: a * alpha // 255))
    base.alpha_composite(layer_img)


def draw_piece(base: Image.Image, source: Image.Image, layer: dict) -> None:
    """A piece of the turned picture, scaled into its box."""
    s = layer["source"]
    left = round(s["x"] / 100 * source.width)
    top = round(s["y"] / 100 * source.height)
    right = min(source.width, max(left + 1, round((s["x"] + s["w"]) / 100 * source.width)))
    bottom = min(source.height, max(top + 1, round((s["y"] + s["h"]) / 100 * source.height)))
    piece = source.crop((left, top, right, bottom))
    w = max(1, round(layer["w"] / 100 * base.width))
    h = max(1, round(layer["h"] / 100 * base.height))
    piece = piece.resize((w, h), Image.Resampling.LANCZOS).convert("RGBA")
    base.alpha_composite(piece, (round(layer["x"] / 100 * base.width), round(layer["y"] / 100 * base.height)))


def cropped(image: Image.Image, crop: dict) -> Image.Image:
    if crop == WHOLE:
        return image
    w, h = image.size
    left = round(crop["x"] / 100 * w)
    top = round(crop["y"] / 100 * h)
    right = min(w, max(left + 1, round((crop["x"] + crop["w"]) / 100 * w)))
    bottom = min(h, max(top + 1, round((crop["y"] + crop["h"]) / 100 * h)))
    return image.crop((left, top, right, bottom))


def compose(image: Image.Image, fields: dict, layers: list[dict]) -> Image.Image:
    """The edit done to the picture, in the kit's order: turn, mirror, the layers lowest first, crop."""
    source = turned(image, rotation_of(fields), bool(fields.get("mirror")))
    shown = [layer_of(f) for f in layers]
    shown = [layer for layer in shown if not layer["hidden"]]
    if not shown:
        return cropped(source, crop_of(fields))
    base = source.convert("RGBA")
    for layer in shown:
        if layer["kind"] == "text":
            draw_text(base, layer)
        elif layer["kind"] == "mark":
            draw_mark(base, layer)
        else:
            draw_piece(base, source, layer)
    return cropped(base, crop_of(fields))


def sized(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image if image.size == size else image.resize(size, Image.Resampling.LANCZOS)


def flat(image: Image.Image, alpha: bool) -> Image.Image:
    """RGB over black for a format without transparency; RGBA kept otherwise."""
    if alpha:
        return image if image.mode in ("RGB", "RGBA") else image.convert("RGBA")
    if image.mode == "RGBA":
        ground = Image.new("RGB", image.size, (0, 0, 0))
        ground.paste(image, mask=image.split()[3])
        return ground
    return image if image.mode == "RGB" else image.convert("RGB")


def jpeg_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    flat(image, False).save(out, format="JPEG", quality=85)
    return out.getvalue()


def fitted(image: Image.Image, width: int) -> Image.Image:
    """The picture *width* pixels wide, or as it is when it is narrower."""
    width = min(max(16, int(width)), WIDEST)
    if image.width <= width:
        return image
    return image.resize((width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)


class Renderer:
    """Everything that draws, over a store (the record API, or a test's stand-in)."""

    def __init__(self, store, home: Path | None = None) -> None:
        self.store = store
        self.home = home or Path(tempfile.gettempdir())
        self._info: dict[tuple, dict] = {}

    # -- what a file is --------------------------------------------------------------
    def file(self, file_id: str, user: str | None = None) -> dict:
        try:
            found = self.store.record(FILE, file_id)
        except ApiError as exc:
            raise Refused(404 if exc.status in (403, 404) else 502, f"no such file: {file_id}") from None
        self._mine(found, user)
        return found

    @staticmethod
    def _mine(record: dict, user: str | None) -> None:
        """The renderer reads as the administrator who installed it; a person is shown only their own."""
        if user and record.get("owner") and record["owner"] != user:
            raise Refused(404, "not found")

    def info(self, file: dict) -> dict:
        fields = file["fields"]
        key = (file["id"], fields.get("modified"), fields.get("size"))
        if key not in self._info:
            # The size is in the file's header; a camera's sideways orientation swaps the sides.
            try:
                with Image.open(self.store.path_of(file)) as whole:
                    width, height = whole.size
                    sideways = whole.getexif().get(0x0112, 1) in (5, 6, 7, 8)
            except Exception as exc:  # noqa: BLE001 — not a picture Pillow reads
                raise Refused(422, f"not a picture the server can read: {exc}") from None
            if sideways:
                width, height = height, width
            self._info[key] = {"width": width, "height": height, "image": True}
        return dict(self._info[key])

    def source_jpeg(self, file_id: str, width: int, user: str | None = None) -> bytes:
        file = self.file(file_id, user)
        image = opened(self.store.path_of(file), reduce_to=width)
        return jpeg_bytes(fitted(image, width))

    # -- an edit ----------------------------------------------------------------------
    def edit(self, edit_id: str, user: str | None = None) -> dict:
        try:
            found = self.store.record(EDIT, edit_id)
        except ApiError as exc:
            raise Refused(404 if exc.status in (403, 404) else 502, f"no such edit: {edit_id}") from None
        self._mine(found, user)
        return found

    def layers(self, edit: dict) -> list[dict]:
        return [r["fields"] for r in in_order(self.store.records(LAYER, edit=edit["id"]))]

    def picture_jpeg(self, edit_id: str, width: int, user: str | None = None) -> bytes:
        """The edit as it would save, *width* pixels wide: drawn from a smaller
        copy of the photo, since every place and size is in percent."""
        edit = self.edit(edit_id, user)
        fields = edit["fields"]
        if not fields.get("source"):
            raise Refused(422, "this edit has no photo")
        file = self.file(str(fields["source"]))
        crop = crop_of(fields)
        # Enough pixels that the cropped part is still *width* wide, and no more.
        wanted = min(WIDEST * 2, round(width / (crop["w"] / 100)) * 2)
        image = opened(self.store.path_of(file), reduce_to=wanted)
        image = compose(image, fields, self.layers(edit))
        return jpeg_bytes(fitted(image, width))

    def render(self, edit: dict) -> Image.Image:
        """The save at its full size."""
        fields = edit["fields"]
        file = self.file(str(fields["source"]))
        image = compose(opened(self.store.path_of(file)), fields, self.layers(edit))
        return sized(image, output_size(fields, image.size))

    # -- filling in what the kit left blank ---------------------------------------------
    def fill(self) -> None:
        """Edits without a size get their photo's."""
        for edit in self.store.records(EDIT):
            f = edit["fields"]
            if (int(number(f.get("width"))) > 0 and int(number(f.get("height"))) > 0) or not f.get("source"):
                continue
            try:
                info = self.info(self.store.record(FILE, str(f["source"])))
            except (Refused, ApiError) as exc:
                log("edit", edit["id"], "-", exc)
                continue
            self.store.patch(EDIT, edit["id"], {"width": info["width"], "height": info["height"]})
            log("size of", f.get("name"), "is", info["width"], "x", info["height"])

    # -- saving ------------------------------------------------------------------------
    def save_queued(self) -> None:
        for edit in self.store.records(EDIT, status="queued"):
            self.save(edit)

    def save(self, edit: dict) -> dict:
        """Save one edit: over the photo, or beside it. The edit as it is after, done or failed."""
        fields = edit["fields"]
        self.store.patch(EDIT, edit["id"], {"status": "rendering", "message": "Saving"})
        started = time.time()
        try:
            if not fields.get("source"):
                raise ValueError("this edit has no photo")
            photo = self.file(str(fields["source"]))
            share = str(photo["fields"].get("share") or "my-files")
            folder = str(photo["fields"].get("folder") or "")
            photo_name = str(photo["fields"].get("name") or "photo.jpg")
            overwrite = bool(fields.get("overwrite"))
            wanted = str(fields.get("save_name") or "").strip() if not overwrite else photo_name
            if not wanted:
                raise ValueError("the new file needs a name")
            stem, suffix = split_name(wanted, photo_name)
            fmt = FORMATS.get(suffix.lower())
            if fmt is None:
                # A kind nothing here writes (HEIC, GIF…): a JPEG beside it, under the same name.
                suffix, fmt = ".jpg", FORMATS[".jpg"]
                if overwrite:
                    overwrite = False
            image = flat(self.render(edit), fmt["alpha"])
            self.home.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.home) as tmp:
                out = Path(tmp) / ("out" + suffix)
                log(
                    "saving",
                    fields.get("name"),
                    "as",
                    fmt["pil"],
                    image.size,
                    "over" if overwrite else "beside",
                    photo_name,
                )
                image.save(out, format=fmt["pil"], **fmt["save"])
                if overwrite:
                    made = self.write_over(photo, share, folder, out)
                else:
                    name = unique_name(self.store, share, folder, stem, suffix)
                    made = self.store.upload(share, folder, name, out)
        except (ValueError, Refused, ApiError, OSError) as exc:
            log("save", fields.get("name"), "failed:", exc)
            return self.store.patch(EDIT, edit["id"], {"status": "failed", "message": f"The save failed: {exc}"[:400]})
        made_name = str(made["fields"].get("name") or wanted)
        where = "/".join(x for x in ("My Files" if share == "my-files" else share, folder) if x)
        size = f"{image.size[0]} × {image.size[1]}"
        if overwrite:
            # The picture is the edit now: it starts afresh on the saved photo.
            for layer in self.store.records(LAYER, edit=edit["id"]):
                with contextlib.suppress(ApiError):
                    self.store.delete(LAYER, layer["id"])
            said = f"Saved over {made_name} ({size}) in {time.time() - started:.0f} s; the one before is in the Trash"
            log(said)
            return self.store.patch(
                EDIT,
                edit["id"],
                {
                    "status": "done",
                    "message": said,
                    "output": made["id"],
                    "source": made["id"],
                    "width": image.size[0],
                    "height": image.size[1],
                    "rotate": 0,
                    "mirror": False,
                    "crop": None,
                    "out_width": None,
                    "out_height": None,
                },
            )
        said = f"Saved {made_name} ({size}) to {where} in {time.time() - started:.0f} s"
        log(said)
        return self.store.patch(EDIT, edit["id"], {"status": "done", "message": said, "output": made["id"]})

    def write_over(self, photo: dict, share: str, folder: str, out: Path) -> dict:
        """The saved picture in the photo's place: put beside it under a working
        name, the photo moved to the Trash, the new one given its name — and so
        its id, which a file's path makes."""
        photo_name = str(photo["fields"].get("name") or "photo.jpg")
        stem, suffix = split_name(photo_name, photo_name)
        working = unique_name(self.store, share, folder, f"{stem} (saving)", suffix)
        made = self.store.upload(share, folder, working, out)
        self.store.delete(FILE, photo["id"])
        return self.store.patch(FILE, made["id"], {"name": photo_name})


def split_name(name: str, like: str) -> tuple[str, str]:
    """`(stem, suffix)` of a file name; a name with no suffix takes *like*'s."""
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(name or "")).strip(" .")
    path = Path(clean)
    suffix = path.suffix if path.suffix.lower() in FORMATS or (path.suffix and len(path.suffix) <= 6) else ""
    stem = path.stem if suffix else clean
    if not suffix:
        suffix = Path(like).suffix or ".jpg"
    return stem or "Photo", suffix


def unique_name(store, share: str, folder: str, stem: str, suffix: str) -> str:
    """`<stem><suffix>`, or `<stem> 2<suffix>` and so on when that is taken in the folder."""
    try:
        taken = {str(f["fields"].get("name", "")).lower() for f in store.records(FILE, share=share, folder=folder)}
    except ApiError:
        taken = set()
    name, n = f"{stem}{suffix}", 1
    while name.lower() in taken:
        n += 1
        name = f"{stem} {n}{suffix}"
    return name


# -- the pictures API ------------------------------------------------------------------------
def serve(renderer: Renderer, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """The pictures API on *port*, in a thread of its own; the server, to shut down."""

    class Pictures(BaseHTTPRequestHandler):
        server_version = "retouch-pictures"

        def log_message(self, fmt, *args) -> None:  # quiet: the core logs requests
            pass

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            what = url.path.strip("/").removeprefix("pictures").strip("/")
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            user = self.headers.get("X-Cloudmorrow-User") or None
            try:
                if what == "info":
                    self._json(200, renderer.info(renderer.file(_need(query, "file"), user)))
                elif what == "source":
                    self._jpeg(renderer.source_jpeg(_need(query, "file"), _w(query), user))
                elif what == "picture":
                    self._jpeg(renderer.picture_jpeg(_need(query, "edit"), _w(query), user))
                else:
                    raise Refused(404, "the pictures API answers info, source and picture")
            except Refused as exc:
                self._json(exc.status, {"detail": str(exc)})
            except ApiError as exc:
                self._json(502, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 — said, not a dropped connection
                log("pictures:", repr(exc))
                self._json(500, {"detail": str(exc)[:300]})

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _jpeg(self, raw: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=10")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer((host, port), Pictures)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="pictures", daemon=True).start()
    return server


def _need(query: dict, name: str) -> str:
    if not query.get(name):
        raise Refused(400, f"{name}= is needed")
    return query[name]


def _w(query: dict) -> int:
    try:
        return min(WIDEST, max(16, int(float(query.get("w") or 1280))))
    except ValueError:
        raise Refused(400, "w= is pixels") from None


# -- running ---------------------------------------------------------------------------------
def every(what, name: str) -> None:
    while True:
        try:
            what()
        except ApiError as exc:
            log(name, "- the server:", exc)
        except Exception as exc:  # noqa: BLE001 — the next round may go better
            log(name, "- trouble:", repr(exc))
        time.sleep(EVERY)


def main() -> None:
    url, token = os.environ.get("CLOUDMORROW_URL", ""), os.environ.get("CLOUDMORROW_TOKEN", "")
    if not url or not token:
        print("CLOUDMORROW_URL and CLOUDMORROW_TOKEN are needed: this is started by the server", file=sys.stderr)
        sys.exit(2)
    home = Path(os.environ.get("HOME") or tempfile.gettempdir())
    renderer = Renderer(Store(url, token, home / "cache"), home / "work")
    log("up;", "letters from " + str(font_file() or "Pillow"))
    if os.environ.get("PORT"):
        serve(renderer, int(os.environ["PORT"]))
        log("pictures on port", os.environ["PORT"])
    threading.Thread(target=every, args=(renderer.fill, "filling in"), name="fill", daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):
        every(renderer.save_queued, "saving")


if __name__ == "__main__":
    main()
