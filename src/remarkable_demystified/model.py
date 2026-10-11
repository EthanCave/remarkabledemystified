"""Plain data objects returned by the library (and serialized by the REST API)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import xochitl


def _ms_to_datetime(value: Any) -> datetime | None:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    if ms > 10**15:  # a few old files stored nanoseconds
        ms //= 1_000_000
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


@dataclass
class Item:
    """A document or folder, as shown in the tablet's file browser."""

    id: str
    name: str
    #: ``"folder"`` or ``"document"`` (``"template"`` for custom templates on 3.x)
    type: str
    #: Parent folder id. ``""`` is the root ("My files"), ``"trash"`` is the trash.
    parent: str
    #: ``"pdf"``, ``"epub"`` or ``"notebook"`` for documents, ``None`` for folders.
    file_type: str | None = None
    pinned: bool = False
    tags: list[str] = field(default_factory=list)
    page_count: int | None = None
    #: Page the document was last left open on (0-based).
    current_page: int | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    last_opened: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False)
    content: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_folder(self) -> bool:
        return self.type == "folder"

    @property
    def in_trash(self) -> bool:
        return self.parent == xochitl.TRASH

    @classmethod
    def from_files(cls, doc_id: str, metadata: dict[str, Any], content: dict[str, Any]) -> Item:
        kind = metadata.get("type")
        if kind == xochitl.COLLECTION:
            type_ = "folder"
        elif kind == xochitl.TEMPLATE:
            type_ = "template"
        else:
            type_ = "document"

        file_type = None
        page_count = None
        if type_ == "document":
            file_type = content.get("fileType") or "notebook"
            pages = xochitl.page_refs(content)
            page_count = len(pages) or content.get("pageCount") or None

        return cls(
            id=doc_id,
            name=str(metadata.get("visibleName") or doc_id),
            type=type_,
            parent=str(metadata.get("parent") or ""),
            file_type=file_type,
            pinned=bool(metadata.get("pinned", False)),
            tags=xochitl.document_tags(content),
            page_count=page_count,
            current_page=metadata.get("lastOpenedPage") if type_ == "document" else None,
            created=_ms_to_datetime(metadata.get("createdTime")),
            last_modified=_ms_to_datetime(metadata.get("lastModified")),
            last_opened=_ms_to_datetime(metadata.get("lastOpened")),
            metadata=metadata,
            content=content,
        )

    def to_dict(self, *, raw: bool = False) -> dict[str, Any]:
        out = asdict(self)
        for key in ("created", "last_modified", "last_opened"):
            if out[key] is not None:
                out[key] = out[key].isoformat()
        if not raw:
            out.pop("metadata")
            out.pop("content")
        return out


ROOT_ITEM = Item(id=xochitl.ROOT, name="My files", type="folder", parent="")
TRASH_ITEM = Item(id=xochitl.TRASH, name="Trash", type="folder", parent="")


@dataclass
class PageInfo:
    index: int
    id: str
    #: Page of the original PDF/EPUB (0-based), if this page shows one.
    source_page: int | None
    template: str | None
    #: True when the page has a ``.rm`` file (strokes, highlights or typed text).
    has_annotations: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Highlight:
    text: str
    #: Position of the page in the document as the tablet shows it (0-based).
    page_index: int
    color: str | None = None
    #: Page of the original PDF/EPUB (0-based); differs from ``page_index`` once
    #: pages are inserted on the tablet, and is the number to cite.
    source_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PageText:
    index: int
    #: Typed text (keyboard / type folio), as Markdown.
    text: str
    highlights: list[Highlight] = field(default_factory=list)
    #: Page of the original PDF/EPUB (0-based), ``None`` for notebook and inserted pages.
    source_page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "source_page": self.source_page,
            "text": self.text,
            "highlights": [h.to_dict() for h in self.highlights],
        }

    def heading(self, has_source: bool) -> str:
        """``Page 3``, ``PDF page 2 (tablet page 3)`` or ``Inserted page (tablet page 3)``."""
        if not has_source:
            return f"Page {self.index + 1}"
        if self.source_page is None:
            return f"Inserted page (tablet page {self.index + 1})"
        if self.source_page == self.index:
            return f"Page {self.index + 1}"
        return f"PDF page {self.source_page + 1} (tablet page {self.index + 1})"


@dataclass
class DocumentText:
    id: str
    name: str
    pages: list[PageText]
    #: ``"pdf"``, ``"epub"`` or ``"notebook"``.
    file_type: str | None = None

    def to_markdown(self) -> str:
        has_source = self.file_type in ("pdf", "epub")
        parts = [f"# {self.name}\n"]
        for page in self.pages:
            if not page.text.strip() and not page.highlights:
                continue
            parts.append(f"\n## {page.heading(has_source)}\n")
            if page.text.strip():
                parts.append(page.text.rstrip() + "\n")
            if page.highlights:
                parts.append("\n**Highlights**\n")
                for h in page.highlights:
                    parts.append(f"\n> {h.text.strip()}\n")
        return "".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "file_type": self.file_type,
            "pages": [p.to_dict() for p in self.pages],
        }
