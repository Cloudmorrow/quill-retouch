"""Retouch's renderer: a program beside the server, with Pillow.

The core starts it with the Quill and keeps it running. It does three things,
through the record API as the administrator who installed Retouch:

- **the pictures API** (`[[apis]] id = "pictures"`): an HTTP server on
  `$PORT`, which the core forwards `/api/q/retouch/pictures/…` to. `info` is
  a photo's size as taken (the camera's orientation applied), `source` the
  photo itself at a width, `picture` an edit as it would export — turned,
  mirrored, written on, cropped — at a width, which the terminal's viewer
  draws;
- **filling in**: an edit with no width or height gets its photo's;
- **exports**: an edit whose status is *queued* is rendered at its full size
  and the result is put in My Files › Retouch Exports, made the first time;
  the edit says *done* and points at it, or *failed* and why.

The sums are the photo kit's (docs/PHOTO.md in the core): the turn, then
the mirror, then the words (in percent of the turned picture), then the
crop (the same), then the size. The words are drawn in the app's own
letters, Barlow, from the core's web folder, so what the web app showed is
what comes out; without it, Pillow's own letters.

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

EDIT, TEXT, FILE = "retouch.edit", "retouch.text", "file"
# Every export lands here, in the drive of whoever the renderer runs as.
EXPORTS = ("my-files", "Retouch Exports")
EVERY = 3.0
# The most pixels an export is across or down, and the widest a preview is.
LONGEST = 8192
WIDEST = 3840
# What the kit starts a text with, when a record says nothing.
TEXT_AT = (6.0, 6.0)
TEXT_SIZE = 6.0
TEXT_COLOUR = "#ffffff"
WHOLE = {"x": 0.0, "y": 0.0, "w": 100.0, "h": 100.0}
FORMATS = {
    "jpeg": {"suffix": ".jpg", "pil": "JPEG", "save": {"quality": 90, "optimize": True}, "alpha": False},
    "png": {"suffix": ".png", "pil": "PNG", "save": {"optimize": True}, "alpha": True},
    "webp": {"suffix": ".webp", "pil": "WEBP", "save": {"quality": 90, "method": 4}, "alpha": True},
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


def output_size(fields: dict, cropped: tuple[int, int]) -> tuple[int, int]:
    """What the export is: the cropped size, or what out_width and out_height say."""
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


def text_of(fields: dict) -> dict:
    return {
        "text": str(fields.get("text") or ""),
        "x": min(max(number(fields.get("x"), TEXT_AT[0]), 0.0), 100.0),
        "y": min(max(number(fields.get("y"), TEXT_AT[1]), 0.0), 100.0),
        "size": min(max(number(fields.get("size"), TEXT_SIZE), 0.5), 100.0),
        "colour": colour_of(fields.get("colour")),
    }


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


def written(image: Image.Image, texts: list[dict]) -> Image.Image:
    """The texts drawn on the picture, each at its place, in its size and colour, with a dark edge."""
    if not texts:
        return image
    base = image.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for text in texts:
        px = max(1, round(text["size"] / 100 * base.height))
        x = text["x"] / 100 * base.width
        y = text["y"] / 100 * base.height
        letters = font(px)
        for i, line in enumerate(text["text"].split("\n")):
            draw.text(
                (x, y + i * px * 1.2),
                line,
                font=letters,
                fill=(*text["colour"], 255),
                stroke_width=max(1, round(px * 0.08)),
                stroke_fill=(0, 0, 0, 140),
            )
    return Image.alpha_composite(base, layer)


def cropped(image: Image.Image, crop: dict) -> Image.Image:
    if crop == WHOLE:
        return image
    w, h = image.size
    left = round(crop["x"] / 100 * w)
    top = round(crop["y"] / 100 * h)
    right = min(w, max(left + 1, round((crop["x"] + crop["w"]) / 100 * w)))
    bottom = min(h, max(top + 1, round((crop["y"] + crop["h"]) / 100 * h)))
    return image.crop((left, top, right, bottom))


def compose(image: Image.Image, fields: dict, texts: list[dict]) -> Image.Image:
    """The edit done to the picture, in the kit's order: turn, mirror, words, crop."""
    image = turned(image, rotation_of(fields), bool(fields.get("mirror")))
    image = written(image, [text_of(t) for t in texts])
    return cropped(image, crop_of(fields))


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

    def texts(self, edit: dict) -> list[dict]:
        return [t["fields"] for t in self.store.records(TEXT, edit=edit["id"])]

    def picture_jpeg(self, edit_id: str, width: int, user: str | None = None) -> bytes:
        """The edit as it would export, *width* pixels wide: drawn from a smaller
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
        image = compose(image, fields, self.texts(edit))
        return jpeg_bytes(fitted(image, width))

    def render(self, edit: dict) -> Image.Image:
        """The export at its full size."""
        fields = edit["fields"]
        file = self.file(str(fields["source"]))
        image = compose(opened(self.store.path_of(file)), fields, self.texts(edit))
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

    # -- exports ---------------------------------------------------------------------
    def render_queued(self) -> None:
        for edit in self.store.records(EDIT, status="queued"):
            self.export(edit)

    def export(self, edit: dict) -> dict:
        """Render one edit's export; the edit as it is after, done or failed."""
        fields = edit["fields"]
        self.store.patch(EDIT, edit["id"], {"status": "rendering", "message": "Rendering"})
        fmt = FORMATS.get(str(fields.get("format") or "jpeg")) or FORMATS["jpeg"]
        started = time.time()
        try:
            if not fields.get("source"):
                raise ValueError("this edit has no photo")
            image = flat(self.render(edit), fmt["alpha"])
            share, folder = EXPORTS
            ensure_folder(self.store, share, folder)
            name = unique_name(
                self.store, share, folder, export_stem(fields.get("export_name") or fields.get("name")), fmt["suffix"]
            )
            self.home.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.home) as tmp:
                out = Path(tmp) / ("out" + fmt["suffix"])
                log("rendering", fields.get("name"), "as", fmt["pil"], image.size)
                image.save(out, format=fmt["pil"], **fmt["save"])
                made = self.store.upload(share, folder, name, out)
        except (ValueError, Refused, ApiError, OSError) as exc:
            log("export", fields.get("name"), "failed:", exc)
            return self.store.patch(
                EDIT, edit["id"], {"status": "failed", "message": f"The export failed: {exc}"[:400]}
            )
        where = "/".join(x for x in ("My Files" if share == "my-files" else share, folder) if x)
        said = f"Exported {name} ({image.size[0]} × {image.size[1]}) to {where} in {time.time() - started:.0f} s"
        log(said)
        return self.store.patch(EDIT, edit["id"], {"status": "done", "message": said, "output": made["id"]})


def fitted(image: Image.Image, width: int) -> Image.Image:
    """The picture *width* pixels wide, or as it is when it is narrower."""
    width = min(max(16, int(width)), WIDEST)
    if image.width <= width:
        return image
    return image.resize((width, max(1, round(image.height * width / image.width))), Image.Resampling.LANCZOS)


def ensure_folder(store, share: str, folder: str) -> None:
    """The exports' folder, made the first time there is something to put in it."""
    try:
        if any(
            f["fields"].get("kind") == "folder" and f["fields"].get("name") == folder
            for f in store.records(FILE, share=share, folder="")
        ):
            return
        store.create(FILE, {"share": share, "folder": "", "name": folder, "kind": "folder"})
    except ApiError as exc:
        # Made meanwhile is as good; anything else, the upload says.
        log("the exports folder:", exc)


def export_stem(name) -> str:
    """What an export is called, without the suffix; nothing a file name cannot hold."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(name or "")).strip(" .")
    return stem or "Export"


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
        every(renderer.render_queued, "exports")


if __name__ == "__main__":
    main()
