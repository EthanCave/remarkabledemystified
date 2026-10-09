# remarkabledemystified

A Python library, REST API and command-line tool for **reading, editing and
organizing everything on a reMarkable tablet**: list and search your library,
upload PDFs/EPUBs, create notebooks with typed text, rename/move/pin/tag/trash
documents, and export your handwriting and highlights as PDF, PNG, SVG or
Markdown.

It talks to the tablet in whichever way suits you:

| Backend | How it connects | Setup | Can do |
|---|---|---|---|
| `ssh` (default) | SSH over the USB cable (`10.11.99.1`) or Wi-Fi | root password from the tablet | **everything** |
| `cloud` | the reMarkable cloud, from anywhere | one-time code from my.remarkable.com | **everything** (incl. device-sync) |
| `usb` | the tablet's built-in *USB web interface* | toggle one setting | list, download PDF/`.rmdoc`, upload |
| `local` | a copy of the tablet's `xochitl` folder | none | everything (offline / backups) |

> **Back up first.** Write operations change the tablet's own files. A backup is one command:
> `scp -O -r root@10.11.99.1:/home/root/.local/share/remarkable/xochitl ./rm-backup`
> (`-O` selects classic scp, since the tablet may have no SFTP server). You can browse
> the backup later with `--backend local --local-dir ./rm-backup`.

---

## Install

```bash
git clone https://github.com/ethancave/remarkabledemystified
cd remarkabledemystified
pip install -e .            # or: uv pip install -e .
rmd --help
```

Python 3.10+. Optional: install `pymupdf` *or* poppler's `pdftoppm` to draw the
underlying PDF page behind PNG/SVG page renders (PDF exports never need them).

## Connect your tablet

### SSH (recommended, full access)

1. Plug the tablet in with USB (it appears at `10.11.99.1`), or use its Wi-Fi IP.
2. Find the root password: **Settings → Help → Copyrights and licenses**, at the
   bottom of the *GPLv3 Compliance* section (reMarkable 1 and 2).
   *reMarkable Paper Pro / Move:* you must first enable **developer mode**, which
   **factory-resets the tablet**. Sync to the cloud before you do that.
3. Tell `rmd` about it:

   ```bash
   export RMD_SSH_PASSWORD='the-password'     # or put it in the config file
   rmd ls
   ```

   SSH keys work too (`RMD_SSH_KEY=~/.ssh/id_ed25519`, after copying your public
   key into `/home/root/.ssh/authorized_keys` on the tablet).

When something changes over SSH, `rmd` briefly stops the tablet UI (`xochitl`),
writes the files, and starts it again so the tablet sees the change. That is the
standard approach used by community tools; set `RMD_SSH_RESTART=never` to skip it
and run `rmd restart-ui` yourself.

### Cloud (no cable needed)

```bash
# get an 8-letter code from https://my.remarkable.com/device/browser/connect
rmd cloud-login abcdefgh
export RMD_BACKEND=cloud
rmd ls
```

Changes are pushed to your tablets immediately. The cloud protocol is not
officially documented. This implementation follows
[rmapi](https://github.com/ddvk/rmapi) (Aug 2026), and the cloud root is only
swapped after every file is uploaded, so if reMarkable changes the protocol,
writes fail cleanly instead of corrupting your library.

### USB web interface (no password, read-mostly)

On the tablet: **Settings → Storage → USB web interface → on**, then
`export RMD_BACKEND=usb`. You can list, download and upload. Edits return
*Not Supported* (HTTP 501).

### Configuration file

`~/.config/remarkable-demystified/config.toml` (or point `RMD_CONFIG` at one):

```toml
backend = "ssh"            # ssh | cloud | usb | local

[ssh]
host = "10.11.99.1"        # or the tablet's Wi-Fi address
password = "the-password"
# key = "~/.ssh/id_ed25519"
# restart = "never"

[server]
api_token = "a-long-random-string"   # required by the REST API when set
```

Every key has an environment variable (`RMD_BACKEND`, `RMD_SSH_HOST`,
`RMD_SSH_PASSWORD`, `RMD_SSH_KEY`, `RMD_SSH_PORT`, `RMD_SSH_RESTART`,
`RMD_LOCAL_DIR`, `RMD_USB_URL`, `RMD_API_TOKEN`, ...). Precedence: command-line
flag > environment > config file > default.

---

## REST API

```bash
export RMD_API_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(24))")
rmd serve                      # http://127.0.0.1:8000, docs at /docs
rmd serve --bind 0.0.0.0       # reachable from your network: always set a token!
```

Interactive documentation (Swagger UI) is served at **`/docs`**. Send the token as
`Authorization: Bearer <token>` or `X-API-Key: <token>`.

Ids are the tablet's document UUIDs. `""` is the root folder and `"trash"` is the trash.
Use `GET /items/resolve?path=/Books/Dune` to turn a path into an id.

| Method & path | What it does |
|---|---|
| `GET /health` | liveness (no auth, doesn't touch the tablet) |
| `GET /device` | battery, storage, firmware, model (SSH) / cloud generation; capabilities |
| `POST /device/restart-ui` | restart the tablet UI (SSH) |
| `POST /sync` | drop caches and re-read the library |
| `GET /items` | everything; `?parent=<id>` for one folder, `?q=` search, `?type=folder\|document`, `?include_trash=true` |
| `GET /items/tree` | nested folder tree |
| `GET /items/resolve?path=` | look up by path |
| `GET /items/recent` · `GET /items/current` | recently opened · the document + page open right now |
| `GET /items/{id}` | one item |
| `PATCH /items/{id}` | `{"name", "parent", "pinned", "tags"}`: rename, move, favorite, tag |
| `DELETE /items/{id}` | move to trash; `?permanent=true[&recursive=true]` deletes for good |
| `POST /items/{id}/restore` | restore from trash `{"parent": ""}` |
| `POST /items/move` | `{"ids": [...], "parent": "<id>"}`: bulk move |
| `DELETE /trash` | empty the trash |
| `POST /folders` | `{"name", "parent"}` or `{"path": "/A/B/C"}` (creates missing folders) |
| `POST /documents` | multipart upload: `file` (PDF/EPUB/.rmdoc), `parent`, `name` |
| `POST /notebooks` | `{"name", "parent", "text"}`: new notebook, `text` becomes typed text |
| `PUT /documents/{id}/file` | replace a document's PDF/EPUB, keeping annotations |
| `GET /documents/{id}/file` | original PDF/EPUB |
| `GET /documents/{id}/pdf` | **annotated PDF** (handwriting + highlights + typed text) |
| `GET /documents/{id}/archive` | `.rmdoc` archive (re-importable) |
| `GET /documents/{id}/pages` | pages with PDF page mapping and annotation flags |
| `GET /documents/{id}/pages/{n}` | render page `n` (0-based): `?format=svg\|png\|pdf&scale=1` |
| `GET /documents/{id}/text` | typed text + highlighted passages; `?format=markdown` |
| `GET /documents/{id}/files[/{name}]` | the raw xochitl files |

```bash
H="Authorization: Bearer $RMD_API_TOKEN"
curl -H "$H" localhost:8000/items/resolve?path=/Books
curl -H "$H" -F file=@paper.pdf -F parent=<folder-id> localhost:8000/documents
curl -H "$H" -X PATCH -H 'content-type: application/json' -d '{"name":"Read me"}' localhost:8000/items/<id>
curl -H "$H" -o notes.pdf localhost:8000/documents/<id>/pdf
curl -H "$H" "localhost:8000/documents/<id>/text?format=markdown"
curl -H "$H" -H 'content-type: application/json' \
     -d '{"name":"Todo","text":"# Today\n- [ ] email prof\n- [x] lab report"}' localhost:8000/notebooks
```

## Command line

```bash
rmd ls -l /Books                      # list (long format shows type, pages, date, id)
rmd tree                              # whole folder tree
rmd info                              # battery, storage, firmware...
rmd put lecture*.pdf --to /School/Fall --mkdir
rmd get "/Notes/Meeting"              # annotated PDF -> ./Meeting.pdf
rmd get "/Books/Dune" -f original -o ~/Downloads/
rmd get "/Notes/Meeting" -f rmdoc     # full-fidelity archive
rmd text "/Notes/Meeting"             # typed text + highlights as Markdown
rmd render "/Notes/Meeting" --page 2 -o page2.png
rmd note "Shopping" --text "# Shopping\n- [ ] eggs" --to /Lists
rmd note "Lecture notes" --file notes.md
rmd mv "/Inbox/paper" /Papers         # move into a folder
rmd mv "/Papers/paper" "/Papers/Smith 2024"   # rename
rmd pin "/Papers/Smith 2024"
rmd tag "/Papers/Smith 2024" ml thesis
rmd rm "/Papers/old"                  # to trash   (--permanent to delete)
rmd restore "/Trash/old" --to /Papers
rmd empty-trash
```

Add `--backend cloud|usb|local`, `--host`, `--password` to any command.

## Python

```python
from remarkable_demystified import connect

rm = connect()                                   # uses your config / env
# rm = connect(backend="ssh", ssh_password="...")

books = rm.resolve("/Books")
for item in rm.children(books.id):
    print(item.name, item.file_type, item.page_count)

doc = rm.upload(open("paper.pdf", "rb").read(), "paper.pdf", parent=books.id)
rm.update(doc.id, name="Smith et al. 2024", pinned=True, tags=["thesis"])

notes = rm.resolve("/Notes/Meeting")
open("meeting.pdf", "wb").write(rm.export_pdf(notes.id))
print(rm.text(notes.id).to_markdown())
png, _ = rm.render_page(notes.id, 0, "png", scale=0.5)

rm.create_notebook("Todo", text="# Today\n- [ ] finish API\n- [x] coffee")
```

---

## How the tablet stores things (the "demystified" part)

Every document **and** folder is a UUID with a family of files, both on the
tablet (`/home/root/.local/share/remarkable/xochitl/`) and in the cloud:

| File | Contents |
|---|---|
| `<id>.metadata` | JSON: `visibleName`, `type` (`DocumentType`/`CollectionType`), `parent` (`""` root, `"trash"`), `pinned`, timestamps |
| `<id>.content` | JSON: `fileType` (`pdf`/`epub`/`notebook`), page list, tags, view settings |
| `<id>.pdf` / `<id>.epub` | the original file |
| `<id>/<page-id>.rm` | strokes, highlights and typed text for one page |
| `<id>.thumbnails/`, `<id>.highlights/`, `<id>.pagedata`, `<id>.local` | thumbnails, 2.x highlights, templates, device-only state |

**Page order.** Firmware 3.x keeps pages in `cPages.pages`: a CRDT list ordered
by the `idx` string, where deleted pages carry `deleted`, and `redir` maps a page
to the page of the original PDF. Pages you insert into a PDF have no `redir`.
Older firmware used a plain `pages` array plus `redirectionPageMap`.
(`xochitl.page_refs`.)

**`.rm` files.** There are three versions:
* **v3/v5** (firmware 1.x–2.x): layers → strokes → points of six floats
  `x, y, speed, tilt, width, pressure`. Origin is the top-left corner.
* **v6** (firmware 3.x): a tagged-block CRDT "scene tree" (parsed with
  [rmscene](https://github.com/ricklupton/rmscene)) of layers, strokes, typed
  text and PDF highlights. **`x = 0` is the horizontal centre of the page.**
  Handwriting next to typed text lives in groups *anchored to a character*, with
  coordinates relative to that line's baseline. The baseline offset (33.6 px
  below the text box's `pos_y`) was measured here by comparing the same
  strokes saved before and after anchoring (`Lines_v2.rm` vs
  `Lines_v2_updated.rm`).

**PDF placement.** The tablet scales a PDF page to fit its 1404×1872 px screen
(`k = min(1404/w, 1872/h)` px per point), centred horizontally in v6. Exports
invert that transform and draw strokes as vector paths onto the original page
(`rm/pdf.py`).

**Cloud sync.** Everything is a content-addressed blob. Each document has an
index blob listing `hash:0:filename:0:size` lines. A root index (schema 4,
sorted by id) lists every document. The root pointer carries a generation
number, so a write first uploads files, then document indexes, then the new
root, and finally swaps the pointer if the generation is unchanged (HTTP 412
means the tablet synced first: re-read and retry). Full description in
`storage/cloud.py`.

## Limitations

* Rendering is a close approximation, not pixel-perfect: pen textures (pencil
  grain, brush) are simplified, and typed-text wrapping uses estimated glyph widths.
* Editing existing handwriting isn't supported. You can create notebooks with
  typed text, and add, replace and reorganize documents.
* PNG/SVG renders show the PDF page underneath only if PyMuPDF or poppler is installed.
* The cloud and USB web interfaces are undocumented by reMarkable and may change.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest              # 68 tests; SSH tests start a throwaway sshd if OpenSSH server is installed
ruff check src tests && mypy src
```

The tests run against a realistic fake library: real `.rm` pages written by
tablets (from rmscene's test suite, MIT, see `tests/data/rm/LICENSE-rmscene`),
a strict fake cloud server (`tests/fake_cloud.py`) that enforces hashes, sort
order, CRC32C headers and generations, a fake USB web interface, and a real
OpenSSH server with a fake `systemctl`.

Built on the shoulders of [rmscene](https://github.com/ricklupton/rmscene),
[rmc](https://github.com/ricklupton/rmc) and [rmapi](https://github.com/ddvk/rmapi).
