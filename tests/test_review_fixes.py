"""Regression tests for the issues found in the multi-agent review of the MCP server PR.

Each test names the finding it pins down (C = correctness, S = security,
M = MCP client behavior, R = regressions).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile

import pytest
from conftest import FOLDER_ID, LEGACY_ID, NOTEBOOK_ID, PDF_ID, make_pdf, make_v5_lines
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, RectangleObject

from remarkable_demystified import xochitl
from remarkable_demystified.errors import InvalidRequest, NotFound
from remarkable_demystified.rm import pdf as rmpdf
from remarkable_demystified.rm import scene, write
from remarkable_demystified.storage.base import DocChange

mcp = pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

from remarkable_demystified.server.mcp_server import build_server, tile_tops  # noqa: E402


def call(server, name, args=None):
    async def main():
        async with Client(server) as c:
            return await c.call_tool(name, args or {})

    return asyncio.run(main())


def list_tools(server):
    async def main():
        async with Client(server) as c:
            return (await c.list_tools()).tools

    return asyncio.run(main())


def images(result):
    assert not result.is_error, result.content[0].text
    return [Image.open(io.BytesIO(base64.b64decode(b.data))).convert("L") for b in result.content if b.type == "image"]


def dark_rows(img, threshold=128):
    w, h = img.size
    px = img.load()
    return [y for y in range(h) if any(px[x, y] < threshold for x in range(0, w, 2))]


# -- view_pages: nothing cut off ------------------------------------------------------


def test_C1_tile_tops_cover_the_whole_image():
    assert tile_tops(1951, 1872) == [0]  # slightly scrolled page: one tile, kept whole
    assert tile_tops(3681, 1872) == [0, 1808]  # no 65px sliver tile at the end
    assert tile_tops(5000, 1872)[-1] < 5000
    assert tile_tops(100, 10) == [0]  # absurd page heights cannot produce zero steps (S4)


def test_C1_bottom_of_slightly_scrolled_page_is_visible(library, xochitl_dir):
    page = make_v5_lines([(4, 0, [(100, 100), (900, 100)]), (4, 0, [(100, 1950), (900, 1950)])])
    (xochitl_dir / LEGACY_ID / "l-1.rm").write_bytes(page)
    imgs = images(call(build_server(library), "view_pages", {"document_path": "/Legacy sketch"}))
    assert len(imgs) == 1
    rows = dark_rows(imgs[0])
    assert rows[0] < imgs[0].height * 0.1 and rows[-1] > imgs[0].height * 0.9  # both lines present


def test_C1_ink_above_the_top_edge_is_visible(library, xochitl_dir):
    page = make_v5_lines([(4, 0, [(100, -60), (900, -60)]), (4, 0, [(100, 1850), (900, 1850)])])
    (xochitl_dir / LEGACY_ID / "l-1.rm").write_bytes(page)
    img = images(call(build_server(library), "view_pages", {"document_path": "/Legacy sketch"}))[0]
    rows = dark_rows(img)
    assert rows[0] < 20 and rows[-1] > img.height - 60


def test_C2_ink_below_a_pdf_page_reaches_the_model(library, xochitl_dir):
    page = make_v5_lines([(4, 0, [(100, 300), (900, 300)]), (4, 0, [(100, 2300), (900, 2300)])])
    (xochitl_dir / PDF_ID / "p-c.rm").write_bytes(page)
    result = call(build_server(library), "view_pages", {"document_path": "/Books/Paper", "first_page": 3})
    imgs = images(result)
    all_rows = sum((len(dark_rows(i)) > 0 for i in imgs), 0)
    assert all_rows >= 1
    combined_height = sum(i.height for i in imgs)
    assert combined_height > 1568  # taller than one screen: the strip below the page is included
    assert any(dark_rows(i) and dark_rows(i)[-1] > i.height * 0.5 for i in imgs[-1:])


def test_M1_view_pages_reads_the_pdf_once(library):
    reads = []
    original = library.storage.read_file

    def counting(doc_id, name):
        reads.append(name)
        return original(doc_id, name)

    library.storage.read_file = counting
    assert not call(build_server(library), "view_pages",
                    {"document_path": "/Books/Paper", "first_page": 1, "count": 4}).is_error
    assert reads.count(f"{PDF_ID}.pdf") <= 2  # once for page geometry, once for rendering at most


def test_S4_absurd_paper_size_is_ignored():
    from rmscene import CrdtId, LwwValue, read_blocks, write_blocks
    from rmscene.scene_stream import SceneInfo

    blocks = list(read_blocks(io.BytesIO(write.text_page("hi"))))
    blocks.insert(1, SceneInfo(current_layer=LwwValue(CrdtId(0, 0), CrdtId(0, 0)),
                               background_visible=None, root_document_visible=None, paper_size=(1404, 65)))
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    parsed = scene.parse(buf.getvalue())
    assert (parsed.width, parsed.height) == (1404, 1872)


def test_S3_rasterizing_huge_pages_stays_within_budget():
    scale = rmpdf._raster_scale(100, 14400, (2808, 3744))
    assert 100 * 14400 * scale * scale <= rmpdf.MAX_RASTER_PIXELS * 1.0001
    assert rmpdf._raster_scale(612, 792, (1224, 1584)) == pytest.approx(2.0)


# -- PDF export growth -----------------------------------------------------------------


def _export_page(library, n=2):
    return PdfReader(io.BytesIO(library.export_pdf(PDF_ID))).pages[n]


def test_C4_R2_erasers_do_not_grow_pages(library, xochitl_dir):
    page = make_v5_lines([(4, 0, [(100, 300), (900, 300)]), (8, 0, [(100, 1700), (900, 2600)]),
                          (6, 0, [(1300, 900), (1700, 2200)])])
    (xochitl_dir / PDF_ID / "p-c.rm").write_bytes(page)
    assert [float(v) for v in _export_page(library).mediabox] == [0, 0, 612, 792]


def test_C5_short_typed_text_does_not_widen_narrow_pages():
    narrow = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=250, height=1000)
    w.write(narrow)
    hi = scene.parse(write.text_page("Hi"))
    out = PdfReader(io.BytesIO(rmpdf.annotate_pdf(narrow.getvalue(), [(0, hi)]))).pages[0]
    assert float(out.cropbox.width) < 300  # was 500 (the whole text box)


def test_R1_ink_touching_the_edge_does_not_resize(library, xochitl_dir):
    page = make_v5_lines([(4, 0, [(100, 300), (1404, 300)])])  # to the very edge of the screen
    (xochitl_dir / PDF_ID / "p-c.rm").write_bytes(page)
    assert [float(v) for v in _export_page(library).mediabox] == [0, 0, 612, 792]


def test_R1_R3_growth_keeps_boxes_and_hides_cropped_content():
    src = PdfWriter()
    page = src.add_blank_page(width=1224, height=792)
    page.cropbox = RectangleObject([0, 0, 612, 792])
    page[NameObject("/TrimBox")] = RectangleObject([10, 10, 602, 782])
    buf = io.BytesIO()
    src.write(buf)
    ink = scene.parse(make_v5_lines([(4, 0, [(100, 300), (1400, 2200)])]))
    out = PdfReader(io.BytesIO(rmpdf.annotate_pdf(buf.getvalue(), [(0, ink)]))).pages[0]
    assert float(out.cropbox.bottom) < 0  # grew downwards for the ink
    trim = [float(v) for v in out["/TrimBox"]]
    assert trim[1] < 0  # TrimBox kept and enlarged, not deleted
    data = out.get_contents().get_data()
    assert b"612" in data.split(b"W")[0]  # original drawing clipped to the old crop box


# -- paths, folders, uploads ----------------------------------------------------------


def test_S8_ambiguous_names_are_errors_with_ids(library):
    library.create_folder("Paper", FOLDER_ID)  # same name as the document /Books/Paper
    assert library.resolve("/Books/Paper", kind="document").id == PDF_ID
    assert library.resolve("/Books/Paper", kind="folder").type == "folder"
    with pytest.raises(InvalidRequest, match="2 items are called 'Paper'") as exc:
        library.resolve("/Books/Paper")
    assert PDF_ID in str(exc.value)
    result = call(build_server(library), "read_document", {"document_path": "/Books/Paper"})
    assert not result.is_error  # the tool asks for a document, so the document wins


def test_M3_names_containing_slashes_resolve(library):
    nb = library.create_notebook("Meeting 10/10", FOLDER_ID)
    assert library.path(nb.id) == "/Books/Meeting 10/10"
    assert library.resolve("/Books/Meeting 10/10").id == nb.id
    with pytest.raises(NotFound, match="nothing called 'Nope' in /Books"):
        library.resolve("/Books/Nope")


def test_S5_ensure_folder_is_safe_and_batched(library):
    applies = []
    original = library.storage.apply

    def counting(changes):
        changes = list(changes)
        applies.append(len(changes))
        return original(changes)

    library.storage.apply = counting
    deep = library.ensure_folder("/A/B/C")
    assert library.path(deep.id) == "/A/B/C" and applies == [3]  # one write for all three
    assert library.ensure_folder(deep.id).id == deep.id  # ids work
    assert library.ensure_folder("/a/b/c").id == deep.id  # case-insensitive reuse
    for bad in ("/Trash", "/Trash/x", "/" + "/".join("x" * 11), "/a/../b"):
        with pytest.raises(InvalidRequest):
            library.ensure_folder(bad)


def test_S5_upload_file_validates_before_creating_folders(library, tmp_path):
    fake = tmp_path / "fake.pdf"
    fake.write_text("not a pdf")
    server = build_server(library, allow_write=True)
    result = call(server, "upload_file", {"local_path": str(fake), "folder_path": "/New/Folder"})
    assert result.is_error
    with pytest.raises(NotFound):
        library.resolve("/New")


def test_S1_upload_errors_do_not_reveal_paths(library, tmp_path):
    result = call(build_server(library, allow_write=True), "upload_file",
                  {"local_path": str(tmp_path / "secret-dir" / "missing.pdf")})
    assert result.is_error and "missing.pdf" in result.content[0].text
    assert str(tmp_path) not in result.content[0].text


def test_S7_archives_cannot_unpack_beyond_the_limit(library):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.content", "{}")
        zf.writestr("x.metadata", "{}")
        zf.writestr("x/page.rm", b"\0" * 5_000_000)
    with pytest.raises(InvalidRequest, match="unpacks to more than"):
        library.upload(buf.getvalue(), "bomb.rmdoc", max_unpacked=1_000_000)


def test_S6_page_count_is_bounded():
    refs = xochitl.page_refs({"fileType": "epub", "pageCount": 10**9})
    assert len(refs) == xochitl.MAX_PAGES
    assert xochitl.page_refs({"fileType": "pdf", "pageCount": "nope"}) == []


# -- writes against the current file (S2) ----------------------------------------------


def test_S2_patches_apply_to_the_file_as_it_is_now(library, xochitl_dir):
    content_path = xochitl_dir / f"{NOTEBOOK_ID}.content"
    library.items()  # records cached
    content = json.loads(content_path.read_text())
    content["cPages"]["pages"].append({"id": "n-3", "idx": {"timestamp": "1:9", "value": "bz"}})
    content_path.write_text(json.dumps(content))  # the tablet adds a page meanwhile
    library.storage.apply([DocChange(NOTEBOOK_ID, patch={
        xochitl.content_name(NOTEBOOK_ID): lambda c: xochitl.with_tags(c, ["meeting"]),
    })])
    after = json.loads(content_path.read_text())
    assert [t["name"] for t in after["tags"]] == ["meeting"]
    assert any(p["id"] == "n-3" for p in after["cPages"]["pages"])


# -- MCP output shape ------------------------------------------------------------------


def test_M4_browse_is_capped_with_offset(library):
    folder = library.create_folder("Big")
    from remarkable_demystified.storage.base import DocChange as Change

    changes = []
    for i in range(250):
        doc_id = xochitl.new_id()
        changes.append(Change(doc_id, write={
            xochitl.content_name(doc_id): xochitl.dumps(xochitl.new_content("notebook")),
            xochitl.metadata_name(doc_id): xochitl.dumps(xochitl.new_metadata(f"n{i:03}", folder.id)),
        }))
    library.storage.apply(changes)
    server = build_server(library)
    first = json.loads(call(server, "browse", {"path": "/Big"}).content[0].text)
    assert first["total"] == 250 and len(first["items"]) == 200 and "offset=200" in first["note"]
    rest = json.loads(call(server, "browse", {"path": "/Big", "offset": 200}).content[0].text)
    assert len(rest["items"]) == 50 and "note" not in rest


def test_M4_list_pages_is_windowed(library):
    out = json.loads(call(build_server(library), "list_pages",
                          {"document_path": "/Books/Paper", "first_page": 2, "count": 2}).content[0].text)
    assert out["total_pages"] == 4 and out["annotated_pages"] == "1, 3"
    assert [p["page"] for p in out["pages"]] == [2, 3] and "note" in out


def test_M7_read_document_is_plain_text(library):
    result = call(build_server(library), "read_document", {"document_path": "/Notes"})
    assert result.structured_content is None
    tool = next(t for t in list_tools(build_server(library)) if t.name == "read_document")
    assert tool.output_schema is None


def test_M8_device_info_lists_real_tools(library):
    info = json.loads(call(build_server(library), "device_info").content[0].text)
    assert "capabilities" not in info and "path" not in info
    assert "view_pages" in info["tools"] and "trash" not in info["tools"]


def test_M11_schemas_describe_parameters(library):
    tools = {t.name: t for t in list_tools(build_server(library))}
    find = tools["find"].input_schema["properties"]["type"]
    assert {"document", "folder"} <= set(json.dumps(find).replace('"', " ").split())
    count = tools["view_pages"].input_schema["properties"]["count"]
    assert count["maximum"] == 4
    assert "id" in tools["list_pages"].input_schema["properties"]["document_path"]["description"]


# -- --print-config (C3) -----------------------------------------------------------------


def test_C3_print_config_is_self_contained(tmp_path, monkeypatch, capsys):
    from remarkable_demystified.cli import main

    for key in list(__import__("os").environ):
        if key.startswith("RMD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfg = tmp_path / "rmd.toml"
    cfg.write_text('[ssh]\nhost = "192.168.1.42"\npassword = "secret"\nport = 2222\n')
    monkeypatch.setenv("RMD_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "keys").mkdir()
    assert main(["mcp", "--print-config", "--key", "keys/id_rm"]) == 0
    out = capsys.readouterr().out
    env = json.loads(out)["mcpServers"]["remarkable"]["env"]
    assert env["RMD_CONFIG"] == str(cfg.resolve())
    assert env["RMD_SSH_KEY"] == str((tmp_path / "keys" / "id_rm").resolve())
    assert env["RMD_SSH_PORT"] == "2222" and env["RMD_SSH_HOST"] == "192.168.1.42"
    assert env["RMD_KNOWN_HOSTS"].startswith(str(tmp_path / "xdg"))
    assert "secret" not in out


def test_S9_strict_host_key_setting(monkeypatch, tmp_path):
    from remarkable_demystified.config import load_settings

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("RMD_SSH_STRICT_HOST_KEY", "yes")
    assert load_settings().ssh_strict_host_key is True
    monkeypatch.setenv("RMD_SSH_STRICT_HOST_KEY", "maybe")
    with pytest.raises(InvalidRequest):
        load_settings()


def test_make_pdf_helper_still_used():
    assert make_pdf(1).startswith(b"%PDF")
