"""Knowledge about xochitl's on-disk document format.

xochitl is the reMarkable's UI process. Every document or folder is a UUID with a
family of files next to it, both on the tablet (``~/.local/share/remarkable/xochitl``)
and in the cloud (where each file becomes a content-addressed blob):

====================================  ==================================================
``<id>.metadata``                     JSON: visible name, type, parent, timestamps, pin
``<id>.content``                      JSON: file type, page list, tags, view settings
``<id>.pdf`` / ``<id>.epub``          the original file for imported documents
``<id>/<page-id>.rm``                 pen strokes / typed text for one page
``<id>/<page-id>-metadata.json``      layer names for one page (older firmware)
``<id>.pagedata``                     template name per page (older firmware)
``<id>.thumbnails/<page-id>.png``     thumbnails rendered by the tablet
``<id>.highlights/<page-id>.json``    text highlights (firmware 2.x)
``<id>.local``                        device-local state, never synced
====================================  ==================================================

The helpers in this module create and update those JSON files the same way the
tablet and the well-tested community tools (rmapi, rmscene) do.
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

ROOT = ""
TRASH = "trash"

COLLECTION = "CollectionType"
DOCUMENT = "DocumentType"
TEMPLATE = "TemplateType"

#: Extensions that xochitl accepts as the "original" file of a document.
SOURCE_EXTENSIONS = ("pdf", "epub")

#: Display geometry of the tablets, in pixels. Strokes are stored in these units.
RM2_WIDTH = 1404
RM2_HEIGHT = 1872
RM2_DPI = 226


def now_ms() -> str:
    """Current time in milliseconds since the epoch, as xochitl stores it (a string)."""
    return str(int(time.time() * 1000))


def new_id() -> str:
    return str(uuid.uuid4())


def doc_id_of(relative_path: str) -> str:
    """The document id a file belongs to (``<id>.metadata`` / ``<id>/x.rm`` -> ``<id>``)."""
    for i, ch in enumerate(relative_path):
        if ch in "./":
            return relative_path[:i]
    return relative_path


def is_safe_file_name(doc_id: str, name: str) -> bool:
    """True if ``name`` is a plain relative path belonging to ``doc_id``.

    Rejects ``..``, absolute paths and control characters (a newline would let a
    name smuggle a second path into line-based remote commands).
    """
    return (
        bool(doc_id)
        and doc_id_of(name) == doc_id
        and not name.startswith("/")
        and ".." not in name.split("/")
        and not any(ord(c) < 32 or c == "\x7f" for c in name)
    )


def metadata_name(doc_id: str) -> str:
    return f"{doc_id}.metadata"


def content_name(doc_id: str) -> str:
    return f"{doc_id}.content"


def page_rm_name(doc_id: str, page_id: str) -> str:
    return f"{doc_id}/{page_id}.rm"


def source_name(doc_id: str, ext: str) -> str:
    return f"{doc_id}.{ext}"


def dumps(obj: Any) -> bytes:
    """Serialize JSON the way xochitl writes it (indented, UTF-8)."""
    return json.dumps(obj, indent=4, ensure_ascii=False).encode("utf-8")


def loads(data: bytes | str | None) -> dict[str, Any]:
    """Parse a metadata/content file, tolerating empty or slightly broken files."""
    if not data:
        return {}
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# .metadata
# ---------------------------------------------------------------------------


def new_metadata(name: str, parent: str = ROOT, kind: str = DOCUMENT) -> dict[str, Any]:
    ts = now_ms()
    return {
        "createdTime": ts,
        "deleted": False,
        "lastModified": ts,
        "lastOpened": "0",
        "lastOpenedPage": 0,
        "metadatamodified": False,
        "modified": False,
        "parent": parent,
        "pinned": False,
        "synced": False,
        "type": kind,
        "version": 0,
        "visibleName": name,
    }


def updated_metadata(meta: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Return a copy of ``meta`` with ``changes`` applied, marked as modified.

    Unknown keys written by newer firmware are preserved untouched.
    """
    out = copy.deepcopy(meta)
    out.update(changes)
    out["lastModified"] = now_ms()
    out["metadatamodified"] = True
    if isinstance(out.get("version"), int):
        out["version"] += 1
    return out


# ---------------------------------------------------------------------------
# .content
# ---------------------------------------------------------------------------


def new_content(file_type: str, page_ids: list[str] | None = None) -> dict[str, Any]:
    """A ``.content`` file for a freshly imported document.

    ``file_type`` is ``"pdf"``, ``"epub"`` or ``"notebook"``. We write the
    ``formatVersion: 1`` page list (``pages``), which every firmware accepts and
    3.x firmware migrates to ``cPages`` on first open.
    """
    page_ids = page_ids or []
    return {
        "dummyDocument": False,
        "extraMetadata": {},
        "fileType": file_type,
        "fontName": "",
        "lastOpenedPage": 0,
        "lineHeight": -1,
        "margins": 180,
        "orientation": "portrait",
        "pageCount": len(page_ids),
        "pages": page_ids,
        "pageTags": [],
        "tags": [],
        "textScale": 1,
        "transform": {
            "m11": 1, "m12": 0, "m13": 0,
            "m21": 0, "m22": 1, "m23": 0,
            "m31": 0, "m32": 0, "m33": 1,
        },
    }


def folder_content() -> dict[str, Any]:
    return {"tags": []}


@dataclass(frozen=True)
class PageRef:
    """One visible page of a document, in reading order."""

    index: int
    id: str
    #: Page of the original PDF/EPUB shown on this page (None for inserted blank pages
    #: and for notebooks).
    source_page: int | None
    template: str | None = None


def _lww_value(entry: Any) -> Any:
    """cPages stores most values as ``{"timestamp": "1:2", "value": ...}``."""
    if isinstance(entry, dict):
        return entry.get("value")
    return entry


def page_refs(content: dict[str, Any], source_page_count: int | None = None) -> list[PageRef]:
    """Visible pages of a document in reading order.

    Handles both page-list formats:

    * ``formatVersion 2`` (firmware 3.x): ``cPages.pages`` is a CRDT list whose
      order is given by the ``idx`` string; deleted pages carry ``deleted``;
      ``redir`` maps the page to the original PDF page.
    * older firmware: ``pages`` (page ids) plus ``redirectionPageMap``.

    For a PDF that has never been opened on the tablet neither list exists yet;
    pass ``source_page_count`` to synthesize one page per PDF page.
    """
    file_type = content.get("fileType") or "notebook"
    has_source = file_type in SOURCE_EXTENSIONS

    cpages = content.get("cPages")
    if isinstance(cpages, dict) and isinstance(cpages.get("pages"), list):
        visible = []
        for entry in cpages["pages"]:
            if not isinstance(entry, dict) or "id" not in entry:
                continue
            deleted = _lww_value(entry.get("deleted"))
            if deleted not in (None, 0, False):
                continue
            idx = _lww_value(entry.get("idx")) or ""
            redir = _lww_value(entry.get("redir"))
            template = _lww_value(entry.get("template"))
            visible.append((str(idx), entry["id"], redir, template))
        visible.sort(key=lambda p: (p[0], p[1]))
        return [
            PageRef(
                index=i,
                id=pid,
                source_page=redir if has_source and isinstance(redir, int) else None,
                template=template,
            )
            for i, (_idx, pid, redir, template) in enumerate(visible)
        ]

    pages = content.get("pages")
    if isinstance(pages, list) and pages:
        redirection = content.get("redirectionPageMap")
        refs = []
        for i, pid in enumerate(pages):
            src: int | None = None
            if has_source:
                if isinstance(redirection, list) and i < len(redirection):
                    src = redirection[i] if redirection[i] >= 0 else None
                else:
                    src = i
            refs.append(PageRef(index=i, id=str(pid), source_page=src))
        return refs

    # PDF/EPUB that the tablet has not paginated yet.
    count = source_page_count if source_page_count is not None else content.get("pageCount") or 0
    if has_source and count:
        return [PageRef(index=i, id=f"page-{i}", source_page=i) for i in range(count)]
    return []


def document_tags(content: dict[str, Any]) -> list[str]:
    tags = content.get("tags") or []
    out = []
    for tag in tags:
        if isinstance(tag, dict) and "name" in tag:
            out.append(str(tag["name"]))
        elif isinstance(tag, str):
            out.append(tag)
    return out


def with_tags(content: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    """Return a copy of ``content`` with its document tags replaced by ``tags``."""
    out = copy.deepcopy(content)
    existing = {
        t["name"]: t for t in out.get("tags") or [] if isinstance(t, dict) and "name" in t
    }
    ts = int(time.time() * 1000)
    out["tags"] = [existing.get(name) or {"name": name, "timestamp": ts} for name in tags]
    return out
