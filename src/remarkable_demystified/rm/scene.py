"""Parse ``.rm`` page files into one simple, version-independent model.

Three on-disk versions exist:

* **v3 / v5** ("lines" files, firmware 1.x-2.x): layers of strokes, each stroke a
  list of points with ``x, y, speed, tilt, width, pressure`` floats. The origin is
  the top-left corner of the screen.
* **v6** (firmware 3.x): a CRDT "scene tree" of tagged blocks holding layers,
  strokes, typed text and PDF text highlights. ``x == 0`` is the horizontal
  *centre* of the page, and groups can be anchored to a line of typed text.
  Parsed with `rmscene <https://github.com/ricklupton/rmscene>`_.

Everything is normalized into :class:`PageScene`, in screen pixels with the
origin at the top-left corner of the page.
"""

from __future__ import annotations

import io
import logging
import math
import struct
import textwrap
from dataclasses import dataclass, field

from .. import xochitl
from ..errors import InvalidRequest

log = logging.getLogger(__name__)


class _QuietRmsceneText(logging.Filter):
    """rmscene logs a false "Unknown formatting code" warning for bold on/off codes."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith("Unknown formatting code in text: ")


logging.getLogger("rmscene.text").addFilter(_QuietRmsceneText())

HEADER_V3 = b"reMarkable .lines file, version=3          "
HEADER_V5 = b"reMarkable .lines file, version=5          "
HEADER_V6 = b"reMarkable .lines file, version=6          "

#: Tool ids shared by all file versions (``rmscene.scene_items.Pen``).
TOOLS = {
    0: "paintbrush", 12: "paintbrush",
    1: "pencil", 14: "pencil",
    2: "ballpoint", 15: "ballpoint",
    3: "marker", 16: "marker",
    4: "fineliner", 17: "fineliner",
    5: "highlighter", 18: "highlighter",
    6: "eraser",
    7: "mechanical_pencil", 13: "mechanical_pencil",
    8: "erase_area",
    21: "calligraphy",
    23: "shader",
}

#: Colour ids (``rmscene.scene_items.PenColor``) to RGB. Highlighter and shader
#: strokes on 3.x firmware carry an explicit RGBA instead.
PALETTE: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 0),          # black
    1: (144, 144, 144),    # gray
    2: (255, 255, 255),    # white
    3: (251, 247, 25),     # yellow
    4: (0, 255, 0),        # green
    5: (255, 192, 203),    # pink
    6: (78, 105, 201),     # blue
    7: (179, 62, 57),      # red
    8: (125, 125, 125),    # gray overlap
    9: (255, 237, 117),    # highlight (default yellow)
    10: (161, 216, 125),   # green 2
    11: (139, 208, 229),   # cyan
    12: (183, 130, 205),   # magenta
    13: (247, 232, 81),    # yellow 2
}

#: Tools whose strokes leave nothing visible: the eraser paints white, erase-area is a lasso.
INVISIBLE_TOOLS = ("eraser", "erase_area")

#: Approximate line heights (px) of typed-text paragraph styles on 3.x firmware.
LINE_HEIGHTS = {"heading": 150}
DEFAULT_LINE_HEIGHT = 70
#: Rough average glyph width (px) per style, used to wrap long paragraphs.
CHAR_WIDTHS = {"heading": 26, "bold": 16}
DEFAULT_CHAR_WIDTH = 15
#: Distance from the text box's ``pos_y`` to the first baseline. Measured from
#: the same strokes saved before and after firmware started anchoring them to
#: text (rmscene's ``Lines_v2.rm`` vs ``Lines_v2_updated.rm``): 267.59 - 234.
FIRST_BASELINE = 33.59375
DEFAULT_TEXT_Y = 234.0

# Special anchor ids used by groups pinned to the top/bottom of the text.
_ANCHOR_TOP = 0xFFFFFFFFFFFE
_ANCHOR_BOTTOM = 0xFFFFFFFFFFFF

RGBA = tuple[int, int, int, int]


@dataclass
class Point:
    x: float
    y: float
    #: Rendered width of the stroke at this point, in pixels.
    width: float
    #: Pen pressure, 0..1.
    pressure: float
    speed: float = 0.0
    #: Pen tilt / direction in radians.
    tilt: float = 0.0


@dataclass
class Stroke:
    tool: str
    color: RGBA
    thickness_scale: float
    points: list[Point]


@dataclass
class HighlightRect:
    x: float
    y: float
    w: float
    h: float


@dataclass
class GlyphHighlight:
    """Text highlighted in a PDF/EPUB with the highlighter (firmware 3.x)."""

    text: str
    color: RGBA
    rects: list[HighlightRect]


@dataclass
class Paragraph:
    style: str
    text: str
    #: Visual lines after wrapping to the text box width.
    lines: list[str]
    #: Baseline of the paragraph's first line on the page (px).
    y: float


@dataclass
class TypedText:
    x: float
    y: float
    width: float
    paragraphs: list[Paragraph]

    def to_markdown(self) -> str:
        prefixes = {
            "heading": "# ",
            "bullet": "- ",
            "bullet2": "  - ",
            "checkbox": "- [ ] ",
            "checkbox_checked": "- [x] ",
        }
        lines = []
        for p in self.paragraphs:
            text = p.text.rstrip()
            if not text:
                lines.append("")
            elif p.style == "bold":
                lines.append(f"**{text}**")
            else:
                lines.append(prefixes.get(p.style, "") + text)
        return "\n".join(lines).strip("\n") + "\n" if lines else ""


@dataclass
class PageScene:
    version: int
    #: Page canvas size in pixels (1404x1872 on reMarkable 1/2).
    width: int = xochitl.RM2_WIDTH
    height: int = xochitl.RM2_HEIGHT
    strokes: list[Stroke] = field(default_factory=list)
    highlights: list[GlyphHighlight] = field(default_factory=list)
    text: TypedText | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.strokes or self.highlights or (self.text and self.text.paragraphs))

    def ink_bounds(self, *, include_text: bool = True) -> tuple[float, float, float, float] | None:
        """``(x0, y0, x1, y1)`` of pen strokes (and typed text); ``None`` if there are none.

        PDF text highlights (``GlyphRange``) are left out on purpose: their
        rectangles do not seem to share the stroke coordinate frame (the real
        samples place them partly off the page), so they must not resize pages.
        """
        xs: list[float] = []
        ys: list[float] = []
        for s in self.strokes:
            if s.tool in INVISIBLE_TOOLS:
                continue
            for p in s.points:
                half = p.width / 2
                xs += (p.x - half, p.x + half)
                ys += (p.y - half, p.y + half)
        if self.text and include_text:
            for para in self.text.paragraphs:
                if para.text.strip():
                    # estimated width of the longest line (+ room for a bullet), not the whole box
                    longest = max(len(line) for line in para.lines)
                    width = longest * CHAR_WIDTHS.get(para.style, DEFAULT_CHAR_WIDTH) + 40
                    xs += (self.text.x, self.text.x + min(self.text.width, width))
                    # para.y is the first baseline; leave room for ascenders
                    ys += (para.y - line_height(para.style), para.y + line_height(para.style) * len(para.lines))
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def bounds(self) -> tuple[float, float, float, float]:
        """``(x0, y0, x1, y1)`` covering the page and everything drawn on it."""
        x0, y0, x1, y1 = 0.0, 0.0, float(self.width), float(self.height)
        ink = self.ink_bounds()
        if ink is None:
            return x0, y0, x1, y1
        return min(x0, ink[0]), min(y0, ink[1]), max(x1, ink[2]), max(y1, ink[3])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_version(data: bytes) -> int:
    head = data[: len(HEADER_V6)]
    if head == HEADER_V6:
        return 6
    if head == HEADER_V5:
        return 5
    if head == HEADER_V3:
        return 3
    raise InvalidRequest("not a reMarkable .rm file (unknown header)")


def parse(data: bytes) -> PageScene:
    """Parse the bytes of a ``.rm`` file."""
    version = detect_version(data)
    if version == 6:
        return _parse_v6(data)
    return _parse_legacy(data, version)


# ---------------------------------------------------------------------------
# v3 / v5
# ---------------------------------------------------------------------------


def _parse_legacy(data: bytes, version: int) -> PageScene:
    scene = PageScene(version=version)
    offset = len(HEADER_V5)

    def read(fmt: str):
        nonlocal offset
        values = struct.unpack_from("<" + fmt, data, offset)
        offset += struct.calcsize("<" + fmt)
        return values

    try:
        (n_layers,) = read("i")
        for _ in range(n_layers):
            (n_strokes,) = read("i")
            for _ in range(n_strokes):
                if version == 3:
                    pen, color, _unk, width, n_seg = read("iiifi")
                else:
                    pen, color, _unk, width, _unk2, n_seg = read("iiifii")
                points = []
                for _ in range(n_seg):
                    x, y, speed, tilt, seg_width, pressure = read("ffffff")
                    points.append(Point(x, y, seg_width, pressure, speed, tilt))
                tool = TOOLS.get(pen, "ballpoint")
                rgb = PALETTE.get(color, PALETTE[0])
                if tool == "highlighter" and color in (0, 1, 2):
                    rgb = PALETTE[9]
                scene.strokes.append(Stroke(tool, (*rgb, 255), float(width), points))
    except struct.error:
        log.warning("truncated v%d .rm file; returning the strokes read so far", version)
    return scene


# ---------------------------------------------------------------------------
# v6
# ---------------------------------------------------------------------------


def _parse_v6(data: bytes) -> PageScene:
    from rmscene import read_tree
    from rmscene import scene_items as si

    tree = read_tree(io.BytesIO(data))
    scene = PageScene(version=6)
    info = getattr(tree, "scene_info", None)
    paper = getattr(info, "paper_size", None) if info is not None else None
    # Real tablets write 1404x1872, 1620x2160 or 954x1696; ignore anything absurd.
    if paper and len(paper) == 2 and all(isinstance(v, int) and 500 <= v <= 5000 for v in paper):
        scene.width, scene.height = int(paper[0]), int(paper[1])
    center = scene.width / 2

    anchors: dict[object, float] = {}
    if tree.root_text is not None:
        scene.text = _layout_text(tree.root_text, center, anchors)
    else:
        # xochitl's default text box starts at y=234 even when it is empty.
        anchors[("special", _ANCHOR_TOP)] = DEFAULT_TEXT_Y + FIRST_BASELINE
        anchors[("special", _ANCHOR_BOTTOM)] = DEFAULT_TEXT_Y + FIRST_BASELINE

    def anchor_offset(group) -> tuple[float, float]:
        if getattr(group, "anchor_id", None) is None:
            return 0.0, 0.0
        ax = group.anchor_origin_x.value if group.anchor_origin_x is not None else 0.0
        anchor = group.anchor_id.value
        key = ("special", anchor.part2) if anchor.part1 == 0 and anchor.part2 in (
            _ANCHOR_TOP, _ANCHOR_BOTTOM) else anchor
        if key not in anchors:
            log.debug("unknown text anchor %s", anchor)
        return float(ax), float(anchors.get(key, 0.0))

    def walk(group, dx: float, dy: float) -> None:
        visible = getattr(group, "visible", None)
        if visible is not None and visible.value is False:
            return
        ax, ay = anchor_offset(group)
        dx, dy = dx + ax, dy + ay
        for child in group.children.values():
            if isinstance(child, si.Group):
                walk(child, dx, dy)
            elif isinstance(child, si.Line):
                scene.strokes.append(_convert_line(child, center + dx, dy))
            elif isinstance(child, si.GlyphRange):
                scene.highlights.append(_convert_glyphs(child, center + dx, dy))

    walk(tree.root, 0.0, 0.0)
    return scene


def _convert_line(line, dx: float, dy: float) -> Stroke:
    tool = TOOLS.get(int(line.tool), "ballpoint")
    if line.color_rgba is not None:
        r, g, b, a = line.color_rgba
        color: RGBA = (r, g, b, a)
    else:
        rgb = PALETTE.get(int(line.color), PALETTE[0])
        color = (*rgb, 255)
    points = [
        Point(
            x=p.x + dx,
            y=p.y + dy,
            width=p.width / 4,
            pressure=p.pressure / 255,
            speed=p.speed / 4,
            tilt=p.direction * (2 * math.pi) / 255,
        )
        for p in line.points
    ]
    return Stroke(tool, color, float(line.thickness_scale), points)


def _convert_glyphs(glyphs, dx: float, dy: float) -> GlyphHighlight:
    if glyphs.color_rgba is not None:
        color: RGBA = tuple(glyphs.color_rgba)  # type: ignore[assignment]
    else:
        rgb = PALETTE.get(int(glyphs.color), PALETTE[9])
        color = (*rgb, 255)
    rects = [HighlightRect(r.x + dx, r.y + dy, r.w, r.h) for r in glyphs.rectangles]
    return GlyphHighlight(glyphs.text or "", color, rects)


def line_height(style: str) -> float:
    return LINE_HEIGHTS.get(style, DEFAULT_LINE_HEIGHT)


def _layout_text(text, center: float, anchors: dict[object, float]) -> TypedText:
    """Lay out typed text roughly the way xochitl does, recording anchor positions.

    Strokes written next to typed text live in groups anchored to a character;
    their coordinates are relative to that character's line baseline, so the
    baselines computed here decide where those strokes end up.
    """
    from rmscene.text import TextDocument

    doc = TextDocument.from_scene_item(text)
    first = text.pos_y + FIRST_BASELINE
    baseline = first
    paragraphs = []
    for p in doc.contents:
        style = p.style.value.name.lower() if hasattr(p.style.value, "name") else "plain"
        raw = str(p)
        per_line = max(1, int(text.width / CHAR_WIDTHS.get(style, DEFAULT_CHAR_WIDTH)))
        lines = textwrap.wrap(raw, per_line) or [""]
        height = line_height(style)
        anchors[p.start_id] = baseline
        offset = 0
        for span in p.contents:
            for char_id in span.i:
                anchors[char_id] = baseline + height * min(offset // per_line, len(lines) - 1)
                offset += 1
        paragraphs.append(Paragraph(style, raw, lines, baseline))
        baseline += height * len(lines)
    anchors[("special", _ANCHOR_TOP)] = first
    anchors[("special", _ANCHOR_BOTTOM)] = baseline
    return TypedText(x=text.pos_x + center, y=text.pos_y, width=text.width, paragraphs=paragraphs)
