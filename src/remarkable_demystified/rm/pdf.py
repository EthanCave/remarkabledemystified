"""Write annotations as vector graphics into PDFs (pure Python, via pypdf).

Two jobs:

* :func:`annotate_pdf` draws each page's strokes onto the original PDF, giving
  the same "annotated PDF" the tablet exports.
* :func:`notebook_pdf` builds a PDF for a notebook, one page per tablet page.

Placement on PDF pages follows what xochitl does on screen: the PDF page is
scaled to fit the 1404x1872 display (``k = min(1404/w, 1872/h)`` pixels per
point). In v6 files ``x = 0`` is the centre of the page; in v3/v5 files the
page is anchored to the top-left corner.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    RectangleObject,
)

from .. import xochitl
from .render import text_lines
from .scene import PageScene
from .style import segments

PX_TO_PT = 72.0 / xochitl.RM2_DPI

log = logging.getLogger(__name__)


@dataclass
class Transform:
    """Maps tablet pixels (origin top-left, y down) to PDF points (y up)."""

    scale: float  # points per pixel
    dx: float
    dy: float  # PDF y of the page's top edge

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        return self.dx + x * self.scale, self.dy - y * self.scale


def notebook_transform(height_px: float) -> Transform:
    return Transform(PX_TO_PT, 0.0, height_px * PX_TO_PT)


def pdf_page_transform(box: RectangleObject, scene: PageScene) -> Transform:
    """Where the tablet drew this PDF page, as a pixel -> point transform."""
    left, bottom = float(box.left), float(box.bottom)
    w, h = float(box.width), float(box.height)
    screen_w = scene.width if scene.width else xochitl.RM2_WIDTH
    screen_h = scene.height if scene.height else xochitl.RM2_HEIGHT
    k = min(screen_w / w, screen_h / h)  # pixels per point
    if scene.version >= 6:
        # scene x is relative to the page centre; our scene shifted it by +width/2.
        dx = left + w / 2 - (screen_w / 2) / k
    else:
        dx = left
    return Transform(1.0 / k, dx, bottom + h)


#: Characters our text layout uses that the standard PDF fonts (WinAnsi) cannot show.
_WINANSI_SUBSTITUTES = {"\u2610": "[ ]", "\u2611": "[x]", "\u25e6": "-"}


def _pdf_string(s: str) -> bytes:
    """``s`` as the bytes of a PDF literal string in WinAnsiEncoding (cp1252), escaped."""
    for char, substitute in _WINANSI_SUBSTITUTES.items():
        s = s.replace(char, substitute)
    raw = s.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def content_stream(scene: PageScene, t: Transform) -> tuple[bytes, dict[str, float], bool]:
    """PDF drawing operators for ``scene``.

    Returns ``(operators, {gs_name: alpha}, uses_font)``.
    """
    ops: list[str | bytes] = ["q", "1 j"]
    alphas: dict[str, float] = {}

    def gs(alpha: float) -> str:
        name = f"RMa{int(round(alpha * 1000)):03d}"
        alphas[name] = alpha
        return name

    def rgb(color: Sequence[int]) -> str:
        return " ".join(f"{c / 255:.3f}" for c in color[:3])

    for hl in scene.highlights:
        ops.append(f"q /{gs(0.35)} gs {rgb(hl.color)} rg")
        for rect in hl.rects:
            x, y = t(rect.x, rect.y + rect.h)
            ops.append(f"{x:.2f} {y:.2f} {rect.w * t.scale:.2f} {rect.h * t.scale:.2f} re f")
        ops.append("Q")

    for stroke in scene.strokes:
        for seg in segments(stroke):
            cap = 2 if seg.cap == "square" else 1
            alpha = f"/{gs(seg.opacity)} gs " if seg.opacity < 1 else ""
            ops.append(f"q {alpha}{cap} J {rgb(seg.color)} RG {max(seg.width * t.scale, 0.1):.3f} w")
            pts = seg.points if len(seg.points) > 1 else seg.points * 2
            x, y = t(*pts[0])
            path = [f"{x:.2f} {y:.2f} m"]
            for px, py in pts[1:]:
                x, y = t(px, py)
                path.append(f"{x:.2f} {y:.2f} l")
            ops.append(" ".join(path) + " S Q")

    uses_font = False
    if scene.text is not None:
        for x, y, line, size, weight in text_lines(scene.text):
            uses_font = True
            px, py = t(x, y)
            font = "RMfB" if weight == "bold" else "RMf"
            ops.append(
                f"BT /{font} {size * t.scale:.2f} Tf 0 g {px:.2f} {py:.2f} Td (".encode()
                + _pdf_string(line)
                + b") Tj ET"
            )
    ops.append("Q")
    data = b"\n".join(op if isinstance(op, bytes) else op.encode("ascii") for op in ops)
    return data, alphas, uses_font


def _overlay_page(writer: PdfWriter, scene: PageScene, t: Transform, box: RectangleObject) -> PageObject:
    data, alphas, uses_font = content_stream(scene, t)
    page = PageObject.create_blank_page(width=float(box.width), height=float(box.height))
    page.mediabox = RectangleObject(box)
    resources = DictionaryObject()
    if alphas:
        ext = DictionaryObject()
        for name, alpha in alphas.items():
            ext[NameObject("/" + name)] = DictionaryObject({
                NameObject("/Type"): NameObject("/ExtGState"),
                NameObject("/CA"): FloatObject(alpha),
                NameObject("/ca"): FloatObject(alpha),
            })
        resources[NameObject("/ExtGState")] = ext
    if uses_font:
        fonts = DictionaryObject()
        for name, base in (("/RMf", "/Helvetica"), ("/RMfB", "/Helvetica-Bold")):
            fonts[NameObject(name)] = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject(base),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            })
        resources[NameObject("/Font")] = fonts
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(data)
    page[NameObject("/Contents")] = writer._add_object(stream)
    return page


def _grown_box(box: RectangleObject, scene: PageScene, t: Transform) -> RectangleObject:
    """``box`` enlarged to hold any ink written past the edge of the page.

    The tablet lets you write beyond a PDF page's border (or scroll a page
    down). Exporting into the original box would cut those strokes off.
    """
    ink = scene.ink_bounds()
    left, bottom, right, top = float(box.left), float(box.bottom), float(box.right), float(box.top)
    if ink is None:
        return RectangleObject([left, bottom, right, top])
    ax, ay = t(ink[0], ink[1])  # top-left of the ink, in PDF space (y up)
    bx, by = t(ink[2], ink[3])  # bottom-right
    return RectangleObject([
        min(left, ax, bx), min(bottom, ay, by), max(right, ax, bx), max(top, ay, by),
    ])


def _union(a: RectangleObject, b: RectangleObject) -> RectangleObject:
    return RectangleObject([
        min(float(a.left), float(b.left)), min(float(a.bottom), float(b.bottom)),
        max(float(a.right), float(b.right)), max(float(a.top), float(b.top)),
    ])


def _draw(writer: PdfWriter, page: PageObject, scene: PageScene, t: Transform) -> None:
    """Merge ``scene`` onto ``page``, growing the page if ink spills over its edges."""
    box = _grown_box(page.cropbox, scene, t)
    if box != page.cropbox:
        page.mediabox = _union(page.mediabox, box)
        page.cropbox = box
        for name in ("/TrimBox", "/BleedBox", "/ArtBox"):  # stale boxes would re-crop in some viewers
            if name in page:
                del page[name]
    page.merge_page(_overlay_page(writer, scene, t, box))


def annotate_pdf(
    source: bytes,
    pages: Sequence[tuple[int | None, PageScene | None]],
    *,
    blank_size: tuple[float, float] | None = None,
) -> bytes:
    """Draw annotations onto a PDF.

    ``pages`` lists the document's pages in tablet order as
    ``(source_page_index or None, scene or None)``. Pages inserted on the
    tablet (``None`` source index) become blank pages. Pages grow to fit ink
    written past their edges.
    """
    reader = PdfReader(io.BytesIO(source))
    writer = PdfWriter()
    for src_index, scene in pages:
        if src_index is not None and 0 <= src_index < len(reader.pages):
            writer.add_page(reader.pages[src_index])
            page = writer.pages[-1]
            if scene is not None and not scene.is_empty:
                _draw(writer, page, scene, pdf_page_transform(page.cropbox, scene))
        else:
            w, h = blank_size or (xochitl.RM2_WIDTH * PX_TO_PT, xochitl.RM2_HEIGHT * PX_TO_PT)
            page = writer.add_blank_page(width=w, height=h)
            if scene is not None and not scene.is_empty:
                k = min(scene.width / w, scene.height / h)
                _draw(writer, page, scene, Transform(1 / k, 0.0, h))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def notebook_pdf(scenes: Sequence[PageScene | None], *, grow: bool = True) -> bytes:
    """One PDF page per notebook page; pages grow to fit ink past any edge (long scrolls)."""
    writer = PdfWriter()
    for scene in scenes:
        if scene is None:
            writer.add_blank_page(width=xochitl.RM2_WIDTH * PX_TO_PT, height=xochitl.RM2_HEIGHT * PX_TO_PT)
            continue
        x0, y0, x1, y1 = scene.bounds() if grow else (0.0, 0.0, float(scene.width), float(scene.height))
        w, h = (x1 - x0) * PX_TO_PT, (y1 - y0) * PX_TO_PT
        page = writer.add_blank_page(width=w, height=h)
        t = Transform(PX_TO_PT, -x0 * PX_TO_PT, h + y0 * PX_TO_PT)
        if not scene.is_empty:
            page.merge_page(_overlay_page(writer, scene, t, page.mediabox))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def single_page(pdf: bytes, index: int) -> bytes:
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter()
    writer.add_page(reader.pages[index])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_texts(pdf: bytes, indices: Sequence[int]) -> dict[int, str]:
    """Extracted text layer of the given (0-based) PDF pages; pages without text are left out."""
    reader = PdfReader(io.BytesIO(pdf))
    out = {}
    for i in indices:
        if not 0 <= i < len(reader.pages):
            continue
        try:
            text = reader.pages[i].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - malformed pages must not sink the request
            log.warning("could not extract text from PDF page %d: %s", i, exc)
            continue
        if text.strip():
            out[i] = text
    return out


def page_count(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    return [(float(p.cropbox.width), float(p.cropbox.height)) for p in PdfReader(io.BytesIO(pdf)).pages]


def rasterize_page(pdf: bytes, index: int, width_px: int):
    """Render one PDF page to a Pillow image with pypdfium2, PyMuPDF or poppler.

    Returns ``None`` when none of them is installed (pages then render on white).
    """
    try:
        import pypdfium2  # optional: pip install 'remarkable-demystified[render]'
    except ImportError:
        pypdfium2 = None
    if pypdfium2 is not None:
        doc = pypdfium2.PdfDocument(pdf)
        try:
            page = doc[index]
            image = page.render(scale=width_px / page.get_width()).to_pil().convert("RGB")
            page.close()
            return image
        finally:
            doc.close()

    try:
        import fitz  # PyMuPDF (optional)
    except ImportError:
        fitz = None
    if fitz is not None:
        from PIL import Image

        fdoc = fitz.open(stream=pdf, filetype="pdf")
        fpage = fdoc[index]
        zoom = width_px / fpage.rect.width
        pix = fpage.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    import shutil
    import subprocess
    import tempfile

    if shutil.which("pdftoppm") is None:
        return None
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        src = f"{tmp}/in.pdf"
        with open(src, "wb") as f:
            f.write(pdf)
        subprocess.run(
            ["pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-png",
             "-scale-to-x", str(width_px), "-scale-to-y", "-1", "-singlefile", src, f"{tmp}/out"],
            check=True, capture_output=True, timeout=60,
        )
        return Image.open(f"{tmp}/out.png").convert("RGB")

