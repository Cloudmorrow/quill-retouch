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

EDIT, TEXT, FILE = "retouch.edit", "retouch.text", "file"


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
    assert editor["texts"] == TEXT and editor["pictures"] == "pictures" and editor["export"] == "export"
    assert editor["media"] == {"share": "my-files", "within": "Photos"}
    # The edits come first: Retouch opens on them, and `cm retouch` has the photo kit's words.
    # Looking through the photos is Media's, which Retouch brings with it.
    assert [(s["id"], s["label"]) for s in manifest["screens"]] == [("editor", "Retouch")]
    assert manifest["requires"]["quills"] == ["media"]
    assert [a["id"] for a in manifest["apis"]] == ["pictures"] and manifest["apis"][0]["service"] == "draw"


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
    assert q.act("edit-photo", photo, name="Dinner").toast == "Editing Dinner"
    assert q.act("edit-photo", photo).toast == "Editing IMG_0001"


def test_only_a_photo_can_be_edited(q):
    notes = a_file(q, "notes.txt", "Documents")
    assert q.act("edit-photo", notes).error == "notes.txt is not a photo"
    assert q.list(EDIT) == []


def test_export_queues_the_edit_for_the_renderer(q):
    photo = a_file(q)
    q.act("edit-photo", photo)
    (edit,) = q.list(EDIT)
    done = q.act("export", edit, format="png", name="Beach at dusk")
    assert done.ok and "Retouch Exports" in done.toast
    (edit,) = q.list(EDIT)
    assert (edit["status"], edit["format"], edit["export_name"]) == ("queued", "png", "Beach at dusk")
    assert "PNG" in edit["message"]
    # Queued, it is not queued twice.
    assert "already" in q.act("export", edit).error


def test_an_export_needs_a_format_it_knows(q):
    photo = a_file(q)
    edit = q.seed(EDIT, name="Beach", source=photo.id)
    # The form refuses it before the code runs; the code would too.
    assert "jpeg, png, webp" in q.act("export", edit, format="bmp").error


def test_texts_go_with_their_edit(q):
    photo = a_file(q)
    q.act("edit-photo", photo)
    (edit,) = q.list(EDIT)
    q.seed(TEXT, edit=edit.id, text="Summer", x=10, y=80, size=8, colour="#f0a81e")
    assert [t["text"] for t in q.list(TEXT, edit=edit.id)] == ["Summer"]
    q.delete(EDIT, edit.id)
    assert q.list(TEXT) == []


def test_refresh_draws_again(q):
    assert q.act("refresh").ok
