"""Retouch's tests: the real record store, files and gate, and quill.py, on this machine.

`cm quill test` runs them; `cm quill test --sandbox` runs the same tests
with quill.py in the sandbox, as a server runs it. The renderer (a service)
is tested on its own in test_picture.py.
"""

import contextlib
import tomllib
from pathlib import Path

import pytest
from cloudmorrow.quill.testing import Harness

EDIT, LAYER, FILE = "retouch.edit", "retouch.layer", "file"


@pytest.fixture()
def q():
    with Harness(".") as harness:
        yield harness


def a_file(q, name="beach.jpg", folder="Photos"):
    """A file in My Files, put there the way an upload is: a few bytes with the right name is enough for the records."""
    parts = folder.split("/")
    for depth in range(1, len(parts) + 1):
        with contextlib.suppress(Exception):  # there already
            q.seed(FILE, share="my-files", path="/".join(parts[:depth]), kind="folder")
    part = q._tmp / name
    part.write_bytes(b"\xff\xd8\xff\xe0")
    q.state.records.put(q._principal(), FILE, {"share": "my-files", "folder": folder, "name": name}, part)
    return next(f for f in q.list(FILE, share="my-files", folder=folder) if f["name"] == name)


def test_the_photo_screen_and_its_datamodels_check_out():
    manifest = tomllib.loads(Path("quill.toml").read_text())
    (editor,) = [s for s in manifest["screens"] if s.get("kit") == "photo"]
    assert editor["layers"] == LAYER and editor["pictures"] == "pictures" and editor["save"] == "save"
    assert "export" not in editor and editor["media"] == {"share": "my-files", "within": "Photos"}
    # The edits come first: Retouch opens on them, and `cm retouch` has the photo kit's words.
    # Looking through the photos is Media's, which Retouch brings with it.
    assert [(s["id"], s["label"]) for s in manifest["screens"]] == [("editor", "Retouch")]
    assert manifest["requires"]["quills"] == ["media"]
    assert [a["id"] for a in manifest["apis"]] == ["pictures"] and manifest["apis"][0]["service"] == "draw"
    save = next(a for a in manifest["actions"] if a["id"] == "save")
    assert set(save["fields"]) == {"overwrite", "name"}


def test_edit_this_photo_starts_an_edit_and_opens_it(q):
    photo = a_file(q)
    done = q.act("edit-photo", photo)
    assert done.ok and done.toast == "Editing beach"
    (edit,) = q.list(EDIT)
    assert edit["name"] == "beach" and edit["source"] == photo.id and edit["status"] == "editing"
    assert edit["width"] is None and edit["height"] is None
    go = next(e for e in done.effects if e["effect"] == "go")
    assert go["screen"] == "editor" and go["params"] == {"edit": edit.id}


def test_an_edit_may_be_named(q):
    photo = a_file(q, "IMG_0001.HEIC")
    assert q.act("edit-photo", photo).toast == "Editing IMG_0001"


def test_only_a_photo_can_be_edited(q):
    """The button is a photo's alone (`only`, by the file's name): the server
    refuses the press on anything else before the code runs, and the code
    says no too, for a photo-named file that is not one."""
    notes = a_file(q, "notes.txt", "Documents")
    refused = q.act("edit-photo", notes)
    assert refused.refused and refused.message == "Edit this photo is not for this file"
    assert q.list(EDIT) == []
    assert q.act("edit-photo", a_file(q, "Holiday.JPG")).ok, "a photo by its name, whatever its case"


def test_save_over_the_photo_queues_the_edit_for_the_renderer(q):
    photo = a_file(q)
    q.act("edit-photo", photo)
    (edit,) = q.list(EDIT)
    done = q.act("save", edit, overwrite=True, name="beach.jpg")
    assert done.ok and "Trash" in done.toast
    (edit,) = q.list(EDIT)
    assert (edit["status"], edit["overwrite"], edit["save_name"]) == ("queued", True, "beach.jpg")
    assert "over the photo" in edit["message"]
    # Queued, it is not queued twice.
    assert "already" in q.act("save", edit, overwrite=False, name="x.png").error


def test_save_as_a_new_file_needs_a_plain_name(q):
    photo = a_file(q)
    q.act("edit-photo", photo)
    (edit,) = q.list(EDIT)
    assert q.act("save", edit, overwrite=False, name="").error == "Give the new file a name"
    assert "slashes" in q.act("save", edit, overwrite=False, name="Other/beach.png").error
    done = q.act("save", edit, overwrite=False, name="beach edited.png")
    assert done.ok and done.toast == "Saving as beach edited.png, beside the photo"
    (edit,) = q.list(EDIT)
    assert (edit["status"], edit["overwrite"], edit["save_name"]) == ("queued", False, "beach edited.png")


def test_layers_go_with_their_edit(q):
    photo = a_file(q)
    q.act("edit-photo", photo)
    (edit,) = q.list(EDIT)
    q.seed(LAYER, edit=edit.id, kind="text", text="Summer", x=10, y=80, size=8, colour="#f0a81e")
    q.seed(LAYER, edit=edit.id, kind="mark", points=[[1, 1], [5, 5]], x=1, y=1, colour="#e0323c", width=2)
    q.seed(LAYER, edit=edit.id, kind="piece", source={"x": 0, "y": 0, "w": 20, "h": 20}, x=40, y=40, w=20, h=20)
    assert sorted(layer["kind"] for layer in q.list(LAYER, edit=edit.id)) == ["mark", "piece", "text"]
    q.delete(EDIT, edit.id)
    assert q.list(LAYER) == []


def test_refresh_draws_again(q):
    assert q.act("refresh").ok
