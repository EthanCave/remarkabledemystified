"""The tablet's built-in "USB web interface" (http://10.11.99.1 over the cable).

Turn it on under *Settings > Storage > USB web interface*. It needs no password
or developer mode, but it can only list documents, download them (as PDF or
``.rmdoc``) and upload new PDFs/EPUBs. Everything else raises
:class:`~remarkable_demystified.errors.NotSupported` (HTTP 501 from the API).

Endpoints (as served by xochitl):

* ``GET /documents/`` and ``GET /documents/<folder id>`` - folder listings.
  Opening a folder also makes it the target of the next upload.
* ``POST /upload`` - multipart ``file`` field, saved into the last opened folder.
* ``GET /download/<id>/pdf`` (older firmware: ``/download/<id>/placeholder``)
  and ``GET /download/<id>/rmdoc`` - exports.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import httpx

from .. import xochitl
from ..errors import ConnectionFailed, InvalidRequest, NotFound, NotSupported, RemarkableError
from ..library import Library
from ..model import Item
from .base import DocChange, DocumentRecord, DocumentStorage

log = logging.getLogger(__name__)

DEFAULT_URL = "http://10.11.99.1"


def _iso_to_ms(value: Any) -> str:
    try:
        return str(int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000))
    except ValueError:
        return "0"


def record_from_entry(entry: dict[str, Any]) -> DocumentRecord:
    """Turn a USB web listing entry into xochitl-style metadata/content."""
    folder = entry.get("Type") == xochitl.COLLECTION
    metadata = {
        "visibleName": entry.get("VissibleName") or entry.get("VisibleName") or entry.get("ID"),
        "type": xochitl.COLLECTION if folder else xochitl.DOCUMENT,
        "parent": entry.get("Parent") or "",
        "lastModified": _iso_to_ms(entry.get("ModifiedClient")),
        "pinned": bool(entry.get("Bookmarked", False)),
        "lastOpenedPage": entry.get("CurrentPage", 0),
    }
    content: dict[str, Any] = {"tags": [{"name": t} for t in entry.get("Tags") or [] if isinstance(t, str)]}
    if not folder:
        content["fileType"] = entry.get("fileType") or "notebook"
        content["pageCount"] = entry.get("pageCount") or 0
    return DocumentRecord(str(entry["ID"]), metadata, content)


class UsbWebStorage(DocumentStorage):
    name = "usb"
    cache_ttl = 5.0

    def __init__(self, url: str = DEFAULT_URL, *, client: httpx.Client | None = None, timeout: float = 30) -> None:
        super().__init__()
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        # The tablet keeps "the current folder" server-side; serialize folder hops + uploads.
        self.lock = threading.RLock()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self.client.request(method, self.url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectionFailed(
                f"cannot reach the USB web interface at {self.url}: {exc}. Connect the cable and enable "
                "Settings > Storage > USB web interface."
            ) from exc
        if resp.status_code == 404:
            raise NotFound(f"{path} not found on the tablet")
        if resp.status_code >= 400:
            raise RemarkableError(f"tablet returned HTTP {resp.status_code} for {path}")
        return resp

    def list_folder(self, folder_id: str = "") -> list[dict[str, Any]]:
        with self.lock:
            data = self.request("GET", f"/documents/{folder_id}").json()
        if not isinstance(data, list):
            raise RemarkableError("unexpected response from the USB web interface")
        return data

    def _load_documents(self) -> dict[str, DocumentRecord]:
        records: dict[str, DocumentRecord] = {}
        queue = [""]
        while queue:
            folder = queue.pop()
            for entry in self.list_folder(folder):
                if "ID" not in entry:
                    continue
                rec = record_from_entry(entry)
                rec.metadata["parent"] = folder  # listings are authoritative about placement
                if rec.id not in records:
                    records[rec.id] = rec
                    if rec.metadata["type"] == xochitl.COLLECTION:
                        queue.append(rec.id)
        return records

    def list_files(self, doc_id: str) -> list[str]:
        raise NotSupported("the USB web interface cannot list a document's files; use SSH or the cloud")

    def read_file(self, doc_id: str, name: str) -> bytes:
        raise NotSupported("the USB web interface cannot read raw document files; use SSH or the cloud")

    def _apply(self, changes: list[DocChange]) -> None:
        raise NotSupported("the USB web interface can only upload new documents; use SSH or the cloud to edit")

    def device_info(self) -> dict[str, Any]:
        return {"backend": self.name, "url": self.url}

    def close(self) -> None:
        self.client.close()


class UsbWebLibrary(Library):
    """A :class:`Library` limited to what the USB web interface offers."""

    capabilities = frozenset({"list", "download", "export_pdf", "export_archive", "upload"})

    storage: UsbWebStorage

    def __init__(self, storage: UsbWebStorage) -> None:
        super().__init__(storage)

    def export_pdf(self, item_id: str) -> bytes:
        self._document(item_id)
        for fmt in ("pdf", "placeholder"):
            try:
                data = self.storage.request("GET", f"/download/{item_id}/{fmt}").content
            except NotFound:
                continue
            if data.startswith(b"%PDF"):
                return data
        raise NotFound("the tablet did not return a PDF for this document")

    def export_archive(self, item_id: str) -> bytes:
        self._document(item_id)
        try:
            data = self.storage.request("GET", f"/download/{item_id}/rmdoc").content
        except NotFound:
            data = b""
        if not data.startswith(b"PK"):
            raise NotSupported("this firmware cannot export .rmdoc files over USB")
        return data

    def source(self, item_id: str) -> tuple[bytes, str]:
        # Only the rendered PDF is downloadable; for PDFs that is the original plus annotations.
        return self.export_pdf(item_id), "pdf"

    def upload(self, data: bytes, filename: str, *, parent: str = xochitl.ROOT, name: str | None = None,
               max_unpacked: int = 0) -> Item:
        ext = PurePosixPath(filename).suffix.lower().lstrip(".")
        if ext not in xochitl.SOURCE_EXTENSIONS:
            raise InvalidRequest("the USB web interface only accepts PDF and EPUB files")
        self._check_parent(parent)
        visible = self._check_name(name or PurePosixPath(filename).stem)
        mime = "application/pdf" if ext == "pdf" else "application/epub+zip"
        with self.storage.lock:
            before = {e.get("ID") for e in self.storage.list_folder(parent)}  # also selects the folder
            self.storage.request(
                "POST", "/upload",
                files={"file": (f"{visible}.{ext}", data, mime)},
                headers={"Origin": self.storage.url, "Referer": self.storage.url + "/"},
            )
            for _ in range(20):  # the tablet imports asynchronously
                new = [e for e in self.storage.list_folder(parent) if e.get("ID") not in before]
                if new:
                    self.storage.invalidate()
                    return self.get(str(new[0]["ID"]))
                time.sleep(0.5)
        raise RemarkableError("upload accepted, but the document did not appear on the tablet yet")
