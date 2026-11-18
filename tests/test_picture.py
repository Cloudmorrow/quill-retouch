"""The renderer on real pictures: a photo Pillow makes, turned, mirrored,
written on, cropped, sized, exported and drawn.

The record API is stood in for by a dict (the service only lists, reads,
patches and uploads), so this runs without a server.
"""

import io
import shutil
import sys
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import picture

EDIT, TEXT, FILE = picture.EDIT, picture.TEXT, picture.FILE


class FakeStore:
    """The record API as the renderer uses it, over dicts and a folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data: dict[str, dict[str, dict]] = {}
        self.n = 0

    def add(self, model: str, fields: dict, *, id_: str | None = None, owner: str = "admin") -> dict:
        self.n += 1
        record = {"id": id_ or f"r{self.n}", "model": model, "owner": owner, "fields": dict(fields)}
        self.data.setdefault(model, {})[record["id"]] = record
        return record

    def records(self, model: str, **where) -> list[dict]:
        return [r for r in self.data.get(model, {}).values() if all(r["fields"].get(k) == v for k, v in where.items())]

    def record(self, model: str, record_id: str) -> dict:
        try:
            return self.data[model][record_id]
        except KeyError:
            raise picture.ApiError(f"GET {model}/{record_id}: 404", 404) from None

    def create(self, model: str, fields: dict) -> dict:
        return self.add(model, fields)

    def patch(self, model: str, record_id: str, fields: dict) -> dict:
        self.record(model, record_id)["fields"].update(fields)
        return self.data[model][record_id]

    def file(self, path: Path, folder: str = "Photos") -> dict:
        fields = {"share": "my-files", "folder": folder, "name": path.name, "kind": "file", "size": path.stat().st_size,
                  "modified": str(path.stat().st_mtime_ns), "local": str(path)}  # fmt: skip
        return self.add(FILE, fields, id_=f"f_{folder}/{path.name}")

    def path_of(self, file: dict) -> Path:
        return Path(file["fields"]["local"])

    def upload(self, share: str, folder: str, name: str, source: Path) -> dict:
        target = self.root / "uploaded" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(source, target)
        return self.file(target, folder)


def photo(path: Path, size=(400, 300), *, orientation: int | None = None) -> Path:
    """A picture: red on the left half, blue on the right, a green strip along the top."""
    image = Image.new("RGB", size, "red")
    image.paste("blue", (size[0] // 2, 0, size[0], size[1]))
    image.paste("green", (0, 0, size[0], size[1] // 10))
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    image.save(path, quality=95, **kwargs)
    return path


def pixel(data: bytes, x: float, y: float) -> tuple:
    """The colour at a point, as a share of the width and the height."""
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        return image.getpixel((int(image.width * x), int(image.height * y)))


def near(colour, wanted, within=40) -> bool:
    return all(abs(a - b) <= within for a, b in zip(colour, wanted, strict=True))


RED, BLUE, GREEN, WHITE = (255, 0, 0), (0, 0, 255), (0, 128, 0), (255, 255, 255)


@pytest.fixture()
def store(tmp_path) -> FakeStore:
    return FakeStore(tmp_path)


@pytest.fixture()
def renderer(store, tmp_path) -> picture.Renderer:
    return picture.Renderer(store, tmp_path / "work")


def an_edit(store, tmp_path, **fields) -> dict:
    file = store.file(photo(tmp_path / "beach.jpg"))
    return store.add(EDIT, {"name": "Beach", "source": file["id"], "status": "queued", **fields})


# -- what a photo is ------------------------------------------------------------------------
def test_info_is_the_size_as_taken(store, renderer, tmp_path):
    plain = store.file(photo(tmp_path / "plain.jpg"))
    assert renderer.info(plain) == {"width": 400, "height": 300, "image": True}
    sideways = store.file(photo(tmp_path / "side.jpg", orientation=6))
    assert renderer.info(sideways) == {"width": 300, "height": 400, "image": True}
    notes = store.file((tmp_path / "notes.txt").write_text("hi") and tmp_path / "notes.txt")
    with pytest.raises(picture.Refused, match="not a picture"):
        renderer.info(notes)


def test_source_is_the_photo_at_a_width_turned_as_the_camera_said(store, renderer, tmp_path):
    file = store.file(photo(tmp_path / "side.jpg", orientation=6))
    data = renderer.source_jpeg(file["id"], 150)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (150, 200)
    # Turned right by the camera's word: the green strip, once at the top, is down the right.
    assert near(pixel(data, 0.95, 0.5), GREEN)


def test_fill_gives_an_edit_its_photos_size(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path, status="editing")
    renderer.fill()
    assert (edit["fields"]["width"], edit["fields"]["height"]) == (400, 300)


# -- the picture ----------------------------------------------------------------------------
def test_the_picture_is_turned_mirrored_and_cropped(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path, rotate=90)
    data = renderer.picture_jpeg(edit["id"], 150)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (150, 200)
    # A quarter turn clockwise: the top strip is now down the right; red, once left, is at the top.
    assert near(pixel(data, 0.95, 0.5), GREEN) and near(pixel(data, 0.3, 0.3), RED)
    edit["fields"].update(rotate=0, mirror=True)
    data = renderer.picture_jpeg(edit["id"], 200)
    assert near(pixel(data, 0.2, 0.5), BLUE) and near(pixel(data, 0.8, 0.5), RED)
    edit["fields"].update(mirror=False, crop={"x": 50, "y": 10, "w": 50, "h": 90})
    data = renderer.picture_jpeg(edit["id"], 200)
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (200, 270)
    assert near(pixel(data, 0.1, 0.1), BLUE) and near(pixel(data, 0.5, 0.05), BLUE)


def test_words_are_drawn_where_they_are_said(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path)
    store.add(TEXT, {"edit": edit["id"], "text": "WWWW", "x": 10, "y": 50, "size": 30, "colour": "#ffffff"})
    data = renderer.picture_jpeg(edit["id"], 400)
    # Somewhere in the box the words fill, a pixel is white; well away from it, the photo is as it was.
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        box = image.crop((40, 150, 200, 240))
        assert any(
            near(box.getpixel((x, y)), WHITE, 30) for x in range(0, box.width, 2) for y in range(0, box.height, 2)
        )
    assert near(pixel(data, 0.9, 0.3), BLUE)


def test_a_text_on_a_missing_edit_or_photo_is_refused(store, renderer, tmp_path):
    with pytest.raises(picture.Refused, match="no such edit"):
        renderer.picture_jpeg("nope", 100)
    edit = store.add(EDIT, {"name": "Lost", "source": "gone"})
    with pytest.raises(picture.Refused, match="no such file"):
        renderer.picture_jpeg(edit["id"], 100)


# -- exports ----------------------------------------------------------------------------------
def test_an_export_lands_in_the_exports_folder_at_its_size(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path, rotate=90, crop={"x": 0, "y": 0, "w": 50, "h": 50}, out_width=60, format="png")
    store.add(TEXT, {"edit": edit["id"], "text": "Hi", "x": 5, "y": 5, "size": 20})
    renderer.render_queued()
    fields = edit["fields"]
    assert fields["status"] == "done", fields["message"]
    made = store.record(FILE, fields["output"])["fields"]
    assert made["folder"] == "Retouch Exports" and made["name"] == "Beach.png"
    with Image.open(made["local"]) as out:
        # Turned: 300 × 400; half each way: 150 × 200; 60 wide keeps the shape.
        assert out.size == (60, 80) and out.format == "PNG"
    assert "60 × 80" in fields["message"]
    # The folder was made, once; a second export does not take the first's name.
    assert len(store.records(FILE, kind="folder")) == 1
    fields.update(status="queued", format="jpeg", export_name="Beach")
    renderer.render_queued()
    assert store.record(FILE, fields["output"])["fields"]["name"] == "Beach.jpg"
    fields.update(status="queued")
    renderer.render_queued()
    assert store.record(FILE, fields["output"])["fields"]["name"] == "Beach 2.jpg"


def test_a_failed_export_says_why(store, renderer):
    edit = store.add(EDIT, {"name": "Lost", "source": "gone", "status": "queued"})
    renderer.render_queued()
    assert edit["fields"]["status"] == "failed" and "no such file" in edit["fields"]["message"]


# -- the API -----------------------------------------------------------------------------------
def test_the_pictures_api_answers_info_source_and_picture(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path)
    server = picture.serve(renderer, 0)
    port = server.server_address[1]
    try:
        base = f"http://127.0.0.1:{port}/pictures"
        with urllib.request.urlopen(f"{base}/info?file={edit['fields']['source']}") as answer:
            assert b'"width": 400' in answer.read()
        with urllib.request.urlopen(f"{base}/source?file={edit['fields']['source']}&w=100") as answer:
            assert answer.headers["Content-Type"] == "image/jpeg"
        with urllib.request.urlopen(f"{base}/picture?edit={edit['id']}&w=100") as answer:
            drawn = answer.read()
        with Image.open(io.BytesIO(drawn)) as image:
            assert image.size == (100, 75)
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/picture?w=100")
        assert refused.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/nothing")
        assert refused.value.code == 404
        # Somebody else's edit is not theirs to see.
        request = urllib.request.Request(
            f"{base}/picture?edit={edit['id']}&w=100", headers={"X-Cloudmorrow-User": "ana"}
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request)
        assert refused.value.code == 404
    finally:
        server.shutdown()
