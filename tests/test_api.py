from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from conftest import FOLDER_ID, NOTEBOOK_ID, PDF_ID, TRASHED_ID, make_pdf
from fastapi.testclient import TestClient
from pypdf import PdfReader
from test_usbweb import FakeTablet

from remarkable_demystified.config import Settings
from remarkable_demystified.server.app import create_app
from remarkable_demystified.storage.usbweb import UsbWebLibrary, UsbWebStorage


@pytest.fixture
def client(library):
    with TestClient(create_app(library, settings=Settings(backend="local"))) as c:
        yield c


def test_health_and_docs(client):
    assert client.get("/health").json() == {"ok": True, "backend": "local", "version": "0.1.0"}
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/docs"


def test_token_auth(library):
    app = create_app(library, settings=Settings(backend="local"), api_token="s3cret")
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200  # always open
        r = c.get("/items")
        assert r.status_code == 401 and r.json()["error"] == "Unauthorized"
        assert c.get("/items", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/items", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert c.get("/items", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_browse(client):
    items = client.get("/items").json()
    assert [i["name"] for i in items] == ["Archive", "Books", "Legacy sketch", "Notes", "Paper"]
    paper = next(i for i in items if i["id"] == PDF_ID)
    assert paper["path"] == "/Books/Paper" and paper["file_type"] == "pdf" and paper["page_count"] == 4

    assert [i["name"] for i in client.get("/items", params={"parent": FOLDER_ID}).json()] == ["Archive", "Paper"]
    assert [i["name"] for i in client.get("/items", params={"parent": ""}).json()] == ["Books", "Legacy sketch", "Notes"]
    assert [i["name"] for i in client.get("/items", params={"q": "note"}).json()] == ["Notes"]
    assert [i["name"] for i in client.get("/items", params={"type": "folder"}).json()] == ["Archive", "Books"]
    assert "Old draft" in [i["name"] for i in client.get("/items", params={"include_trash": True}).json()]

    assert client.get("/items/resolve", params={"path": "/Books/Paper"}).json()["id"] == PDF_ID
    assert client.get("/items/current").json()["path"] == "/Notes"
    assert [i["name"] for i in client.get("/items/recent").json()] == ["Notes", "Paper"]
    tree = client.get("/items/tree").json()
    assert tree[0]["item"]["name"] == "Books" and tree[0]["children"][1]["item"]["name"] == "Paper"
    assert client.get(f"/items/{NOTEBOOK_ID}").json()["pinned"] is True


def test_errors_are_json(client):
    r = client.get("/items/nope")
    assert r.status_code == 404 and r.json()["error"] == "NotFound"
    r = client.get("/items/resolve", params={"path": "/Missing"})
    assert r.status_code == 404
    r = client.get(f"/documents/{PDF_ID}/pages/0", params={"format": "gif"})
    assert r.status_code == 422


def test_organize(client):
    r = client.patch(f"/items/{PDF_ID}", json={"name": "Paper 2", "parent": "", "pinned": True, "tags": ["x"]})
    assert r.status_code == 200
    body = r.json()
    assert (body["name"], body["path"], body["pinned"], body["tags"]) == ("Paper 2", "/Paper 2", True, ["x"])

    assert client.delete(f"/items/{PDF_ID}").status_code == 204
    assert client.get(f"/items/{PDF_ID}").json()["parent"] == "trash"
    assert client.post(f"/items/{PDF_ID}/restore", json={"parent": FOLDER_ID}).json()["path"] == "/Books/Paper 2"

    r = client.post("/items/move", json={"ids": [PDF_ID, NOTEBOOK_ID], "parent": ""})
    assert [i["parent"] for i in r.json()] == ["", ""]

    r = client.post("/folders", json={"name": "New", "parent": FOLDER_ID})
    assert r.status_code == 201 and r.json()["path"] == "/Books/New"
    assert client.post("/folders", json={"path": "/A/B/C"}).json()["path"] == "/A/B/C"
    assert client.post("/folders", json={}).status_code == 400

    assert client.delete("/trash").json() == {"deleted": 1}
    assert client.get(f"/items/{TRASHED_ID}").status_code == 404
    assert client.delete(f"/items/{FOLDER_ID}", params={"permanent": True}).status_code == 400
    assert client.delete(f"/items/{FOLDER_ID}", params={"permanent": True, "recursive": True}).status_code == 204


def test_upload_and_create(client):
    r = client.post("/documents", files={"file": ("Lecture.pdf", make_pdf(2), "application/pdf")},
                    data={"parent": FOLDER_ID})
    assert r.status_code == 201
    doc = r.json()
    assert doc["name"] == "Lecture" and doc["path"] == "/Books/Lecture" and doc["page_count"] == 2

    r = client.post("/documents", files={"file": ("x.pdf", b"nope", "application/pdf")})
    assert r.status_code == 400 and "PDF" in r.json()["detail"]

    r = client.post("/notebooks", json={"name": "Ideas", "text": "# Ideas\n- one\n- two"})
    assert r.status_code == 201
    md = client.get(f"/documents/{r.json()['id']}/text", params={"format": "markdown"}).text
    assert "# Ideas\n- one\n- two" in md

    r = client.put(f"/documents/{doc['id']}/file", files={"file": ("v2.pdf", make_pdf(5), "application/pdf")})
    assert r.status_code == 200
    original = client.get(f"/documents/{doc['id']}/file")
    assert len(PdfReader(io.BytesIO(original.content)).pages) == 5


def test_read_endpoints(client):
    r = client.get(f"/documents/{PDF_ID}/pdf")
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith('inline; filename="Paper.pdf"')
    assert len(PdfReader(io.BytesIO(r.content)).pages) == 4

    r = client.get(f"/documents/{PDF_ID}/file")
    assert r.content.startswith(b"%PDF") and "attachment" in r.headers["content-disposition"]

    r = client.get(f"/documents/{NOTEBOOK_ID}/archive")
    assert f"{NOTEBOOK_ID}.content" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()

    pages = client.get(f"/documents/{PDF_ID}/pages").json()
    assert [p["has_annotations"] for p in pages] == [True, False, True, False]

    png = client.get(f"/documents/{NOTEBOOK_ID}/pages/1", params={"format": "png", "scale": 0.2})
    assert png.headers["content-type"] == "image/png" and png.content.startswith(b"\x89PNG")
    svg = client.get(f"/documents/{NOTEBOOK_ID}/pages/0")
    assert svg.headers["content-type"].startswith("image/svg+xml") and b"Heading line" in svg.content
    assert client.get(f"/documents/{NOTEBOOK_ID}/pages/7").status_code == 404

    text = client.get(f"/documents/{PDF_ID}/text").json()
    assert text["pages"][0]["highlights"][0]["text"] == "The reMarkable uses electronic paper"

    files = client.get(f"/documents/{PDF_ID}/files").json()
    assert f"{PDF_ID}/p-a.rm" in files
    raw = client.get(f"/documents/{PDF_ID}/files/{PDF_ID}/p-a.rm")
    assert raw.content.startswith(b"reMarkable .lines file, version=6")
    assert client.get(f"/documents/{PDF_ID}/files/{NOTEBOOK_ID}.content").status_code == 400


def test_device(client):
    info = client.get("/device").json()
    assert info["backend"] == "local" and "render" in info["capabilities"]
    assert client.post("/device/restart-ui").status_code == 501
    assert client.post("/sync").status_code == 204


def test_unsupported_backend_returns_501():
    tablet = FakeTablet()
    lib = UsbWebLibrary(UsbWebStorage(client=httpx.Client(transport=httpx.MockTransport(tablet.handler))))
    with TestClient(create_app(lib, settings=Settings(backend="usb"))) as c:
        assert [i["name"] for i in c.get("/items").json()] == ["Books", "Dune", "Sketch"]
        r = c.patch("/items/d1", json={"name": "x"})
        assert r.status_code == 501 and r.json()["error"] == "NotSupported"
        assert c.post("/notebooks", json={"name": "x"}).status_code == 501
        assert c.get("/documents/d1/pdf").content.startswith(b"%PDF")


def test_lazy_library_factory(xochitl_dir):
    from remarkable_demystified.library import Library
    from remarkable_demystified.storage.local import LocalStorage

    opened = []

    def factory():
        opened.append(1)
        return Library(LocalStorage(xochitl_dir))

    with TestClient(create_app(factory, settings=Settings(backend="local"))) as c:
        assert c.get("/health").status_code == 200 and not opened
        assert c.get("/items").status_code == 200 and opened == [1]
        c.get("/items")
        assert opened == [1]
