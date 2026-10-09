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


def _escape_pdf_text(s: str) -> str:
    s = s.encode("cp1252", errors="replace").decode("cp1252")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def content_stream(scene: PageScene, t: Transform) -> tuple[bytes, dict[str, float], bool]:
    """PDF drawing operators for ``scene``.

    Returns ``(operators, {gs_name: alpha}, uses_font)``.
    """
    ops: list[str] = ["q", "1 j"]
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
                f"BT /{font} {size * t.scale:.2f} Tf 0 g {px:.2f} {py:.2f} Td "
                f"({_escape_pdf_text(line)}) Tj ET"
            )
    ops.append("Q")
    return "\n".join(ops).encode("latin-1"), alphas, uses_font


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


def annotate_pdf(
    source: bytes,
    pages: Sequence[tuple[int | None, PageScene | None]],
    *,
    blank_size: tuple[float, float] | None = None,
) -> bytes:
    """Draw annotations onto a PDF.

    ``pages`` lists the document's pages in tablet order as
    ``(source_page_index or None, scene or None)``. Pages inserted on the
    tablet (``None`` source index) become blank pages.
    """
    reader = PdfReader(io.BytesIO(source))
    writer = PdfWriter()
    for src_index, scene in pages:
        if src_index is not None and 0 <= src_index < len(reader.pages):
            writer.add_page(reader.pages[src_index])
            page = writer.pages[-1]
            box = page.cropbox
            if scene is not None and not scene.is_empty:
                page.merge_page(_overlay_page(writer, scene, pdf_page_transform(box, scene), box))
        else:
            w, h = blank_size or (xochitl.RM2_WIDTH * PX_TO_PT, xochitl.RM2_HEIGHT * PX_TO_PT)
            page = writer.add_blank_page(width=w, height=h)
            if scene is not None and not scene.is_empty:
                box = page.mediabox
                k = min(scene.width / w, scene.height / h)
                t = Transform(1 / k, 0.0, h)
                page.merge_page(_overlay_page(writer, scene, t, box))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def notebook_pdf(scenes: Sequence[PageScene | None], *, grow: bool = True) -> bytes:
    """One PDF page per notebook page; pages grow downwards to fit long scrolls."""
    writer = PdfWriter()
    for scene in scenes:
        if scene is None:
            writer.add_blank_page(width=xochitl.RM2_WIDTH * PX_TO_PT, height=xochitl.RM2_HEIGHT * PX_TO_PT)
            continue
        x0, y0, x1, y1 = scene.bounds() if grow else (0, 0, scene.width, scene.height)
        y0 = min(0.0, y0)
        height_px = y1 - y0
        w, h = scene.width * PX_TO_PT, height_px * PX_TO_PT
        page = writer.add_blank_page(width=w, height=h)
        t = Transform(PX_TO_PT, 0.0, (height_px + y0) * PX_TO_PT)
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


def page_count(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def page_sizes(pdf: bytes) -> list[tuple[float, float]]:
    return [(float(p.cropbox.width), float(p.cropbox.height)) for p in PdfReader(io.BytesIO(pdf)).pages]


def rasterize_page(pdf: bytes, index: int, width_px: int):
    """Render one PDF page to a Pillow image, if PyMuPDF or poppler is available.

    Returns ``None`` when no rasterizer is installed (pages then render on white).
    """
    try:
        import fitz  # PyMuPDF (optional)
    except ImportError:
        fitz = None
    if fitz is not None:
        from PIL import Image

        doc = fitz.open(stream=pdf, filetype="pdf")
        page = doc[index]
        zoom = width_px / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
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

