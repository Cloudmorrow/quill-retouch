"""Retouch: the code behind the buttons.

The editor itself is Cloudmorrow's photo kit, declared in quill.toml: edits
of one photo each, and the layers laid on them, with the kit's own field
names. This is the rest, run in the sandbox as whoever presses it: starting
an edit from a photo (a button on its sheet, in Media or Files), and asking
for a save. The photo itself is never touched here: services/picture.py, a
program beside the server, draws the pictures, fills in sizes, and does the
saving.
"""

from cloudmorrow.quill import action, error, go, redraw, toast

EDIT, LAYER, FILE = "retouch.edit", "retouch.layer", "file"
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif")


def is_photo(record):
    """Whether a file record is a photo, by its type or its name."""
    if record.get("kind") != "file":
        return False
    name = str(record.get("name") or "").lower()
    mime = str(record.get("mime") or "")
    return mime.startswith("image/") or name.endswith(PHOTO_SUFFIXES)


# -- buttons --------------------------------------------------------------------------------
@action("edit_photo")
def edit_photo(ctx, photo, name=""):
    if not is_photo(photo):
        return error(f"{photo['name']} is not a photo")
    title = (name or "").strip() or str(photo["name"]).rsplit(".", 1)[0] or "Photo"
    edit = ctx.records.create(EDIT, name=title, source=photo.id, status="editing")
    return [toast(f"Editing {title}"), go("editor", edit=edit.id)]


@action("save")
def save(ctx, edit, overwrite=False, name=""):
    if edit.get("status") in ("queued", "rendering"):
        return error("It is being saved already; wait for it to finish")
    if not edit.get("source"):
        return error("This edit has no photo: there is nothing to save")
    name = (name or "").strip()
    if not overwrite and not name:
        return error("Give the new file a name")
    if not overwrite and "/" in name:
        return error("A name has no slashes: the file goes beside the photo")
    ctx.records.patch(
        EDIT,
        edit.id,
        {
            "status": "queued",
            "message": "Waiting to be written over the photo" if overwrite else f"Waiting to be saved as {name}",
            "overwrite": bool(overwrite),
            "save_name": name,
        },
    )
    if overwrite:
        return toast("Saving over the photo; the one before goes to the Trash")
    return toast(f"Saving as {name}, beside the photo")


@action("refresh")
def refresh(ctx):
    return redraw()
