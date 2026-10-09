"""Shared fixtures: a realistic fake xochitl directory.

The ``.rm`` page files in ``tests/data/rm`` come from rmscene's test suite
(MIT licensed, see ``LICENSE-rmscene``) and were written by real tablets.
"""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path

import pytest
from pypdf import PdfWriter

from remarkable_demystified import xochitl
from remarkable_demystified.library import Library
from remarkable_demystified.storage.local import LocalStorage

DATA = Path(__file__).parent / "data"

FOLDER_ID = "11111111-1111-4111-8111-111111111111"
PDF_ID = "22222222-2222-4222-8222-222222222222"
NOTEBOOK_ID = "33333333-3333-4333-8333-333333333333"
TRASHED_ID = "44444444-4444-4444-8444-444444444444"
LEGACY_ID = "55555555-5555-4555-8555-555555555555"
SUBFOLDER_ID = "66666666-6666-4666-8666-666666666666"


def rm_sample(name: str) -> bytes:
    return (DATA / "rm" / f"{name}.rm").read_bytes()


def make_pdf(pages: int = 3, size: tuple[float, float] = (612, 792)) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=size[0], height=size[1])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_v5_lines(strokes: list[tuple[int, int, list[tuple[float, float]]]]) -> bytes:
    """A firmware 2.x ``.rm`` file: one layer, strokes of (pen, color, points)."""
    out = bytearray(b"reMarkable .lines file, version=5          ")
    out += struct.pack("<i", 1)
    out += struct.pack("<i", len(strokes))
    for pen, color, points in strokes:
        out += struct.pack("<iiifii", pen, color, 0, 2.0, 0, len(points))
        for x, y in points:
            out += struct.pack("<ffffff", x, y, 0.1, 0.2, 2.5, 0.7)
    return bytes(out)


def meta(name: str, parent: str = "", kind: str = xochitl.DOCUMENT, **extra) -> bytes:
    m = {
        "createdTime": "1700000000000",
        "lastModified": "1700000000000",
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "parent": parent,
        "pinned": False,
        "type": kind,
        "visibleName": name,
    }
    m.update(extra)
    return json.dumps(m).encode()


def cpages(page_ids: list[str], redirs: list[int | None] | None = None) -> dict:
    pages = []
    for i, pid in enumerate(page_ids):
        entry = {"id": pid, "idx": {"timestamp": "1:2", "value": "b" + chr(ord("a") + i)},
                 "template": {"timestamp": "1:1", "value": "Blank"}}
        if redirs and redirs[i] is not None:
            entry["redir"] = {"timestamp": "1:2", "value": redirs[i]}
        pages.append(entry)
    # a deleted page that must be skipped
    pages.append({"id": "deleted-page", "idx": {"timestamp": "1:2", "value": "aa"},
                  "deleted": {"timestamp": "1:3", "value": 1}})
    return {"pages": pages, "original": {"timestamp": "0:0", "value": -1}}


def build_library(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)

    def put(name: str, data: bytes) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    # Folders: /Books and /Books/Archive
    put(f"{FOLDER_ID}.metadata", meta("Books", kind=xochitl.COLLECTION))
    put(f"{FOLDER_ID}.content", b'{"tags": []}')
    put(f"{SUBFOLDER_ID}.metadata", meta("Archive", FOLDER_ID, kind=xochitl.COLLECTION))
    put(f"{SUBFOLDER_ID}.content", b"{}")

    # /Books/Paper: an annotated 3-page PDF; tablet page 1 is an inserted blank page.
    put(f"{PDF_ID}.metadata", meta("Paper", FOLDER_ID, lastOpened="1700000500000", lastOpenedPage=2))
    put(f"{PDF_ID}.pdf", make_pdf(3))
    content = {
        "fileType": "pdf", "formatVersion": 2, "pageCount": 4,
        "cPages": cpages(["p-a", "p-b", "p-c", "p-d"], [0, None, 1, 2]),
        "tags": [{"name": "research", "timestamp": 1}],
    }
    put(f"{PDF_ID}.content", json.dumps(content).encode())
    put(f"{PDF_ID}/p-a.rm", rm_sample("Wikipedia_highlighted_p1"))
    put(f"{PDF_ID}/p-c.rm", rm_sample("Lines_v2"))

    # /Notes: a two-page notebook with typed text and handwriting
    put(f"{NOTEBOOK_ID}.metadata", meta("Notes", pinned=True, lastOpened="1700000900000", lastOpenedPage=1))
    put(f"{NOTEBOOK_ID}.content", json.dumps({
        "fileType": "notebook", "formatVersion": 2, "cPages": cpages(["n-1", "n-2"]),
    }).encode())
    put(f"{NOTEBOOK_ID}/n-1.rm", rm_sample("Normal_A_stroke_2_layers_v3.3.2"))
    put(f"{NOTEBOOK_ID}/n-2.rm", rm_sample("More_color_highlight_shader_v3.15.4.2"))
    put(f"{NOTEBOOK_ID}.local", b"{}")

    # A trashed document
    put(f"{TRASHED_ID}.metadata", meta("Old draft", xochitl.TRASH))
    put(f"{TRASHED_ID}.content", json.dumps({"fileType": "notebook", "pages": ["t-1"]}).encode())

    # A firmware 2.x notebook: formatVersion 1 page list and a v5 page
    put(f"{LEGACY_ID}.metadata", meta("Legacy sketch", deleted=False))
    put(f"{LEGACY_ID}.content", json.dumps({"fileType": "notebook", "pages": ["l-1"], "pageCount": 1}).encode())
    put(f"{LEGACY_ID}/l-1.rm", make_v5_lines([(4, 0, [(100, 100), (300, 300), (500, 200)]), (5, 3, [(100, 600), (900, 600)])]))
    put(f"{LEGACY_ID}.highlights/l-1.json", json.dumps(
        {"highlights": [[{"color": 1, "length": 5, "start": 0, "text": "legacy highlight", "rects": []}]]}
    ).encode())

    # A document deleted by old firmware (tombstone metadata): must be invisible
    put("77777777-7777-4777-8777-777777777777.metadata", meta("Gone", deleted=True))
    return root


@pytest.fixture
def xochitl_dir(tmp_path: Path) -> Path:
    return build_library(tmp_path / "xochitl")


@pytest.fixture
def library(xochitl_dir: Path) -> Library:
    return Library(LocalStorage(xochitl_dir))
