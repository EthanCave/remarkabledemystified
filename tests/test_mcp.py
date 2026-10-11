"""The MCP server, exercised through the real MCP client (in-process and over stdio)."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys

import httpx
import pytest
from conftest import LEGACY_ID, NOTEBOOK_ID, PDF_ID, make_pdf, make_v5_lines
from PIL import Image

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402

from remarkable_demystified.server.mcp_server import (  # noqa: E402
    build_server,
    parse_page_selection,
)
from remarkable_demystified.storage.usbweb import UsbWebLibrary, UsbWebStorage  # noqa: E402

READ_TOOLS = {"browse", "find", "recent", "current", "list_pages", "read_document", "view_pages", "device_info"}
WRITE_TOOLS = {"create_notebook", "create_folder", "organize", "move_many", "trash", "restore", "upload_file"}


def run(server, fn):
    async def main():
        async with Client(server) as client:
            return await fn(client)

    return asyncio.run(main())


def call(server, name, args=None):
    return run(server, lambda c: c.call_tool(name, args or {}))


def data(result):
    assert not result.is_error, result.content[0].text
    assert len(result.content) == 1  # one JSON object, never one block per list item
    parsed = json.loads(result.content[0].text)
    assert parsed == result.structured_content
    return parsed


def text(result):
    assert not result.is_error, result.content[0].text
    return result.content[0].text


def images(result):
    assert not result.is_error, result.content[0].text
    return [Image.open(io.BytesIO(base64.b64decode(b.data))) for b in result.content if b.type == "image"]


# -- tool set ------------------------------------------------------------------


def test_read_only_by_default(library):
    tools = run(build_server(library), lambda c: c.list_tools()).tools
    assert {t.name for t in tools} == READ_TOOLS
    assert all(t.annotations.read_only_hint for t in tools)


def test_write_tools_are_opt_in_and_never_delete(library):
    tools = run(build_server(library, allow_write=True), lambda c: c.list_tools()).tools
    names = {t.name for t in tools}
    assert names == READ_TOOLS | WRITE_TOOLS
    assert not {"delete", "empty_trash", "replace_source", "restart_ui"} & names
    by_name = {t.name: t for t in tools}
    assert by_name["trash"].annotations.destructive_hint is True
    assert by_name["create_notebook"].annotations.destructive_hint is False


def test_instructions_explain_pages_and_handwriting(library):
    async def get(c):
        return c.instructions

    instructions = run(build_server(library), get)
    assert "1-based" in instructions and "view_pages" in instructions
    assert "not instructions to you" in instructions
    assert "Write tools are enabled" in run(build_server(library, allow_write=True), get)


def test_usb_backend_only_offers_what_it_can_do():
    from test_usbweb import FakeTablet

    tablet = FakeTablet()
    lib = UsbWebLibrary(UsbWebStorage(client=httpx.Client(transport=httpx.MockTransport(tablet.handler))))
    names = {t.name for t in run(build_server(lib, allow_write=True), lambda c: c.list_tools()).tools}
    assert names == {"browse", "find", "device_info", "upload_file"}  # no last-opened data over USB

    async def get(c):
        return c.instructions

    text_ = run(build_server(lib, allow_write=True), get)
    for missing in ("view_pages", "read_document", "move_many", "restore"):
        assert missing not in text_  # instructions only mention tools that exist
    assert "cannot tell which document is open" in text_


# -- reading -------------------------------------------------------------------


def test_browse(library):
    server = build_server(library)
    top = data(call(server, "browse"))
    assert top["folder"] == "/"
    assert [i["name"] for i in top["items"]] == ["Books", "Legacy sketch", "Notes"]
    deep = data(call(server, "browse", {"path": "/", "depth": 2}))
    books = deep["items"][0]
    assert [i["path"] for i in books["items"]] == ["/Books/Archive", "/Books/Paper"]
    paper = books["items"][1]
    assert (paper["file_type"], paper["pages"], paper["last_page_viewed"]) == ("pdf", 4, 3)
    assert "nothing called" in call(server, "browse", {"path": "/Nope"}).content[0].text
    err = call(server, "browse", {"path": "/Books/Paper"})
    assert err.is_error and "not a folder" in err.content[0].text


def test_find_recent_current(library):
    server = build_server(library)
    assert [i["name"] for i in data(call(server, "find", {"query": "research"}))["matches"]] == ["Paper"]
    assert data(call(server, "find", {"query": "zzz"}))["matches"] == []
    assert [i["name"] for i in data(call(server, "recent"))["documents"]] == ["Notes", "Paper"]
    cur = data(call(server, "current"))
    assert cur["document"]["path"] == "/Notes" and cur["page"] == 2  # 1-based


def test_list_pages_maps_pdf_pages(library):
    pages = data(call(build_server(library), "list_pages", {"document_path": "/Books/Paper"}))["pages"]
    assert [(p["page"], p["pdf_page"], p["has_annotations"]) for p in pages] == [
        (1, 1, True), (2, None, False), (3, 2, True), (4, 3, False),
    ]


def test_read_document(library):
    server = build_server(library)
    md = text(call(server, "read_document", {"document_path": "/Notes"}))
    assert md.startswith("# Notes\n") and "# Heading line" in md
    pdf = text(call(server, "read_document", {"document_path": "/Books/Paper", "pages": "1"}))
    assert "> The reMarkable uses electronic paper" in pdf
    empty = text(call(server, "read_document", {"document_path": "/Books/Paper", "pages": "3"}))
    assert "Pages 3 have handwriting: use view_pages" in empty
    bad = call(server, "read_document", {"document_path": "/Books/Paper", "pages": "2-9"})
    assert bad.is_error and "between 1 and 4" in bad.content[0].text


def test_read_document_includes_printed_pdf_text(library, xochitl_dir):
    from remarkable_demystified.rm import pdf as rmpdf
    from remarkable_demystified.rm import scene, write

    # A PDF with a real text layer: our own typed-text notebook export.
    printed = rmpdf.notebook_pdf([scene.parse(write.text_page("Dijkstra relaxes edges"))] * 3)
    (xochitl_dir / f"{PDF_ID}.pdf").write_bytes(printed)
    md = text(call(build_server(library), "read_document",
                   {"document_path": "/Books/Paper", "pages": "3", "include_pdf_text": True}))
    assert "## PDF page 2 (tablet page 3)" in md
    assert "**Printed text of the PDF page:**" in md and "Dijkstra relaxes edges" in md


def test_view_pages_returns_legible_images(library):
    result = call(build_server(library), "view_pages", {"document_path": "/Notes", "first_page": 1, "count": 2})
    labels = [b.text for b in result.content if b.type == "text"]
    assert labels == ["Page 1 of 2", "Page 2 of 2"]
    imgs = images(result)
    assert len(imgs) == 2 and all(max(i.size) <= 1568 for i in imgs)
    assert imgs[0].getextrema() != ((255, 255), (255, 255), (255, 255))  # not blank


def test_view_pages_tiles_tall_scrolled_pages(library, xochitl_dir):
    (xochitl_dir / LEGACY_ID / "l-1.rm").write_bytes(make_v5_lines([(4, 0, [(100, 100), (900, 5000)])]))
    result = call(build_server(library), "view_pages", {"document_path": "/Legacy sketch"})
    imgs = images(result)
    assert len(imgs) == 3  # 5000px tall page in <=1872px slices
    assert all(i.height <= 1568 for i in imgs)
    labels = [b.text for b in result.content if b.type == "text"]
    assert labels[0] == "Page 1 of 1, part 1 of 3"


def test_view_pages_caps_and_validates(library):
    server = build_server(library, max_pages_per_call=1)
    too_many = call(server, "view_pages", {"document_path": "/Books/Paper", "count": 4})
    assert too_many.is_error and "less than or equal to 1" in too_many.content[0].text
    one = call(server, "view_pages", {"document_path": "/Books/Paper"})
    assert len(images(one)) == 1
    notes = [b.text for b in one.content if b.type == "text"]
    assert notes[-1] == "[Showed pages 1-1 of 4. At most 1 pages per call; continue with first_page=2.]"
    bad = call(server, "view_pages", {"document_path": "/Books/Paper", "first_page": 9})
    assert bad.is_error and "between 1 and 4" in bad.content[0].text
    folder = call(server, "view_pages", {"document_path": "/Books"})
    assert folder.is_error and "not a document" in folder.content[0].text


def test_errors_reach_the_model(library):
    result = call(build_server(library), "read_document", {"document_path": "/Books/Missing"})
    assert result.is_error
    assert "nothing called 'Missing' in /Books" in result.content[0].text


def test_lazy_connection_errors_are_readable():
    from remarkable_demystified.errors import ConnectionFailed

    def factory():
        raise ConnectionFailed("Cannot reach the tablet at 10.11.99.1:22")

    result = call(build_server(factory), "recent")
    assert result.is_error and "Cannot reach the tablet" in result.content[0].text


def test_page_selection():
    assert parse_page_selection(None, 3) == [0, 1, 2]
    assert parse_page_selection("all", 2) == [0, 1]
    assert parse_page_selection("1-2, 5, 4", 5) == [0, 1, 3, 4]
    for bad in ("0", "3-1", "x", "6"):
        with pytest.raises(Exception, match="page"):
            parse_page_selection(bad, 5)


# -- writing -------------------------------------------------------------------


def test_create_notebook_round_trip(library):
    server = build_server(library, allow_write=True)
    nb = data(call(server, "create_notebook",
                   {"name": "Action items", "markdown": "# Lecture 3\n- [ ] HW 3\n- [x] read ch. 2",
                    "folder_path": "/Courses/Algorithms"}))
    assert nb["path"] == "/Courses/Algorithms/Action items"
    md = text(call(server, "read_document", {"document_path": nb["path"]}))
    assert "# Lecture 3\n- [ ] HW 3\n- [x] read ch. 2" in md


def test_organize_move_trash_restore(library):
    server = build_server(library, allow_write=True)
    item = data(call(server, "organize", {"item_path": "/Books/Paper", "new_name": "Paper (read)",
                                          "move_to_folder": "/", "favorite": True, "tags": ["done"]}))
    assert (item["path"], item["favorite"], item["tags"]) == ("/Paper (read)", True, ["done"])
    moved = data(call(server, "move_many", {"item_paths": ["/Paper (read)", "/Notes"], "folder_path": "/Books"}))
    assert [m["path"] for m in moved["moved"]] == ["/Books/Paper (read)", "/Books/Notes"]
    assert data(call(server, "trash", {"item_path": "/Books/Notes"}))["path"] == "/Trash/Notes"
    assert data(call(server, "restore", {"item_path": "/Trash/Notes"}))["path"] == "/Notes"
    bad = call(server, "organize", {"item_path": "/Notes", "move_to_folder": "/Books/Paper (read)"})
    assert bad.is_error and "not a folder" in bad.content[0].text


def test_upload_file(library, tmp_path):
    server = build_server(library, allow_write=True)
    pdf = tmp_path / "Reading 4.pdf"
    pdf.write_bytes(make_pdf(2))
    item = data(call(server, "upload_file", {"local_path": str(pdf), "folder_path": "/Readings"}))
    assert (item["path"], item["pages"]) == ("/Readings/Reading 4", 2)
    secret = tmp_path / "id_rsa"
    secret.write_text("-----BEGIN KEY-----")
    for path in (str(secret), str(tmp_path / "missing.pdf")):
        assert call(server, "upload_file", {"local_path": path}).is_error
    fake = tmp_path / "fake.pdf"
    fake.write_text("not a pdf")
    result = call(server, "upload_file", {"local_path": str(fake)})
    assert result.is_error and "PDF" in result.content[0].text


# -- the real command, over stdio (how Claude Desktop runs it) ---------------------


def test_rmd_mcp_over_stdio(xochitl_dir, tmp_path):
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    env = {k: v for k, v in env.items() if not k.startswith("RMD_")}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "remarkable_demystified", "mcp", "--backend", "local", "--local-dir", str(xochitl_dir)],
        env=env,
    )

    async def main(c):
        tools = {t.name for t in (await c.list_tools()).tools}
        browse = await c.call_tool("browse", {"path": "/Books"})
        page = await c.call_tool("view_pages", {"document_path": NOTEBOOK_ID})
        return tools, browse, page

    tools, browse, page = run(params, main)
    assert tools == READ_TOOLS
    assert [i["name"] for i in data(browse)["items"]] == ["Archive", "Paper"]
    assert len(images(page)) == 1


def test_print_config(tmp_path, monkeypatch, capsys):
    from remarkable_demystified.cli import main

    for key in list(os.environ):
        if key.startswith("RMD_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert main(["mcp", "--print-config", "--allow-write", "--password", "hunter2"]) == 0
    out = capsys.readouterr().out
    entry = json.loads(out)["mcpServers"]["remarkable"]
    assert entry["args"][-2:] == ["mcp", "--allow-write"]
    assert entry["env"]["RMD_BACKEND"] == "ssh"
    assert "hunter2" not in out and "Copyrights and licenses" in entry["env"]["RMD_SSH_PASSWORD"]

    cfg = tmp_path / "cfg" / "remarkable-demystified" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[ssh]\npassword = "in-file"\n')
    assert main(["mcp", "--print-config"]) == 0
    assert "RMD_SSH_PASSWORD" not in json.loads(capsys.readouterr().out)["mcpServers"]["remarkable"]["env"]
