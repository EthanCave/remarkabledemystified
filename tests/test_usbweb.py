from __future__ import annotations

import httpx
import pytest
from conftest import make_pdf

from remarkable_demystified.errors import ConnectionFailed, InvalidRequest, NotSupported
from remarkable_demystified.storage.usbweb import UsbWebLibrary, UsbWebStorage


class FakeTablet:
    """Mimics xochitl's USB web server, including its 'current folder' state."""

    def __init__(self, *, old_firmware: bool = False) -> None:
        self.old_firmware = old_firmware
        self.current = ""
        self.entries = [
            {"ID": "f1", "Parent": "", "Type": "CollectionType", "VissibleName": "Books",
             "ModifiedClient": "2025-01-02T03:04:05.000Z", "Bookmarked": False},
            {"ID": "d1", "Parent": "f1", "Type": "DocumentType", "VissibleName": "Dune",
             "ModifiedClient": "2025-02-02T03:04:05.000Z", "Bookmarked": True, "fileType": "epub",
             "pageCount": 412, "CurrentPage": 17, "Tags": ["scifi"]},
            {"ID": "d2", "Parent": "", "Type": "DocumentType", "VissibleName": "Sketch",
             "ModifiedClient": "2025-03-02T03:04:05.000Z", "fileType": "notebook", "pageCount": 2},
        ]
        self.uploads: list[tuple[str, str, bytes]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/documents/"):
            folder = path[len("/documents/"):]
            self.current = folder
            return httpx.Response(200, json=[e for e in self.entries if e["Parent"] == folder])
        if path == "/upload" and request.method == "POST":
            body = request.content
            filename = body.split(b'filename="')[1].split(b'"')[0].decode()
            self.uploads.append((self.current, filename, body))
            self.entries.append({"ID": f"new{len(self.uploads)}", "Parent": self.current,
                                 "Type": "DocumentType", "VissibleName": filename.rsplit(".", 1)[0],
                                 "ModifiedClient": "2025-04-01T00:00:00Z", "fileType": "pdf"})
            return httpx.Response(201)
        if path.startswith("/download/"):
            _, _, doc_id, fmt = path.split("/")
            if fmt == "pdf" and not self.old_firmware:
                return httpx.Response(200, content=make_pdf(1))
            if fmt == "placeholder":
                return httpx.Response(200, content=make_pdf(2))
            if fmt == "rmdoc" and not self.old_firmware:
                return httpx.Response(200, content=b"PK\x03\x04zip")
            return httpx.Response(404)
        return httpx.Response(404)


@pytest.fixture
def tablet():
    return FakeTablet()


def make_lib(tablet: FakeTablet) -> UsbWebLibrary:
    client = httpx.Client(transport=httpx.MockTransport(tablet.handler))
    return UsbWebLibrary(UsbWebStorage("http://10.11.99.1", client=client))


def test_listing(tablet):
    lib = make_lib(tablet)
    assert [i.name for i in lib.items()] == ["Books", "Dune", "Sketch"]
    dune = lib.resolve("/Books/Dune")
    assert dune.file_type == "epub" and dune.pinned and dune.page_count == 412
    assert dune.current_page == 17 and dune.tags == ["scifi"]
    assert dune.last_modified.year == 2025
    assert lib.path("d1") == "/Books/Dune"


def test_downloads(tablet):
    lib = make_lib(tablet)
    assert lib.export_pdf("d1").startswith(b"%PDF")
    assert lib.export_archive("d2").startswith(b"PK")
    old = FakeTablet(old_firmware=True)
    old_lib = make_lib(old)
    assert old_lib.export_pdf("d1").startswith(b"%PDF")  # falls back to /placeholder
    with pytest.raises(NotSupported):
        old_lib.export_archive("d1")


def test_upload_goes_to_selected_folder(tablet):
    lib = make_lib(tablet)
    item = lib.upload(make_pdf(1), "paper.pdf", parent="f1", name="My Paper")
    assert item.name == "My Paper" and item.parent == "f1"
    assert tablet.uploads[0][:2] == ("f1", "My Paper.pdf")
    with pytest.raises(InvalidRequest):
        lib.upload(b"x", "notes.rmdoc")


def test_unsupported_operations(tablet):
    lib = make_lib(tablet)
    for call in (
        lambda: lib.rename("d1", "x"),
        lambda: lib.create_folder("x"),
        lambda: lib.render_page("d1", 0),
        lambda: lib.text("d1"),
        lambda: lib.delete("d1"),
    ):
        with pytest.raises(NotSupported):
            call()
    assert "rename" not in lib.capabilities and "upload" in lib.capabilities


def test_unreachable():
    def boom(request):
        raise httpx.ConnectError("no route")

    lib = UsbWebLibrary(UsbWebStorage(client=httpx.Client(transport=httpx.MockTransport(boom))))
    with pytest.raises(ConnectionFailed):
        lib.items()
