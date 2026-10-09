"""Request/response models for the REST API (they drive the OpenAPI docs at /docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ItemOut(BaseModel):
    id: str = Field(description='Document/folder UUID. "" is the root, "trash" the trash.')
    name: str
    type: str = Field(description='"folder", "document" or "template"')
    parent: str
    path: str | None = None
    file_type: str | None = Field(None, description='"pdf", "epub" or "notebook" for documents')
    pinned: bool = False
    tags: list[str] = []
    page_count: int | None = None
    current_page: int | None = None
    created: datetime | None = None
    last_modified: datetime | None = None
    last_opened: datetime | None = None


class TreeNode(BaseModel):
    item: ItemOut
    children: list[TreeNode] | None = None


class CurrentOut(BaseModel):
    item: ItemOut
    page: int
    path: str


class ItemUpdate(BaseModel):
    name: str | None = Field(None, description="New name")
    parent: str | None = Field(None, description='Folder id to move into ("" = root, "trash" = trash)')
    pinned: bool | None = Field(None, description="Favorite (star) on the tablet")
    tags: list[str] | None = Field(None, description="Replace the document's tags")


class RestoreIn(BaseModel):
    parent: str = ""


class MoveIn(BaseModel):
    ids: list[str]
    parent: str


class FolderIn(BaseModel):
    name: str | None = Field(None, description="Folder name (with `parent`)")
    parent: str = ""
    path: str | None = Field(None, description="Or a full path like /Books/2026; missing folders are created")


class NotebookIn(BaseModel):
    name: str
    parent: str = ""
    text: str | None = Field(
        None,
        description="Typed text for the first page. Markdown-ish: `# heading`, `- bullet`, "
        "`- [ ] checkbox`, `**bold line**`.",
    )


class PageOut(BaseModel):
    index: int
    id: str
    source_page: int | None
    template: str | None
    has_annotations: bool


class HighlightOut(BaseModel):
    text: str
    page_index: int
    color: str | None = None


class PageTextOut(BaseModel):
    index: int
    text: str
    highlights: list[HighlightOut]


class TextOut(BaseModel):
    id: str
    name: str
    pages: list[PageTextOut]


class DeletedOut(BaseModel):
    deleted: int


class ErrorOut(BaseModel):
    error: str
    detail: str


class Health(BaseModel):
    ok: bool
    backend: str
    version: str


DeviceInfo = dict[str, Any]
