"""High-level operations on a reMarkable library, independent of the transport.

>>> from remarkable_demystified import connect
>>> rm = connect()                      # settings from env / config file
>>> [i.name for i in rm.children("")]   # top-level folders and documents
>>> rm.upload(open("paper.pdf", "rb").read(), "paper.pdf", parent=rm.resolve("/Papers").id)
>>> open("notes.pdf", "wb").write(rm.export_pdf(rm.resolve("/Notes/Meeting").id))
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

from . import xochitl
from .errors import InvalidRequest, NotFound, NotSupported
from .model import ROOT_ITEM, TRASH_ITEM, DocumentText, Highlight, Item, PageInfo, PageText
from .rm import pdf as rmpdf
from .rm import render, scene, write
from .storage.base import DocChange, DocumentRecord, DocumentStorage

log = logging.getLogger(__name__)

#: Everything a :class:`Library` can do. Limited backends (USB web) advertise less.
ALL_CAPABILITIES = frozenset({
    "list", "read_files", "download", "export_pdf", "export_archive", "render", "text",
    "upload", "import_archive", "create_folder", "create_notebook", "rename", "move",
    "pin", "tags", "trash", "delete", "replace_source",
})

#: Files that only make sense on the device that wrote them.
_ARCHIVE_EXCLUDE = (".local", ".cache/", ".tombstone")


class Library:
    """Everything you can do with the documents on a tablet (or in its cloud)."""

    capabilities = ALL_CAPABILITIES

    def __init__(self, storage: DocumentStorage) -> None:
        self.storage = storage

    @property
    def backend(self) -> str:
        return self.storage.name

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Listing and lookup
    # ------------------------------------------------------------------

    def _records(self, refresh: bool = False) -> dict[str, DocumentRecord]:
        return {
            doc_id: rec
            for doc_id, rec in self.storage.documents(refresh=refresh).items()
            if not rec.metadata.get("deleted")
        }

    def refresh(self) -> None:
        self.storage.documents(refresh=True)

    def items(self, *, include_trash: bool = False, refresh: bool = False) -> list[Item]:
        """Every document and folder (optionally including the trash)."""
        items = [Item.from_files(r.id, r.metadata, r.content) for r in self._records(refresh).values()]
        if not include_trash:
            trashed = self._trashed_ids(items)
            items = [i for i in items if i.id not in trashed]
        return sorted(items, key=_sort_key)

    def _trashed_ids(self, items: list[Item]) -> set[str]:
        """Items in the trash, including the contents of trashed folders."""
        by_id = {i.id: i for i in items}
        trashed: set[str] = set()
        for item in items:
            seen = set()
            cur: Item | None = item
            while cur is not None and cur.id not in seen:
                seen.add(cur.id)
                if cur.parent == xochitl.TRASH:
                    trashed.add(item.id)
                    break
                cur = by_id.get(cur.parent)
        return trashed

    def get(self, item_id: str) -> Item:
        if item_id == xochitl.ROOT:
            return ROOT_ITEM
        if item_id == xochitl.TRASH:
            return TRASH_ITEM
        rec = self._records().get(item_id)
        if rec is None:
            raise NotFound(f"no document or folder with id {item_id!r}")
        return Item.from_files(rec.id, rec.metadata, rec.content)

    def children(self, folder_id: str = xochitl.ROOT) -> list[Item]:
        """Contents of a folder (``""`` = My files, ``"trash"`` = Trash)."""
        if folder_id not in (xochitl.ROOT, xochitl.TRASH):
            folder = self.get(folder_id)
            if not folder.is_folder:
                raise InvalidRequest(f"{folder.name!r} is a document, not a folder")
        return [i for i in self.items(include_trash=True) if i.parent == folder_id]

    def tree(self, folder_id: str = xochitl.ROOT) -> list[dict[str, Any]]:
        """Nested ``{"item": ..., "children": [...]}`` structure below a folder."""
        items = self.items(include_trash=folder_id == xochitl.TRASH)
        by_parent: dict[str, list[Item]] = {}
        for item in items:
            by_parent.setdefault(item.parent, []).append(item)

        def build(parent: str, seen: frozenset[str]) -> list[dict[str, Any]]:
            out = []
            for item in by_parent.get(parent, []):
                node: dict[str, Any] = {"item": item.to_dict()}
                if item.is_folder and item.id not in seen:
                    node["children"] = build(item.id, seen | {item.id})
                out.append(node)
            return out

        return build(folder_id, frozenset())

    def path(self, item_id: str) -> str:
        """Human-readable path, e.g. ``/Books/Dune`` (``/Trash/...`` for trashed items)."""
        if item_id == xochitl.ROOT:
            return "/"
        if item_id == xochitl.TRASH:
            return "/Trash"
        records = self._records()
        parts: list[str] = []
        cur = item_id
        seen: set[str] = set()
        while cur not in (xochitl.ROOT, xochitl.TRASH) and cur not in seen:
            seen.add(cur)
            rec = records.get(cur)
            if rec is None:
                if cur == item_id:
                    raise NotFound(f"no document or folder with id {item_id!r}")
                break
            parts.append(str(rec.metadata.get("visibleName") or cur))
            cur = str(rec.metadata.get("parent") or "")
        if cur == xochitl.TRASH:
            parts.append("Trash")
        return "/" + "/".join(reversed(parts))

    def resolve(self, ref: str) -> Item:
        """Find an item by id or by path (``/Books/Dune``; ``/Trash/...`` for the trash)."""
        ref = ref.strip()
        if ref in ("", "/"):
            return ROOT_ITEM
        records = self._records()
        if ref in records:
            return self.get(ref)
        if ref.lower() in ("trash", "/trash"):
            return TRASH_ITEM
        parts = [p for p in PurePosixPath(ref).parts if p != "/"]
        items = self.items(include_trash=True)
        parent = xochitl.ROOT
        if parts and parts[0].lower() == "trash" and not any(
            i.parent == xochitl.ROOT and i.name == parts[0] for i in items
        ):
            parent = xochitl.TRASH
            parts = parts[1:]
        current: Item = ROOT_ITEM if parent == xochitl.ROOT else TRASH_ITEM
        for part in parts:
            siblings = [i for i in items if i.parent == current.id]
            matches = [i for i in siblings if i.name == part] or [
                i for i in siblings if i.name.casefold() == part.casefold()
            ]
            if not matches:
                raise NotFound(f"nothing called {part!r} in {self.path(current.id)}")
            # Prefer folders when walking, and the most recently modified on ties.
            matches.sort(key=lambda i: (not i.is_folder, -(i.last_modified.timestamp() if i.last_modified else 0)))
            current = matches[0]
        return current

    def search(self, query: str, *, include_trash: bool = False, type: str | None = None) -> list[Item]:
        """Items whose name or tags contain ``query`` (case-insensitive)."""
        q = query.casefold()
        return [
            i for i in self.items(include_trash=include_trash)
            if (q in i.name.casefold() or any(q in t.casefold() for t in i.tags))
            and (type is None or i.type == type)
        ]

    def recent(self, limit: int = 10) -> list[Item]:
        docs = [i for i in self.items() if i.type == "document" and i.last_opened]
        docs.sort(key=lambda i: i.last_opened, reverse=True)  # type: ignore[arg-type,return-value]
        return docs[:limit]

    def current(self) -> dict[str, Any]:
        """The document (and page) most recently open on the tablet."""
        recent = self.recent(1)
        if not recent:
            raise NotFound("no document has been opened yet")
        item = recent[0]
        page = item.current_page or 0
        return {"item": item, "page": page, "path": self.path(item.id)}

    # ------------------------------------------------------------------
    # Creating and editing
    # ------------------------------------------------------------------

    def _check_parent(self, parent: str, *, allow_trash: bool = False) -> None:
        if parent == xochitl.ROOT or (allow_trash and parent == xochitl.TRASH):
            return
        if parent == xochitl.TRASH:
            raise InvalidRequest("cannot create items directly in the trash")
        target = self.get(parent)
        if not target.is_folder:
            raise InvalidRequest(f"{target.name!r} is a document, not a folder")

    @staticmethod
    def _check_name(name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise InvalidRequest("name must not be empty")
        if len(name) > 255:
            raise InvalidRequest("name is too long")
        return name

    def create_folder(self, name: str, parent: str = xochitl.ROOT) -> Item:
        name = self._check_name(name)
        self._check_parent(parent)
        doc_id = xochitl.new_id()
        self.storage.apply([DocChange(doc_id, write={
            xochitl.content_name(doc_id): xochitl.dumps(xochitl.folder_content()),
            xochitl.metadata_name(doc_id): xochitl.dumps(xochitl.new_metadata(name, parent, xochitl.COLLECTION)),
        })])
        return self.get(doc_id)

    def ensure_folder(self, path: str) -> Item:
        """Return the folder at ``path``, creating missing folders along the way."""
        current = ROOT_ITEM
        for part in [p for p in PurePosixPath(path).parts if p != "/"]:
            existing = [i for i in self.children(current.id) if i.is_folder and i.name == part]
            current = existing[0] if existing else self.create_folder(part, current.id)
        return current

    def upload(self, data: bytes, filename: str, *, parent: str = xochitl.ROOT, name: str | None = None) -> Item:
        """Add a PDF, EPUB, or ``.rmdoc``/``.zip`` archive to the library."""
        ext = PurePosixPath(filename).suffix.lower().lstrip(".")
        if ext in ("rmdoc", "zip"):
            return self.import_archive(data, parent=parent, name=name)
        if ext not in xochitl.SOURCE_EXTENSIONS:
            raise InvalidRequest(f"unsupported file type .{ext}: upload a PDF, EPUB or .rmdoc")
        _check_magic(data, ext)
        name = self._check_name(name or PurePosixPath(filename).stem)
        self._check_parent(parent)
        doc_id = xochitl.new_id()
        content = xochitl.new_content(ext)
        if ext == "pdf":
            try:
                content["pageCount"] = rmpdf.page_count(data)
            except Exception as exc:  # noqa: BLE001 - pypdf raises many types
                raise InvalidRequest(f"this PDF could not be read: {exc}") from exc
        self.storage.apply([DocChange(doc_id, write={
            xochitl.source_name(doc_id, ext): data,
            xochitl.content_name(doc_id): xochitl.dumps(content),
            xochitl.metadata_name(doc_id): xochitl.dumps(xochitl.new_metadata(name, parent)),
        })])
        return self.get(doc_id)

    def create_notebook(self, name: str, parent: str = xochitl.ROOT, *, text: str | None = None) -> Item:
        """Create a notebook; ``text`` (Markdown-ish) becomes typed text on its first page."""
        name = self._check_name(name)
        self._check_parent(parent)
        doc_id, page_id = xochitl.new_id(), xochitl.new_id()
        files = {xochitl.content_name(doc_id): xochitl.dumps(xochitl.new_content("notebook", [page_id]))}
        if text:
            files[xochitl.page_rm_name(doc_id, page_id)] = write.text_page(text)
        files[xochitl.metadata_name(doc_id)] = xochitl.dumps(xochitl.new_metadata(name, parent))
        self.storage.apply([DocChange(doc_id, write=files)])
        return self.get(doc_id)

    def update(
        self,
        item_id: str,
        *,
        name: str | None = None,
        parent: str | None = None,
        pinned: bool | None = None,
        tags: list[str] | None = None,
    ) -> Item:
        """Rename, move, pin/unpin or re-tag an item in one change."""
        if item_id in (xochitl.ROOT, xochitl.TRASH):
            raise InvalidRequest("the root folder and the trash cannot be changed")
        rec = self._records().get(item_id)
        if rec is None:
            raise NotFound(f"no document or folder with id {item_id!r}")
        changes: dict[str, Any] = {}
        if name is not None:
            changes["visibleName"] = self._check_name(name)
        if parent is not None:
            self._check_parent(parent, allow_trash=True)
            if parent != xochitl.TRASH and self._is_descendant(parent, item_id):
                raise InvalidRequest("cannot move a folder into itself")
            changes["parent"] = parent
        if pinned is not None:
            changes["pinned"] = bool(pinned)
        files: dict[str, bytes] = {}
        if tags is not None:
            files[xochitl.content_name(item_id)] = xochitl.dumps(xochitl.with_tags(rec.content, tags))
        if changes or files:
            files[xochitl.metadata_name(item_id)] = xochitl.dumps(xochitl.updated_metadata(rec.metadata, **changes))
            self.storage.apply([DocChange(item_id, write=files)])
        return self.get(item_id)

    def _is_descendant(self, candidate: str, ancestor: str) -> bool:
        records = self._records()
        cur, seen = candidate, set()
        while cur not in (xochitl.ROOT, xochitl.TRASH) and cur not in seen:
            if cur == ancestor:
                return True
            seen.add(cur)
            rec = records.get(cur)
            if rec is None:
                return False
            cur = str(rec.metadata.get("parent") or "")
        return False

    def rename(self, item_id: str, name: str) -> Item:
        return self.update(item_id, name=name)

    def move(self, item_id: str, parent: str) -> Item:
        return self.update(item_id, parent=parent)

    def trash(self, item_id: str) -> Item:
        """Move to the trash, exactly like deleting on the tablet."""
        return self.update(item_id, parent=xochitl.TRASH)

    def restore(self, item_id: str, parent: str = xochitl.ROOT) -> Item:
        return self.update(item_id, parent=parent)

    def _descendants(self, folder_id: str) -> list[str]:
        records = self._records()
        children: dict[str, list[str]] = {}
        for rec in records.values():
            children.setdefault(str(rec.metadata.get("parent") or ""), []).append(rec.id)
        out, stack = [], [folder_id]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in out:
                    out.append(child)
                    stack.append(child)
        return out

    def delete(self, item_id: str, *, permanent: bool = False, recursive: bool = False) -> None:
        """Trash an item, or with ``permanent=True`` remove it for good."""
        if not permanent:
            self.trash(item_id)
            return
        item = self.get(item_id)
        if item.id in (xochitl.ROOT, xochitl.TRASH):
            raise InvalidRequest("use empty_trash() to clear the trash")
        ids = [item_id]
        if item.is_folder:
            below = self._descendants(item_id)
            if below and not recursive:
                raise InvalidRequest(f"folder {item.name!r} is not empty (use recursive delete)")
            ids += below
        self.storage.apply([DocChange(i, delete=True) for i in ids])

    def empty_trash(self) -> int:
        """Permanently delete everything in the trash. Returns the number of items removed."""
        ids = self._descendants(xochitl.TRASH)
        self.storage.apply([DocChange(i, delete=True) for i in ids])
        return len(ids)

    def replace_source(self, item_id: str, data: bytes, filename: str) -> Item:
        """Swap the PDF/EPUB behind a document, keeping its annotations and metadata."""
        item = self.get(item_id)
        ext = PurePosixPath(filename).suffix.lower().lstrip(".")
        if item.file_type != ext:
            raise InvalidRequest(f"document is a {item.file_type}; upload a .{item.file_type} to replace it")
        _check_magic(data, ext)
        rec = self._records()[item_id]
        files = {xochitl.source_name(item_id, ext): data}
        files[xochitl.metadata_name(item_id)] = xochitl.dumps(xochitl.updated_metadata(rec.metadata))
        self.storage.apply([DocChange(item_id, write=files)])
        return self.get(item_id)

    def import_archive(self, data: bytes, *, parent: str = xochitl.ROOT, name: str | None = None) -> Item:
        """Import a ``.rmdoc`` (the desktop app's export format) or an rmapi ``.zip``."""
        self._check_parent(parent)
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise InvalidRequest("not a valid .rmdoc/.zip archive") from exc
        names = [n for n in zf.namelist() if not n.endswith("/")]
        ids = {xochitl.doc_id_of(n) for n in names if n.endswith(".content")}
        if len(ids) != 1:
            raise InvalidRequest("archive must contain exactly one <id>.content file")
        old_id = ids.pop()
        new_id = old_id if old_id not in self._records() and _looks_like_uuid(old_id) else xochitl.new_id()
        files: dict[str, bytes] = {}
        for n in names:
            if not xochitl.is_safe_file_name(old_id, n):
                continue
            files[new_id + n[len(old_id):]] = zf.read(n)
        meta_name = xochitl.metadata_name(new_id)
        content = xochitl.loads(files.get(xochitl.content_name(new_id)))
        metadata = xochitl.loads(files.get(meta_name)) or xochitl.new_metadata(
            name or old_id, parent, xochitl.DOCUMENT
        )
        if not content.get("fileType") and any(n.endswith(".pdf") for n in files):
            content["fileType"] = "pdf"
            files[xochitl.content_name(new_id)] = xochitl.dumps(content)
        metadata = xochitl.updated_metadata(
            metadata,
            parent=parent,
            visibleName=self._check_name(name or str(metadata.get("visibleName") or "Imported")),
            deleted=False,
        )
        files[meta_name] = xochitl.dumps(metadata)
        self.storage.apply([DocChange(new_id, write=files)])
        return self.get(new_id)

    # ------------------------------------------------------------------
    # Reading document contents
    # ------------------------------------------------------------------

    def _document(self, item_id: str) -> Item:
        item = self.get(item_id)
        if item.type != "document":
            raise InvalidRequest(f"{item.name!r} is a {item.type}, not a document")
        return item

    def files(self, item_id: str) -> list[str]:
        self.get(item_id)
        return self.storage.list_files(item_id)

    def read_file(self, item_id: str, name: str) -> bytes:
        return self.storage.read_file(item_id, name)

    def source(self, item_id: str) -> tuple[bytes, str]:
        """The original PDF/EPUB of an imported document: ``(bytes, extension)``."""
        item = self._document(item_id)
        if item.file_type not in xochitl.SOURCE_EXTENSIONS:
            raise NotFound(f"{item.name!r} is a notebook; it has no original file (export it as PDF)")
        return self.storage.read_file(item_id, xochitl.source_name(item_id, item.file_type)), item.file_type

    def _render_pdf_source(self, item: Item, files: list[str]) -> bytes | None:
        """The PDF the tablet renders: the original PDF, or its EPUB conversion."""
        name = xochitl.source_name(item.id, "pdf")
        if name in files:
            return self.storage.read_file(item.id, name)
        return None

    def export_archive(self, item_id: str) -> bytes:
        """Every file of a document as a ``.rmdoc`` zip (re-importable with :meth:`upload`)."""
        self._document(item_id)
        names = [n for n in self.storage.list_files(item_id) if not any(x in n for x in _ARCHIVE_EXCLUDE)]
        contents = self.storage.read_files(item_id, names)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for n in names:
                zf.writestr(n, contents[n])
        return buf.getvalue()

    def pages(self, item_id: str) -> list[PageInfo]:
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        refs = self._page_refs(item, files)
        return [
            PageInfo(r.index, r.id, r.source_page, r.template, self._page_file(item_id, r, files) is not None)
            for r in refs
        ]

    def _page_refs(self, item: Item, files: set[str]) -> list[xochitl.PageRef]:
        refs = xochitl.page_refs(item.content)
        if not refs and item.file_type in xochitl.SOURCE_EXTENSIONS:
            pdf_name = xochitl.source_name(item.id, "pdf")
            if pdf_name in files:
                count = rmpdf.page_count(self.storage.read_file(item.id, pdf_name))
                refs = xochitl.page_refs(item.content, source_page_count=count)
        if not refs:
            # A notebook without a page list: fall back to the .rm files present.
            rm_pages = sorted(n for n in files if n.startswith(f"{item.id}/") and n.endswith(".rm"))
            refs = [
                xochitl.PageRef(i, PurePosixPath(n).stem, None) for i, n in enumerate(rm_pages)
            ]
        return refs

    @staticmethod
    def _page_file(doc_id: str, ref: xochitl.PageRef, files: set[str]) -> str | None:
        for name in (xochitl.page_rm_name(doc_id, ref.id), f"{doc_id}/{ref.index}.rm"):
            if name in files:
                return name
        return None

    def _scenes(self, item: Item, refs: list[xochitl.PageRef], files: set[str]) -> list[scene.PageScene | None]:
        names = {r.index: self._page_file(item.id, r, files) for r in refs}
        wanted = [n for n in names.values() if n]
        data = self.storage.read_files(item.id, wanted) if wanted else {}
        out: list[scene.PageScene | None] = []
        for r in refs:
            name = names[r.index]
            if name is None:
                out.append(None)
                continue
            try:
                out.append(scene.parse(data[name]))
            except Exception as exc:  # noqa: BLE001 - one broken page must not sink the export
                log.warning("could not parse %s: %s", name, exc)
                out.append(None)
        return out

    def page_scene(self, item_id: str, page: int) -> scene.PageScene:
        """Parsed contents of one page (an empty scene for untouched pages)."""
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        refs = self._page_refs(item, files)
        if not 0 <= page < len(refs):
            raise NotFound(f"page {page} does not exist (document has {len(refs)} pages)")
        parsed = self._scenes(item, [refs[page]], files)[0]
        return parsed or scene.PageScene(version=6)

    def render_page(
        self,
        item_id: str,
        page: int,
        fmt: str = "svg",
        *,
        scale: float = 1.0,
        background: bool = True,
    ) -> tuple[bytes, str]:
        """Render one page as ``svg``, ``png`` or ``pdf``. Returns ``(bytes, media type)``."""
        if fmt not in ("svg", "png", "pdf"):
            raise InvalidRequest("format must be svg, png or pdf")
        if not 0.05 <= scale <= 4:
            raise InvalidRequest("scale must be between 0.05 and 4")
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        refs = self._page_refs(item, files)
        if not 0 <= page < len(refs):
            raise NotFound(f"page {page} does not exist (document has {len(refs)} pages)")
        ref = refs[page]
        page_scene = self._scenes(item, [ref], files)[0] or scene.PageScene(version=6)
        source_pdf = None
        if ref.source_page is not None and (fmt == "pdf" or background):
            source_pdf = self._render_pdf_source(item, sorted(files))

        if fmt == "pdf":
            if source_pdf is not None:
                return rmpdf.annotate_pdf(source_pdf, [(ref.source_page, page_scene)]), "application/pdf"
            return rmpdf.notebook_pdf([page_scene]), "application/pdf"

        bg_image = None
        box = None
        if source_pdf is not None and background:
            width_px = round(page_scene.width * min(scale, 1.0) * 2)
            try:
                bg_image = rmpdf.rasterize_page(source_pdf, ref.source_page, width_px)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                log.warning("could not rasterize page: %s", exc)
            if bg_image is not None:
                box = _pdf_box_on_screen(source_pdf, ref.source_page, page_scene)  # type: ignore[arg-type]
        crop = "page" if source_pdf is not None else "content"
        if fmt == "svg":
            bg_bytes = None
            if bg_image is not None:
                buf = io.BytesIO()
                bg_image.save(buf, "PNG")
                bg_bytes = buf.getvalue()
            return render.to_svg(page_scene, background=bg_bytes, background_box=box, crop=crop).encode(), "image/svg+xml"
        return render.to_png(page_scene, scale=scale, background=bg_image, background_box=box, crop=crop), "image/png"

    def export_pdf(self, item_id: str) -> bytes:
        """The whole document as a PDF with every annotation drawn in (like the tablet's export)."""
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        refs = self._page_refs(item, files)
        scenes = self._scenes(item, refs, files)
        source_pdf = self._render_pdf_source(item, sorted(files)) if item.file_type in xochitl.SOURCE_EXTENSIONS else None
        if source_pdf is not None:
            return rmpdf.annotate_pdf(source_pdf, [(r.source_page, s) for r, s in zip(refs, scenes, strict=True)])
        if not refs:
            scenes = [None]
        return rmpdf.notebook_pdf(scenes)

    def pdf_text(self, item_id: str, pages: Iterable[int] | None = None) -> dict[int, str]:
        """Printed text of the PDF page behind each tablet page: ``{tablet page index: text}``.

        Uses the PDF's own text layer (scanned PDFs have none). EPUBs work once the
        tablet has converted them to PDF. Inserted pages and notebooks have no entry.
        """
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        source_pdf = self._render_pdf_source(item, sorted(files))
        if source_pdf is None:
            return {}
        wanted = set(pages) if pages is not None else None
        refs = [
            r for r in self._page_refs(item, files)
            if r.source_page is not None and (wanted is None or r.index in wanted)
        ]
        texts = rmpdf.page_texts(source_pdf, sorted({r.source_page for r in refs if r.source_page is not None}))
        return {r.index: texts[r.source_page] for r in refs if r.source_page in texts}

    def text(self, item_id: str) -> DocumentText:
        """Typed text and highlighted passages, page by page."""
        item = self._document(item_id)
        files = set(self.storage.list_files(item_id))
        refs = self._page_refs(item, files)
        scenes = self._scenes(item, refs, files)
        legacy = self._legacy_highlights(item_id, refs, files)
        pages = []
        for ref, sc in zip(refs, scenes, strict=True):
            highlights = list(legacy.get(ref.index, []))
            text = ""
            if sc is not None:
                if sc.text is not None:
                    text = sc.text.to_markdown()
                for hl in sc.highlights:
                    if hl.text.strip():
                        color = "#{:02x}{:02x}{:02x}".format(*hl.color[:3])
                        highlights.append(Highlight(hl.text, ref.index, color, ref.source_page))
            if text or highlights:
                pages.append(PageText(ref.index, text, highlights, ref.source_page))
        return DocumentText(item.id, item.name, pages, item.file_type)

    def _legacy_highlights(self, doc_id: str, refs: list[xochitl.PageRef], files: set[str]) -> dict[int, list[Highlight]]:
        """Highlights stored by firmware 2.x in ``<id>.highlights/<page>.json``."""
        wanted = {f"{doc_id}.highlights/{r.id}.json": r for r in refs}
        present = [n for n in wanted if n in files]
        if not present:
            return {}
        out: dict[int, list[Highlight]] = {}
        for name, data in self.storage.read_files(doc_id, present).items():
            try:
                groups = json.loads(data).get("highlights", [])
            except (ValueError, AttributeError):
                continue
            ref = wanted[name]
            for group in groups:
                for hl in group if isinstance(group, list) else [group]:
                    if isinstance(hl, dict) and hl.get("text"):
                        out.setdefault(ref.index, []).append(
                            Highlight(hl["text"], ref.index, source_page=ref.source_page)
                        )
        return out

    # ------------------------------------------------------------------

    def device_info(self) -> dict[str, Any]:
        info = self.storage.device_info()
        items = self.items(include_trash=True)
        info.update({
            "documents": sum(1 for i in items if i.type == "document"),
            "folders": sum(1 for i in items if i.is_folder),
            "capabilities": sorted(self.capabilities),
        })
        return info

    def restart_ui(self) -> None:
        restart = getattr(self.storage, "restart_ui", None)
        if restart is None:
            raise NotSupported(f"the {self.backend} backend cannot restart the tablet UI")
        restart()

    def batch_move(self, item_ids: Iterable[str], parent: str) -> list[Item]:
        """Move many items with a single write (one sync / one UI restart)."""
        self._check_parent(parent, allow_trash=True)
        records = self._records()
        changes = []
        for item_id in item_ids:
            rec = records.get(item_id)
            if rec is None:
                raise NotFound(f"no document or folder with id {item_id!r}")
            if parent != xochitl.TRASH and self._is_descendant(parent, item_id):
                raise InvalidRequest("cannot move a folder into itself")
            meta = xochitl.updated_metadata(rec.metadata, parent=parent)
            changes.append(DocChange(item_id, write={xochitl.metadata_name(item_id): xochitl.dumps(meta)}))
        self.storage.apply(changes)
        return [self.get(c.doc_id) for c in changes]


def _sort_key(item: Item) -> tuple:
    return (not item.is_folder, item.name.casefold())


def _looks_like_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _check_magic(data: bytes, ext: str) -> None:
    if ext == "pdf" and not data[:1024].lstrip().startswith(b"%PDF"):
        raise InvalidRequest("this does not look like a PDF file")
    if ext == "epub" and not data.startswith(b"PK"):
        raise InvalidRequest("this does not look like an EPUB file")


def _pdf_box_on_screen(pdf: bytes, index: int, page_scene: scene.PageScene) -> tuple[float, float, float, float]:
    """Where the tablet draws a PDF page, in scene pixels: ``(x, y, w, h)``."""
    w, h = rmpdf.page_sizes(pdf)[index]
    k = min(page_scene.width / w, page_scene.height / h)
    x = (page_scene.width - w * k) / 2 if page_scene.version >= 6 else 0.0
    return x, 0.0, w * k, h * k
