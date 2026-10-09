from __future__ import annotations

import io
import json
import zipfile

import pytest
from conftest import FOLDER_ID, LEGACY_ID, NOTEBOOK_ID, PDF_ID, SUBFOLDER_ID, TRASHED_ID, make_pdf
from pypdf import PdfReader

from remarkable_demystified import xochitl
from remarkable_demystified.errors import InvalidRequest, NotFound
from remarkable_demystified.rm import scene


def names(items):
    return [i.name for i in items]


# -- listing -----------------------------------------------------------------


def test_items_hide_trash_and_tombstones(library):
    assert names(library.items()) == ["Archive", "Books", "Legacy sketch", "Notes", "Paper"]
    with_trash = names(library.items(include_trash=True))
    assert "Old draft" in with_trash
    assert "Gone" not in with_trash


def test_item_fields(library):
    paper = library.get(PDF_ID)
    assert paper.type == "document"
    assert paper.file_type == "pdf"
    assert paper.page_count == 4  # deleted cPages entry skipped
    assert paper.tags == ["research"]
    assert paper.parent == FOLDER_ID
    assert paper.last_opened is not None and paper.last_opened.year == 2023
    notes = library.get(NOTEBOOK_ID)
    assert notes.pinned and notes.file_type == "notebook" and notes.page_count == 2
    assert library.get(FOLDER_ID).is_folder
    d = paper.to_dict()
    assert d["last_opened"].startswith("2023-") and "metadata" not in d


def test_children_and_tree(library):
    assert names(library.children("")) == ["Books", "Legacy sketch", "Notes"]
    assert names(library.children(FOLDER_ID)) == ["Archive", "Paper"]
    assert names(library.children("trash")) == ["Old draft"]
    with pytest.raises(InvalidRequest):
        library.children(PDF_ID)
    tree = library.tree()
    books = next(n for n in tree if n["item"]["name"] == "Books")
    assert [c["item"]["name"] for c in books["children"]] == ["Archive", "Paper"]


def test_paths_and_resolve(library):
    assert library.path(PDF_ID) == "/Books/Paper"
    assert library.path(TRASHED_ID) == "/Trash/Old draft"
    assert library.resolve("/Books/Paper").id == PDF_ID
    assert library.resolve("books/paper").id == PDF_ID  # case-insensitive fallback
    assert library.resolve(PDF_ID).id == PDF_ID
    assert library.resolve("/").id == ""
    assert library.resolve("/Trash/Old draft").id == TRASHED_ID
    with pytest.raises(NotFound):
        library.resolve("/Books/Nope")


def test_search_recent_current(library):
    assert names(library.search("pap")) == ["Paper"]
    assert names(library.search("RESEARCH")) == ["Paper"]
    assert names(library.recent()) == ["Notes", "Paper"]
    current = library.current()
    assert current["item"].id == NOTEBOOK_ID and current["page"] == 1 and current["path"] == "/Notes"


# -- editing -----------------------------------------------------------------


def test_create_folder_and_ensure(library, xochitl_dir):
    folder = library.create_folder("Projects", FOLDER_ID)
    assert library.path(folder.id) == "/Books/Projects"
    meta = json.loads((xochitl_dir / f"{folder.id}.metadata").read_text())
    assert meta["type"] == "CollectionType" and meta["parent"] == FOLDER_ID
    nested = library.ensure_folder("/Books/Projects/2026/Fall")
    assert library.path(nested.id) == "/Books/Projects/2026/Fall"
    assert library.ensure_folder("/Books/Projects/2026/Fall").id == nested.id
    with pytest.raises(InvalidRequest):
        library.create_folder("  ")
    with pytest.raises(InvalidRequest):
        library.create_folder("x", PDF_ID)
    with pytest.raises(InvalidRequest):
        library.create_folder("x", "trash")


def test_upload_pdf_and_epub(library, xochitl_dir):
    doc = library.upload(make_pdf(5), "Lecture 1.pdf", parent=FOLDER_ID)
    assert doc.name == "Lecture 1" and doc.file_type == "pdf" and doc.page_count == 5
    assert (xochitl_dir / f"{doc.id}.pdf").read_bytes().startswith(b"%PDF")
    assert library.source(doc.id)[1] == "pdf"
    assert len(library.pages(doc.id)) == 5  # synthesized from the PDF until the tablet paginates

    epub = library.upload(b"PK\x03\x04fake epub", "book.epub", name="A Book")
    assert epub.file_type == "epub" and epub.name == "A Book"

    with pytest.raises(InvalidRequest):
        library.upload(b"not a pdf", "x.pdf")
    with pytest.raises(InvalidRequest):
        library.upload(b"hello", "notes.txt")


def test_create_notebook_with_text(library, xochitl_dir):
    nb = library.create_notebook("Todo", text="# Today\n- [ ] write API\n- [x] coffee\nplain line")
    assert nb.file_type == "notebook" and nb.page_count == 1
    content = json.loads((xochitl_dir / f"{nb.id}.content").read_text())
    page_id = content["pages"][0]
    page = scene.parse((xochitl_dir / nb.id / f"{page_id}.rm").read_bytes())
    assert [p.style for p in page.text.paragraphs] == ["heading", "checkbox", "checkbox_checked", "plain"]
    assert library.text(nb.id).pages[0].text == "# Today\n- [ ] write API\n- [x] coffee\nplain line\n"

    empty = library.create_notebook("Blank")
    assert library.pages(empty.id)[0].has_annotations is False


def test_update_rename_move_pin_tags(library, xochitl_dir):
    before = json.loads((xochitl_dir / f"{PDF_ID}.metadata").read_text())
    item = library.update(PDF_ID, name="Paper v2", parent="", pinned=True, tags=["a", "research"])
    assert (item.name, item.parent, item.pinned, item.tags) == ("Paper v2", "", True, ["a", "research"])
    after = json.loads((xochitl_dir / f"{PDF_ID}.metadata").read_text())
    assert after["metadatamodified"] is True
    assert after["lastModified"] != before["lastModified"]
    content = json.loads((xochitl_dir / f"{PDF_ID}.content").read_text())
    assert content["tags"][1] == {"name": "research", "timestamp": 1}  # existing tag kept
    assert content["cPages"]  # unrelated content preserved

    with pytest.raises(InvalidRequest):
        library.move(FOLDER_ID, SUBFOLDER_ID)  # into its own child
    with pytest.raises(InvalidRequest):
        library.move(FOLDER_ID, FOLDER_ID)
    with pytest.raises(NotFound):
        library.rename("nope", "x")


def test_trash_restore_delete(library, xochitl_dir):
    library.trash(FOLDER_ID)
    # trashing a folder hides its contents too
    assert "Paper" not in names(library.items())
    assert library.path(PDF_ID) == "/Trash/Books/Paper"
    library.restore(FOLDER_ID)
    assert "Paper" in names(library.items())

    with pytest.raises(InvalidRequest):
        library.delete(FOLDER_ID, permanent=True)  # not empty
    library.delete(FOLDER_ID, permanent=True, recursive=True)
    assert not (xochitl_dir / f"{PDF_ID}.pdf").exists()
    assert not (xochitl_dir / PDF_ID).exists()
    assert not list(xochitl_dir.glob(f"{SUBFOLDER_ID}*"))

    assert library.empty_trash() == 1
    assert not list(xochitl_dir.glob(f"{TRASHED_ID}*"))


def test_archive_roundtrip(library):
    data = library.export_archive(NOTEBOOK_ID)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert f"{NOTEBOOK_ID}/n-1.rm" in zf.namelist()
    assert f"{NOTEBOOK_ID}.local" not in zf.namelist()
    copy = library.upload(data, "Notes.rmdoc", parent=FOLDER_ID, name="Notes copy")
    assert copy.id != NOTEBOOK_ID  # id already taken, so the import is re-keyed
    assert copy.parent == FOLDER_ID and copy.name == "Notes copy" and copy.page_count == 2
    assert library.text(copy.id).pages[0].text == library.text(NOTEBOOK_ID).pages[0].text
    with pytest.raises(InvalidRequest):
        library.upload(b"PK not a zip", "x.rmdoc")


def test_replace_source(library, xochitl_dir):
    library.replace_source(PDF_ID, make_pdf(1), "new.pdf")
    assert len(PdfReader(xochitl_dir / f"{PDF_ID}.pdf").pages) == 1
    assert (xochitl_dir / PDF_ID / "p-a.rm").exists()
    with pytest.raises(InvalidRequest):
        library.replace_source(PDF_ID, b"PK", "x.epub")


# -- reading -------------------------------------------------------------------


def test_pages(library):
    pages = library.pages(PDF_ID)
    assert [(p.id, p.source_page, p.has_annotations) for p in pages] == [
        ("p-a", 0, True), ("p-b", None, False), ("p-c", 1, True), ("p-d", 2, False),
    ]
    assert [p.id for p in library.pages(LEGACY_ID)] == ["l-1"]


def test_text_and_highlights(library):
    pdf_text = library.text(PDF_ID)
    assert pdf_text.pages[0].highlights[0].text == "The reMarkable uses electronic paper"
    notes = library.text(NOTEBOOK_ID)
    assert notes.pages[0].text.startswith("A\nv3.2.2\n")
    assert "# Heading line" in notes.pages[0].text
    md = notes.to_markdown()
    assert md.startswith("# Notes\n") and "## Page 1" in md
    legacy = library.text(LEGACY_ID)
    assert legacy.pages[0].highlights[0].text == "legacy highlight"


@pytest.mark.parametrize("fmt,magic", [("svg", b"<?xml"), ("png", b"\x89PNG"), ("pdf", b"%PDF")])
def test_render_page_formats(library, fmt, magic):
    for doc, page in ((NOTEBOOK_ID, 0), (PDF_ID, 2), (PDF_ID, 1), (LEGACY_ID, 0)):
        data, media = library.render_page(doc, page, fmt, scale=0.25)
        assert data.startswith(magic), (doc, page)
        assert media.split("/")[1].startswith(fmt[:3])
    with pytest.raises(NotFound):
        library.render_page(PDF_ID, 9)
    with pytest.raises(InvalidRequest):
        library.render_page(PDF_ID, 0, "gif")


def test_export_pdf(library):
    annotated = PdfReader(io.BytesIO(library.export_pdf(PDF_ID)))
    assert len(annotated.pages) == 4  # 3 PDF pages + 1 inserted blank page
    assert float(annotated.pages[0].mediabox.width) == 612
    assert b" l\n" in annotated.pages[2].get_contents().get_data()  # strokes drawn on PDF page 2

    notebook = PdfReader(io.BytesIO(library.export_pdf(NOTEBOOK_ID)))
    assert len(notebook.pages) == 2
    text_ops = notebook.pages[0].get_contents().get_data()
    assert b"(Heading line) Tj" in text_ops


def test_legacy_v5_page(library):
    page = library.page_scene(LEGACY_ID, 0)
    assert page.version == 5
    assert [s.tool for s in page.strokes] == ["fineliner", "highlighter"]
    assert page.strokes[0].points[1].x == 300


def test_files_and_read_file(library):
    files = library.files(PDF_ID)
    assert f"{PDF_ID}.pdf" in files and f"{PDF_ID}/p-a.rm" in files
    assert library.read_file(PDF_ID, f"{PDF_ID}.content").startswith(b"{")
    with pytest.raises(InvalidRequest):
        library.read_file(PDF_ID, f"{NOTEBOOK_ID}.content")
    with pytest.raises(NotFound):
        library.read_file(PDF_ID, f"{PDF_ID}.epub")
    with pytest.raises(NotFound):
        library.source(NOTEBOOK_ID)


def test_device_info(library):
    info = library.device_info()
    assert info["backend"] == "local" and info["documents"] == 4 and info["folders"] == 2
    assert "upload" in info["capabilities"]


def test_batch_move(library):
    moved = library.batch_move([NOTEBOOK_ID, LEGACY_ID], SUBFOLDER_ID)
    assert {m.parent for m in moved} == {SUBFOLDER_ID}
    assert library.path(LEGACY_ID) == "/Books/Archive/Legacy sketch"


def test_metadata_helpers_preserve_unknown_keys():
    m = {"visibleName": "a", "futureKey": {"x": 1}, "version": 3}
    out = xochitl.updated_metadata(m, visibleName="b")
    assert out["futureKey"] == {"x": 1} and out["version"] == 4 and m["visibleName"] == "a"


@pytest.mark.parametrize("name", [
    f"{PDF_ID}/../../etc/passwd",
    f"{PDF_ID}.metadata\n/etc/passwd",
    "/etc/passwd",
    f"{NOTEBOOK_ID}.content",
    f"{PDF_ID}/x\x00.rm",
])
def test_unsafe_file_names_rejected(library, name):
    with pytest.raises(InvalidRequest):
        library.read_file(PDF_ID, name)
    assert not xochitl.is_safe_file_name(PDF_ID, name)
