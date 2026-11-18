# Retouch

A basic photo editor for the photos in your own drive: crop, turn, mirror,
make a picture another size, lay words on it, and save a new file. It is a
[Quill](https://github.com/Cloudmorrow/cloudmorrow/blob/main/docs/QUILLS.md)
for Cloudmorrow: the editor is Cloudmorrow's photo kit, drawn on the phone,
in the web app, in the terminal and on the command line by Cloudmorrow
itself, and the pictures and exports are made beside your own server, with
the Pillow it already has. Nothing leaves it, and the original photo is
never changed.

## How it works

1. Open **Retouch**: it starts on your edits. The new button shows the
   photos in My Files › Photos (or any other drive you have) and starts an
   edit of the one you pick. Or start from a photo in **Media** (or Files):
   **Edit this photo** on it does the same.
2. In the editor, **Left** and **Right** turn the picture by a quarter,
   **Mirror** flips it.
3. **Crop** shows a box on the picture: drag it, pull its corners, or hold
   it to a shape — square, 4 : 3, 3 : 2, 16 : 9, tall — and press Done.
   Keep all of it undoes the crop.
4. **Text** lays words on the picture, in the app's own letters with a thin
   dark edge so they read on anything. Drag them where they should be,
   pull their corner to make them bigger or smaller, type the words and
   pick a colour in the panel. Add as many as you like.
5. **Size** says what the export will be: as it is, half, a quarter, a
   usual width, or any width and height you type. One of them keeps the
   shape.
6. **Export**: JPEG (small, for photos), PNG (exact) or WebP, under the
   name you give it. The file lands in My Files › **Retouch Exports**, and
   Open shows it.

In the terminal the picture is the real one, drawn as it will export, and
every step is a key: r and R turn, m mirrors, c crops (C keeps all), s sizes,
t writes, k moves the words, x takes them off, e exports. On the command
line, `cm retouch add beach.jpg`, `turn`, `mirror`, `crop`, `size`, `text`,
`move`, `rm`, `picture` and `export` do the same.

## What it adds to your Cloudmorrow

| | |
| --- | --- |
| Datamodels | uses the foundational `file`; introduces `retouch.edit` (one photo being edited: its turn, mirror, crop, export size, export settings and the status of its export) and `retouch.text` (words on an edit: where, how tall, which colour) |
| Screens | Retouch (the edits and the editor: the photo kit) — on the phone, the web app, the terminal, `cm retouch`, and to your assistant |
| Actions | Edit this photo (on a photo's sheet, in Media or Files), Export (format, name), Refresh |
| Quills | requires [Media](https://github.com/Cloudmorrow/quill-media), which shows the photos and videos in your drive and is installed with Retouch |
| Services, APIs | `draw`: a program beside the server (`services/picture.py`), kept running, with Pillow. It serves the `pictures` API the terminal's viewer draws from (`/api/q/retouch/pictures/info`, `/source`, `/picture`), fills in the sizes the editor leaves blank, and renders the exports, through the record API |

## What it needs

**Media**, which comes with the install: the manifest requires the `media`
Quill (`[requires] quills`), so installing Retouch installs Media first,
from the catalog, and Media cannot be removed while Retouch is here. Media
works without Retouch.

**Nothing else.** The renderer uses Pillow, which the server's own Python
has for its thumbnails, and the app's own font files, which the core
ships. The words are drawn in Barlow's Latin letters; other scripts fall
back to Pillow's own letters. A photo the server cannot read as a picture
(a HEIC without the plug-in, say) is still listed, but the web app says it
cannot draw it.

The renderer runs as the administrator who installed Retouch, the way
Quill services do today, so it draws and exports the edits in *their*
drive. Other people's edits wait for services that run for everyone.

## Working on it

See [CLAUDE.md](CLAUDE.md). In short: `cm quill check`, `cm quill test`
(and `--sandbox`), then `cm quill dev --local`. The renderer is tested
without a server in `tests/test_picture.py`, on pictures Pillow makes,
down to the pixels of an export. It needs a core with the photo kit
(Cloudmorrow 0.101.0 or later).
