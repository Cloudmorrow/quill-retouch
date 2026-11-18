"""Retouch: the code behind the buttons.

The editor itself is Cloudmorrow's photo kit, declared in quill.toml: edits
of one photo each, and the words laid on them, with the kit's own field
names. This is the rest, run in the sandbox as whoever presses it: starting
an edit from a photo (a button on its sheet, in Media or Files), and asking
for an export. The photo itself is never touched here: services/picture.py,
a program beside the server, draws the pictures, fills in sizes, and
renders the exports.
"""

from cloudmorrow.quill import action, error, go, redraw, toast

EDIT, TEXT, FILE = "retouch.edit", "retouch.text", "file"
EXPORTS = "Retouch Exports"
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif")
FORMATS = {"jpeg": "JPEG", "png": "PNG", "webp": "WebP"}


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


@action("export")
def export(ctx, edit, format="jpeg", name=""):
    if edit.get("status") in ("queued", "rendering"):
        return error("It is being exported already; wait for it to finish")
    if not edit.get("source"):
        return error("This edit has no photo: there is nothing to export")
    if format not in FORMATS:
        return error(f"No such format: {format}")
    ctx.records.patch(
        EDIT,
        edit.id,
        {
            "status": "queued",
            "message": f"Waiting to be exported as {FORMATS[format]}",
            "format": format,
            "export_name": (name or "").strip() or edit["name"],
        },
    )
    return toast(f"Queued: the picture lands in My Files › {EXPORTS}")


@action("refresh")
def refresh(ctx):
    return redraw()
