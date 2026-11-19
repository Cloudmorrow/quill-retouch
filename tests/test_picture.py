"""The renderer on real pictures: a photo Pillow makes, turned, mirrored, with
layers on it, cropped, sized, saved over and beside, and drawn.

The record API is stood in for by a dict (the service only lists, reads,
patches, deletes and uploads), so this runs without a server.
"""

import io
import shutil
import sys
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))
import picture  # noqa: E402

EDIT, LAYER, FILE = picture.EDIT, picture.LAYER, picture.FILE


class FakeStore:
    """The record API as the renderer uses it, over dicts and a folder. A
    file's id is its path, as the server's is, so a rename gives it a new one."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data: dict[str, dict[str, dict]] = {}
        self.n = 0
        self.trash: list[str] = []

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
        record = self.record(model, record_id)
        if model == FILE and "name" in fields and fields["name"] != record["fields"]["name"]:
            # Renamed: the file moves, and its id is its new path.
            old = Path(record["fields"]["local"])
            new = old.with_name(fields["name"])
            old.rename(new)
            del self.data[model][record_id]
            return self.file(new, record["fields"]["folder"])
        record["fields"].update(fields)
        return record

    def delete(self, model: str, record_id: str) -> None:
        self.record(model, record_id)
        if model == FILE:
            self.trash.append(record_id)
        del self.data[model][record_id]

    def file(self, path: Path, folder: str = "Photos") -> dict:
        fields = {"share": "my-files", "folder": folder, "name": path.name, "kind": "file", "size": path.stat().st_size,
                  "modified": str(path.stat().st_mtime_ns), "local": str(path)}  # fmt: skip
        return self.add(FILE, fields, id_=f"f_{folder}/{path.name}")

    def path_of(self, file: dict) -> Path:
        return Path(file["fields"]["local"])

    def upload(self, share: str, folder: str, name: str, source: Path) -> dict:
        target = self.root / "drive" / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
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
    (tmp_path / "drive" / "Photos").mkdir(parents=True, exist_ok=True)
    file = store.file(photo(tmp_path / "drive" / "Photos" / "beach.jpg"))
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


def test_layers_are_drawn_in_order_words_marks_and_pieces(store, renderer, tmp_path):
    edit = an_edit(store, tmp_path)
    store.add(
        LAYER,
        {
            "edit": edit["id"],
            "kind": "text",
            "text": "WWWW",
            "x": 10,
            "y": 50,
            "size": 30,
            "colour": "#ffffff",
            "order": 2,
        },
    )
    # A thick green mark across the blue half, under the words.
    store.add(
        LAYER,
        {
            "edit": edit["id"],
            "kind": "mark",
            "points": [[60, 92], [95, 92]],
            "x": 60,
            "y": 92,
            "colour": "#008000",
            "width": 10,
            "order": 1,
        },
    )
    # A piece of the red half put over the blue, at the top right.
    store.add(
        LAYER,
        {
            "edit": edit["id"],
            "kind": "piece",
            "source": {"x": 5, "y": 20, "w": 20, "h": 20},
            "x": 75,
            "y": 15,
            "w": 20,
            "h": 20,
            "order": 0,
        },
    )
    # A hidden layer is kept, not drawn.
    store.add(
        LAYER,
        {
            "edit": edit["id"],
            "kind": "mark",
            "points": [[10, 90], [40, 90]],
            "x": 10,
            "y": 90,
            "colour": "#ffff00",
            "width": 8,
            "hidden": True,
            "order": 3,
        },
    )
    data = renderer.picture_jpeg(edit["id"], 400)
    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        box = image.crop((40, 150, 200, 240))
        assert any(
            near(box.getpixel((x, y)), WHITE, 30) for x in range(0, box.width, 2) for y in range(0, box.height, 2)
        )
    assert near(pixel(data, 0.78, 0.92), GREEN) and near(pixel(data, 0.85, 0.25), RED)
    assert near(pixel(data, 0.25, 0.9), RED)
    # A see-through highlighter lets the picture show through.
    store.add(
        LAYER,
        {
            "edit": edit["id"],
            "kind": "mark",
            "points": [[60, 40], [95, 40]],
            "x": 60,
            "y": 40,
            "colour": "#ffffff",
            "width": 10,
            "opacity": 0.4,
            "order": 4,
        },
    )
    data = renderer.picture_jpeg(edit["id"], 400)
    r, g, b = pixel(data, 0.8, 0.4)
    assert b > 200 and 60 < r < 160


def test_a_picture_of_a_missing_edit_or_photo_is_refused(store, renderer, tmp_path):
    with pytest.raises(picture.Refused, match="no such edit"):
        renderer.picture_jpeg("nope", 100)
    edit = store.add(EDIT, {"name": "Lost", "source": "gone"})
    with pytest.raises(picture.Refused, match="no such file"):
        renderer.picture_jpeg(edit["id"], 100)


# -- saving ----------------------------------------------------------------------------------
def test_saving_beside_the_photo_makes_a_new_file_by_its_name(store, renderer, tmp_path):
    edit = an_edit(
        store,
        tmp_path,
        rotate=90,
        crop={"x": 0, "y": 0, "w": 50, "h": 50},
        out_width=60,
        overwrite=False,
        save_name="Beach small.png",
    )
    store.add(LAYER, {"edit": edit["id"], "kind": "text", "text": "Hi", "x": 5, "y": 5, "size": 20})
    renderer.save_queued()
    fields = edit["fields"]
    assert fields["status"] == "done", fields["message"]
    made = store.record(FILE, fields["output"])["fields"]
    assert made["folder"] == "Photos" and made["name"] == "Beach small.png"
    with Image.open(made["local"]) as out:
        # Turned: 300 × 400; half each way: 150 × 200; 60 wide keeps the shape.
        assert out.size == (60, 80) and out.format == "PNG"
    assert "60 × 80" in fields["message"]
    # The edit keeps its layers and its crop: nothing was written over.
    assert fields["rotate"] == 90 and len(store.records(LAYER, edit=edit["id"])) == 1
    # Saved again by the same name, the second does not take the first's.
    fields.update(status="queued")
    renderer.save_queued()
    assert store.record(FILE, fields["output"])["fields"]["name"] == "Beach small 2.png"
    # A name without a suffix takes the photo's; one nothing here writes becomes a JPEG.
    fields.update(status="queued", save_name="Plain")
    renderer.save_queued()
    assert store.record(FILE, fields["output"])["fields"]["name"] == "Plain.jpg"
    fields.update(status="queued", save_name="Odd.gif")
    renderer.save_queued()
    assert store.record(FILE, fields["output"])["fields"]["name"] == "Odd.jpg"


def test_saving_over_the_photo_keeps_its_name_trashes_the_old_one_and_resets_the_edit(store, renderer, tmp_path):
    edit = an_edit(
        store, tmp_path, rotate=90, crop={"x": 0, "y": 0, "w": 50, "h": 50}, overwrite=True, save_name="beach.jpg"
    )
    store.add(LAYER, {"edit": edit["id"], "kind": "text", "text": "Hi", "x": 5, "y": 5, "size": 20})
    old_id = edit["fields"]["source"]
    renderer.save_queued()
    fields = edit["fields"]
    assert fields["status"] == "done", fields["message"]
    # The saved file has the photo's name, so the photo's id; the one before went to the Trash.
    assert fields["output"] == old_id and fields["source"] == old_id
    assert store.trash == [old_id]
    made = store.record(FILE, old_id)["fields"]
    assert made["name"] == "beach.jpg"
    with Image.open(made["local"]) as out:
        assert out.size == (150, 200) and out.format == "JPEG"
    # The edit starts afresh on the saved picture: no turn, no crop, no layers, the new size.
    assert (fields["rotate"], fields["mirror"], fields["crop"], fields["out_width"]) == (0, False, None, None)
    assert (fields["width"], fields["height"]) == (150, 200)
    assert store.records(LAYER, edit=edit["id"]) == []
    assert "Trash" in fields["message"]


def test_a_photo_that_cannot_be_written_is_saved_beside_as_a_jpeg(store, renderer, tmp_path):
    (tmp_path / "drive" / "Photos").mkdir(parents=True, exist_ok=True)
    file = store.file(photo(tmp_path / "drive" / "Photos" / "beach.bmp"))
    edit = store.add(
        EDIT, {"name": "Beach", "source": file["id"], "status": "queued", "overwrite": True, "save_name": "beach.bmp"}
    )
    renderer.save_queued()
    fields = edit["fields"]
    assert fields["status"] == "done", fields["message"]
    assert store.record(FILE, fields["output"])["fields"]["name"] == "beach.jpg"
    # The BMP is still there, untouched.
    assert store.trash == [] and store.record(FILE, file["id"])["fields"]["name"] == "beach.bmp"


def test_a_failed_save_says_why(store, renderer):
    edit = store.add(EDIT, {"name": "Lost", "source": "gone", "status": "queued", "overwrite": True})
    renderer.save_queued()
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


def test_a_hidden_layer_keeps_its_elements_off_the_picture(store, renderer, tmp_path):
    """Layers are groups: an element on a hidden layer is not drawn, and layers
    go lowest first whatever the elements' own order says."""
    file = store.file(photo(tmp_path / "flat.jpg"), "Photos")
    edit = store.add(EDIT, {"name": "Flat", "source": file["id"], "status": "editing",
                            "layers": [{"id": "a", "name": "Under", "hidden": False}, {"id": "b", "name": "Over", "hidden": True}]})  # fmt: skip
    store.add(LAYER, {"edit": edit["id"], "kind": "piece", "layer": "b", "order": 0, "source": {"x": 0, "y": 0, "w": 50, "h": 50}, "x": 0, "y": 0, "w": 100, "h": 100})  # fmt: skip
    assert renderer.layers(store.record(EDIT, edit["id"])) == [], "the piece is on a hidden layer"
    store.patch(EDIT, edit["id"], {"layers": [{"id": "b", "name": "Over", "hidden": False}, {"id": "a", "name": "Under", "hidden": False}]})  # fmt: skip
    store.add(LAYER, {"edit": edit["id"], "kind": "text", "text": "Top", "layer": "a", "order": 9, "x": 1, "y": 1})
    kinds = [f["kind"] for f in renderer.layers(store.record(EDIT, edit["id"]))]
    assert kinds == ["piece", "text"], "the layer's place comes before the element's order"
