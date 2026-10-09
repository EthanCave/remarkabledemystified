"""REST API over a :class:`~remarkable_demystified.library.Library`.

Run it with ``rmd serve`` (or ``uvicorn remarkable_demystified.server.app:app``)
and open http://127.0.0.1:8000/docs for interactive documentation.
"""

# No `from __future__ import annotations` here: FastAPI must evaluate the
# endpoint annotations, which reference objects local to create_app().
import hmac
import logging
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import __version__
from ..config import Settings, connect, load_settings
from ..errors import InvalidRequest, RemarkableError
from ..library import Library
from ..model import Item
from . import schemas

log = logging.getLogger(__name__)


def _content_disposition(name: str, ext: str) -> str:
    filename = f"{name}.{ext}"
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"


def create_app(
    library: Library | Callable[[], Library] | None = None,
    *,
    settings: Settings | None = None,
    api_token: str | None = None,
) -> FastAPI:
    """Build the API. ``library`` may be an instance or a factory (opened lazily)."""
    settings = settings or (load_settings() if library is None else Settings())
    token = api_token if api_token is not None else settings.api_token
    max_upload = settings.max_upload_mb * 1024 * 1024

    state: dict[str, Any] = {"library": library if isinstance(library, Library) else None}
    factory: Callable[[], Library] = (
        library if callable(library) and not isinstance(library, Library) else (lambda: connect(settings))
    )
    # One tablet, one connection: serialize access so caches and the SSH session stay consistent.
    lock = threading.RLock()

    @contextmanager
    def using() -> Iterator[Library]:
        with lock:
            if state["library"] is None:
                state["library"] = factory()
            yield state["library"]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if state["library"] is not None:
            state["library"].close()

    app = FastAPI(
        title="reMarkable Demystified API",
        version=__version__,
        description=(
            "Read, edit and organize the documents on a reMarkable tablet - over SSH, the USB "
            "web interface, or the reMarkable cloud.\n\n"
            "Item ids are the tablet's UUIDs; `\"\"` is the root folder and `\"trash\"` the trash. "
            "Most endpoints that take an id also accept a path via `GET /items/resolve`."
        ),
        lifespan=lifespan,
        responses={
            400: {"model": schemas.ErrorOut}, 401: {"model": schemas.ErrorOut},
            404: {"model": schemas.ErrorOut}, 501: {"model": schemas.ErrorOut},
        },
    )

    bearer = HTTPBearer(auto_error=False)

    def require_token(
        request: Request,
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if not token:
            return
        supplied = creds.credentials if creds else request.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            raise _HTTPError(401, "Unauthorized", "missing or wrong API token")

    auth = [Depends(require_token)]

    @app.exception_handler(RemarkableError)
    async def _remarkable_error(_request: Request, exc: RemarkableError) -> JSONResponse:
        return JSONResponse({"error": type(exc).__name__, "detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(_HTTPError)
    async def _http_error(_request: Request, exc: _HTTPError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"error": exc.error, "detail": exc.detail}, status_code=exc.status, headers=headers)

    def out(lib: Library, item: Item, with_path: bool = True) -> dict[str, Any]:
        data = item.to_dict()
        if with_path:
            data["path"] = lib.path(item.id)
        return data

    def need(lib: Library, capability: str) -> None:
        if capability not in lib.capabilities:
            from ..errors import NotSupported

            raise NotSupported(f"the {lib.backend} backend does not support {capability}")

    def read_upload(file: UploadFile) -> bytes:
        data = file.file.read(max_upload + 1)
        if len(data) > max_upload:
            raise InvalidRequest(f"file is larger than {settings.max_upload_mb} MB")
        return data

    # -- meta -------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/health", response_model=schemas.Health, tags=["device"])
    def health() -> dict[str, Any]:
        """Liveness check (no auth, does not contact the tablet)."""
        return {"ok": True, "backend": settings.backend, "version": __version__}

    @app.get("/device", tags=["device"], dependencies=auth)
    def device() -> dict[str, Any]:
        """Backend details: battery, storage, firmware (SSH), cloud generation, capabilities."""
        with using() as lib:
            return lib.device_info()

    @app.post("/device/restart-ui", status_code=204, tags=["device"], dependencies=auth)
    def restart_ui() -> Response:
        """Restart xochitl (the tablet UI) so it reloads its library. SSH only."""
        with using() as lib:
            lib.restart_ui()
        return Response(status_code=204)

    @app.post("/sync", status_code=204, tags=["device"], dependencies=auth)
    def sync() -> Response:
        """Drop cached listings and re-read the library from the tablet/cloud."""
        with using() as lib:
            lib.refresh()
        return Response(status_code=204)

    # -- browsing ----------------------------------------------------------------

    @app.get("/items", response_model=list[schemas.ItemOut], tags=["browse"], dependencies=auth)
    def list_items(
        parent: Annotated[str | None, Query(description='Only direct children of this folder ("" = root)')] = None,
        q: Annotated[str | None, Query(description="Search names and tags")] = None,
        type: Annotated[str | None, Query(pattern="^(folder|document|template)$")] = None,
        include_trash: bool = False,
    ) -> list[dict[str, Any]]:
        with using() as lib:
            if parent is not None:
                items = lib.children(parent)
            elif q:
                items = lib.search(q, include_trash=include_trash)
            else:
                items = lib.items(include_trash=include_trash)
            if q and parent is not None:
                items = [i for i in items if q.casefold() in i.name.casefold()]
            if type:
                items = [i for i in items if i.type == type]
            return [out(lib, i) for i in items]

    @app.get("/items/tree", response_model=list[schemas.TreeNode], tags=["browse"], dependencies=auth)
    def tree(parent: str = "") -> list[dict[str, Any]]:
        """The folder hierarchy below `parent`, nested."""
        with using() as lib:
            return lib.tree(parent)

    @app.get("/items/resolve", response_model=schemas.ItemOut, tags=["browse"], dependencies=auth)
    def resolve(path: Annotated[str, Query(description="e.g. /Books/Dune, or an id")]) -> dict[str, Any]:
        with using() as lib:
            return out(lib, lib.resolve(path))

    @app.get("/items/recent", response_model=list[schemas.ItemOut], tags=["browse"], dependencies=auth)
    def recent(limit: Annotated[int, Query(ge=1, le=200)] = 10) -> list[dict[str, Any]]:
        """Documents by when they were last opened on the tablet."""
        with using() as lib:
            return [out(lib, i) for i in lib.recent(limit)]

    @app.get("/items/current", response_model=schemas.CurrentOut, tags=["browse"], dependencies=auth)
    def current() -> dict[str, Any]:
        """The document (and page) most recently open on the tablet."""
        with using() as lib:
            cur = lib.current()
            return {"item": out(lib, cur["item"]), "page": cur["page"], "path": cur["path"]}

    @app.get("/items/{item_id}", response_model=schemas.ItemOut, tags=["browse"], dependencies=auth)
    def get_item(item_id: str) -> dict[str, Any]:
        with using() as lib:
            return out(lib, lib.get(item_id))

    # -- organizing ----------------------------------------------------------------

    @app.patch("/items/{item_id}", response_model=schemas.ItemOut, tags=["organize"], dependencies=auth)
    def update_item(item_id: str, body: schemas.ItemUpdate) -> dict[str, Any]:
        """Rename, move, pin or re-tag a document or folder."""
        with using() as lib:
            return out(lib, lib.update(item_id, **body.model_dump(exclude_none=True)))

    @app.delete("/items/{item_id}", status_code=204, tags=["organize"], dependencies=auth)
    def delete_item(item_id: str, permanent: bool = False, recursive: bool = False) -> Response:
        """Move to the trash (default), or delete for good with `permanent=true`."""
        with using() as lib:
            lib.delete(item_id, permanent=permanent, recursive=recursive)
        return Response(status_code=204)

    @app.post("/items/{item_id}/restore", response_model=schemas.ItemOut, tags=["organize"], dependencies=auth)
    def restore(item_id: str, body: schemas.RestoreIn | None = None) -> dict[str, Any]:
        with using() as lib:
            return out(lib, lib.restore(item_id, (body or schemas.RestoreIn()).parent))

    @app.post("/items/move", response_model=list[schemas.ItemOut], tags=["organize"], dependencies=auth)
    def move_many(body: schemas.MoveIn) -> list[dict[str, Any]]:
        """Move several items at once (a single sync / UI restart)."""
        with using() as lib:
            return [out(lib, i) for i in lib.batch_move(body.ids, body.parent)]

    @app.delete("/trash", response_model=schemas.DeletedOut, tags=["organize"], dependencies=auth)
    def empty_trash() -> dict[str, int]:
        with using() as lib:
            return {"deleted": lib.empty_trash()}

    @app.post("/folders", response_model=schemas.ItemOut, status_code=201, tags=["organize"], dependencies=auth)
    def create_folder(body: schemas.FolderIn) -> dict[str, Any]:
        with using() as lib:
            if body.path:
                return out(lib, lib.ensure_folder(body.path))
            if not body.name:
                raise InvalidRequest("give either name (+ parent) or path")
            return out(lib, lib.create_folder(body.name, body.parent))

    # -- adding content -----------------------------------------------------------

    @app.post("/documents", response_model=schemas.ItemOut, status_code=201, tags=["create"], dependencies=auth)
    def upload(
        file: Annotated[UploadFile, File(description="PDF, EPUB or .rmdoc")],
        parent: Annotated[str, Form()] = "",
        name: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Send a PDF, EPUB or `.rmdoc` archive to the tablet."""
        data = read_upload(file)
        with using() as lib:
            return out(lib, lib.upload(data, file.filename or "upload.pdf", parent=parent, name=name or None))

    @app.post("/notebooks", response_model=schemas.ItemOut, status_code=201, tags=["create"], dependencies=auth)
    def create_notebook(body: schemas.NotebookIn) -> dict[str, Any]:
        """Create a notebook, optionally with typed text on its first page."""
        with using() as lib:
            need(lib, "create_notebook")
            return out(lib, lib.create_notebook(body.name, body.parent, text=body.text))

    @app.put("/documents/{item_id}/file", response_model=schemas.ItemOut, tags=["create"], dependencies=auth)
    def replace_file(item_id: str, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
        """Replace the PDF/EPUB behind a document; annotations stay in place."""
        data = read_upload(file)
        with using() as lib:
            need(lib, "replace_source")
            return out(lib, lib.replace_source(item_id, data, file.filename or "upload"))

    # -- reading content ----------------------------------------------------------

    @app.get("/documents/{item_id}/file", tags=["read"], dependencies=auth,
             responses={200: {"content": {"application/pdf": {}, "application/epub+zip": {}}}})
    def original(item_id: str) -> Response:
        """The original PDF/EPUB, without annotations."""
        with using() as lib:
            item = lib.get(item_id)
            data, ext = lib.source(item_id)
        media = "application/pdf" if ext == "pdf" else "application/epub+zip"
        return Response(data, media_type=media, headers={"Content-Disposition": _content_disposition(item.name, ext)})

    @app.get("/documents/{item_id}/pdf", tags=["read"], dependencies=auth,
             responses={200: {"content": {"application/pdf": {}}}})
    def export_pdf(item_id: str, download: bool = False) -> Response:
        """The document as a PDF with handwriting, highlights and typed text drawn in."""
        with using() as lib:
            item = lib.get(item_id)
            data = lib.export_pdf(item_id)
        headers = {"Content-Disposition": _content_disposition(item.name, "pdf").replace(
            "attachment", "attachment" if download else "inline", 1)}
        return Response(data, media_type="application/pdf", headers=headers)

    @app.get("/documents/{item_id}/archive", tags=["read"], dependencies=auth,
             responses={200: {"content": {"application/zip": {}}}})
    def export_archive(item_id: str) -> Response:
        """Every file of the document as an `.rmdoc` (importable with POST /documents)."""
        with using() as lib:
            item = lib.get(item_id)
            data = lib.export_archive(item_id)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": _content_disposition(item.name, "rmdoc")})

    @app.get("/documents/{item_id}/pages", response_model=list[schemas.PageOut], tags=["read"], dependencies=auth)
    def pages(item_id: str) -> list[dict[str, Any]]:
        with using() as lib:
            return [p.to_dict() for p in lib.pages(item_id)]

    @app.get("/documents/{item_id}/pages/{page}", tags=["read"], dependencies=auth,
             responses={200: {"content": {"image/svg+xml": {}, "image/png": {}, "application/pdf": {}}}})
    def render_page(
        item_id: str,
        page: int,
        format: Annotated[str, Query(pattern="^(svg|png|pdf)$")] = "svg",
        scale: Annotated[float, Query(ge=0.05, le=4)] = 1.0,
        background: Annotated[bool, Query(description="Draw the PDF page underneath (PNG/SVG need PyMuPDF or poppler)")] = True,
    ) -> Response:
        """Render one page (0-based) as SVG, PNG or a single-page PDF."""
        with using() as lib:
            data, media = lib.render_page(item_id, page, format, scale=scale, background=background)
        return Response(data, media_type=media)

    @app.get("/documents/{item_id}/text", tags=["read"], dependencies=auth,
             responses={200: {"model": schemas.TextOut, "content": {"text/markdown": {}}}})
    def text(item_id: str, format: Annotated[str, Query(pattern="^(json|markdown)$")] = "json") -> Any:
        """Typed text and highlighted passages, page by page (as JSON or Markdown)."""
        with using() as lib:
            doc = lib.text(item_id)
        if format == "markdown":
            return PlainTextResponse(doc.to_markdown(), media_type="text/markdown")
        return doc.to_dict()

    @app.get("/documents/{item_id}/files", response_model=list[str], tags=["raw"], dependencies=auth)
    def raw_files(item_id: str) -> list[str]:
        """The raw xochitl files that make up a document."""
        with using() as lib:
            return lib.files(item_id)

    @app.get("/documents/{item_id}/files/{name:path}", tags=["raw"], dependencies=auth)
    def raw_file(item_id: str, name: str) -> Response:
        """Download one raw file (e.g. `<id>.content` or `<id>/<page>.rm`)."""
        with using() as lib:
            data = lib.read_file(item_id, name)
        media = "application/json" if name.endswith((".metadata", ".content", ".json")) else "application/octet-stream"
        return Response(data, media_type=media)

    return app


class _HTTPError(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.error, self.detail = status, error, detail


def __getattr__(name: str) -> FastAPI:
    # `uvicorn remarkable_demystified.server.app:app` builds the app from settings on demand.
    if name == "app":
        return create_app()
    raise AttributeError(name)
