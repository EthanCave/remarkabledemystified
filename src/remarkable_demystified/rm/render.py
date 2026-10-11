"""Render a :class:`~.scene.PageScene` to SVG or PNG."""

from __future__ import annotations

import base64
import io
from typing import Any
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw, ImageFont

from .scene import PageScene, TypedText
from .style import segments

#: Font size (px) and weight of typed text per paragraph style.
TEXT_STYLES = {
    "heading": (52, "bold"),
    "bold": (32, "bold"),
}
DEFAULT_TEXT_STYLE = (32, "normal")
BULLETS = {"bullet": "• ", "bullet2": "◦ ", "checkbox": "☐ ", "checkbox_checked": "☑ "}


def text_lines(text: TypedText):
    """Yield ``(x, baseline_y, string, size, weight)`` for every visual line."""
    from .scene import line_height

    for para in text.paragraphs:
        size, weight = TEXT_STYLES.get(para.style, DEFAULT_TEXT_STYLE)
        prefix = BULLETS.get(para.style, "")
        indent = 40 if para.style == "bullet2" else 0
        for i, line in enumerate(para.lines):
            if not line:
                continue
            y = para.y + i * line_height(para.style)
            yield text.x + indent, y, (prefix if i == 0 else "") + line, size, weight


Box = tuple[float, float, float, float]


def page_box(scene: PageScene, crop: str | Box = "page") -> Box:
    """``(x0, y0, x1, y1)`` to draw: the page itself (``"page"``), the page grown to fit
    everything drawn on it (``"content"``), or an explicit box."""
    if isinstance(crop, tuple):
        return crop
    if crop == "content":
        return scene.bounds()
    return 0.0, 0.0, float(scene.width), float(scene.height)


def to_svg(
    scene: PageScene,
    *,
    background: bytes | None = None,
    background_mime: str = "image/png",
    background_box: tuple[float, float, float, float] | None = None,
    crop: str | Box = "content",
) -> str:
    """Render the page as an SVG document (units: tablet pixels)."""
    x0, y0, x1, y1 = page_box(scene, crop)
    w, h = x1 - x0, y1 - y0
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}">',
        f'<rect x="{x0:.2f}" y="{y0:.2f}" width="{w:.2f}" height="{h:.2f}" fill="#fff"/>',
    ]
    if background is not None:
        data = base64.b64encode(background).decode()
        bx, by, bw, bh = background_box or (0, 0, scene.width, scene.height)
        out.append(
            f'<image x="{bx:.2f}" y="{by:.2f}" width="{bw:.2f}" height="{bh:.2f}" '
            f'preserveAspectRatio="none" href="data:{background_mime};base64,{data}"/>'
        )
    for hl in scene.highlights:
        r, g, b = hl.color[:3]
        for rect in hl.rects:
            out.append(
                f'<rect x="{rect.x:.2f}" y="{rect.y:.2f}" width="{rect.w:.2f}" height="{rect.h:.2f}" '
                f'fill="rgb({r},{g},{b})" fill-opacity="0.35"><title>{escape(hl.text)}</title></rect>'
            )
    if scene.text is not None:
        out.append('<g font-family="Noto Sans, DejaVu Sans, Helvetica, Arial, sans-serif" fill="#000">')
        for x, y, line, size, weight in text_lines(scene.text):
            weight_attr = ' font-weight="bold"' if weight == "bold" else ""
            out.append(f'<text x="{x:.2f}" y="{y:.2f}" font-size="{size}"{weight_attr}>{escape(line)}</text>')
        out.append("</g>")
    out.append('<g fill="none" stroke-linejoin="round">')
    for stroke in scene.strokes:
        for seg in segments(stroke):
            pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in seg.points)
            r, g, b = seg.color
            opacity = f' stroke-opacity="{seg.opacity:.3f}"' if seg.opacity < 1 else ""
            out.append(
                f'<polyline points="{pts}" stroke="rgb({r},{g},{b})" stroke-width="{seg.width:.2f}" '
                f'stroke-linecap="{seg.cap}"{opacity}/>'
            )
    out.append("</g></svg>")
    return "\n".join(out)


def _font(size: int, bold: bool) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def to_image(
    scene: PageScene,
    *,
    scale: float = 1.0,
    background: Image.Image | None = None,
    background_box: tuple[float, float, float, float] | None = None,
    crop: str | Box = "content",
    supersample: int = 2,
) -> Image.Image:
    """Render the page to a Pillow image (RGB)."""
    x0, y0, x1, y1 = page_box(scene, crop)
    k = scale * supersample
    size = (max(1, round((x1 - x0) * k)), max(1, round((y1 - y0) * k)))
    canvas = Image.new("RGB", size, "white")
    if background is not None:
        bx, by, bw, bh = background_box or (0, 0, scene.width, scene.height)
        bg = background.convert("RGB").resize((max(1, round(bw * k)), max(1, round(bh * k))), Image.Resampling.LANCZOS)
        canvas.paste(bg, (round((bx - x0) * k), round((by - y0) * k)))

    def tx(x: float, y: float) -> tuple[float, float]:
        return (x - x0) * k, (y - y0) * k

    # Translucent marks first (highlights, highlighter, shader) so ink stays on top.
    translucent: list[tuple[str, tuple[int, int, int], float, Any]] = []
    for hl in scene.highlights:
        for rect in hl.rects:
            translucent.append(("rect", hl.color[:3], 0.35, rect))
    opaque: list[tuple[str, tuple[int, int, int], float, Any]] = []
    for stroke in scene.strokes:
        for seg in segments(stroke):
            (translucent if seg.opacity < 1 else opaque).append(("seg", seg.color, seg.opacity, seg))

    # One mask per colour/opacity: overlapping marks of the same highlighter don't
    # stack up darker (matching the tablet), and it keeps big pages fast.
    masks: dict[tuple, Image.Image] = {}
    for kind, color, opacity, obj in translucent:
        key = (color, opacity)
        if key not in masks:
            masks[key] = Image.new("L", size, 0)
        mask = ImageDraw.Draw(masks[key])
        if kind == "rect":
            ax, ay = tx(obj.x, obj.y)
            bx, by = tx(obj.x + obj.w, obj.y + obj.h)
            mask.rectangle([ax, ay, bx, by], fill=255)
        else:
            _draw_segment(mask, obj, tx, k, fill=255)
    for (color, opacity), layer in masks.items():
        layer = layer.point(lambda v, o=opacity: int(v * o))
        canvas.paste(Image.new("RGB", size, color), (0, 0), layer)

    draw = ImageDraw.Draw(canvas)
    for _kind, color, _opacity, seg in opaque:
        _draw_segment(draw, seg, tx, k, fill=color)

    if scene.text is not None:
        for x, y, line, fsize, weight in text_lines(scene.text):
            font = _font(round(fsize * k), weight == "bold")
            draw.text(tx(x, y), line, fill=(0, 0, 0), font=font, anchor="ls")

    if supersample > 1:
        canvas = canvas.resize(
            (max(1, round(size[0] / supersample)), max(1, round(size[1] / supersample))),
            Image.Resampling.LANCZOS,
        )
    return canvas


def _draw_segment(draw: ImageDraw.ImageDraw, seg, tx, k: float, fill) -> None:
    pts = [tx(x, y) for x, y in seg.points]
    width = max(1, round(seg.width * k))
    if len(pts) == 1 or all(p == pts[0] for p in pts):
        x, y = pts[0]
        r = width / 2
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)
        return
    draw.line(pts, fill=fill, width=width, joint="curve")
    if seg.cap == "round" and width > 2:
        r = width / 2
        for x, y in (pts[0], pts[-1]):
            draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def to_png(
    scene: PageScene,
    *,
    scale: float = 1.0,
    background: Image.Image | None = None,
    background_box: tuple[float, float, float, float] | None = None,
    crop: str | Box = "content",
) -> bytes:
    buf = io.BytesIO()
    image = to_image(scene, scale=scale, background=background, background_box=background_box, crop=crop)
    image.save(buf, "PNG", optimize=True)
    return buf.getvalue()
