"""How each reMarkable tool looks, shared by the SVG, PNG and PDF renderers.

Every stroke is turned into a list of :class:`Segment` polylines with a single
width/colour/opacity each. Pressure-sensitive tools get many short segments so
the width can follow the pen; constant tools (fineliner, highlighter) get one.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .scene import Stroke

RGB = tuple[int, int, int]


@dataclass
class Segment:
    points: list[tuple[float, float]]
    width: float
    color: RGB
    #: 0..1. Values < 1 are rendered with real transparency (highlighter, shader).
    opacity: float
    cap: str  # "round" or "square"


#: Points per segment for pressure-sensitive tools.
_SEGMENT_POINTS = 4

_VARIABLE = {"ballpoint", "marker", "pencil", "mechanical_pencil", "paintbrush", "calligraphy", "shader", "eraser"}


def _fallback_width(stroke: Stroke) -> float:
    return max(1.0, 2.0 * stroke.thickness_scale)


def _width(stroke: Stroke, pts) -> float:
    widths = [p.width for p in pts if p.width > 0]
    return sum(widths) / len(widths) if widths else _fallback_width(stroke)


def _lighten(rgb: RGB, amount: float) -> RGB:
    """Blend towards white: cheap opacity for ink that should stay below highlights."""
    amount = min(max(amount, 0.0), 1.0)
    return tuple(int(round(c + (255 - c) * amount)) for c in rgb)  # type: ignore[return-value]


def segments(stroke: Stroke) -> list[Segment]:
    """Split a stroke into drawable segments. Returns ``[]`` for invisible tools."""
    pts = stroke.points
    if not pts or stroke.tool == "erase_area":
        return []
    rgb: RGB = stroke.color[:3]
    alpha = stroke.color[3] / 255 if len(stroke.color) > 3 else 1.0

    if stroke.tool == "highlighter":
        width = max(_width(stroke, pts), 15.0)
        opacity = 0.35 if alpha >= 0.99 else alpha
        return [Segment([(p.x, p.y) for p in pts], width, rgb, opacity, "square")]

    if stroke.tool == "fineliner":
        widths = [p.width for p in pts if p.width > 0]
        width = statistics.median(widths) if widths else _fallback_width(stroke)
        return [Segment([(p.x, p.y) for p in pts], width, rgb, 1.0, "round")]

    if stroke.tool == "eraser":
        rgb, alpha = (255, 255, 255), 1.0

    out: list[Segment] = []
    if len(pts) == 1:
        p = pts[0]
        return [Segment([(p.x, p.y), (p.x, p.y)], _width(stroke, pts), rgb, alpha, "round")]
    step = _SEGMENT_POINTS if stroke.tool in _VARIABLE else len(pts)
    for start in range(0, len(pts) - 1, step):
        chunk = pts[start : start + step + 1]  # overlap one point to stay connected
        width = _width(stroke, chunk)
        color = rgb
        opacity = alpha
        if stroke.tool == "pencil":
            pressure = sum(p.pressure for p in chunk) / len(chunk)
            color = _lighten(rgb, 0.55 - 0.5 * pressure)
        elif stroke.tool == "mechanical_pencil":
            color = _lighten(rgb, 0.3)
        elif stroke.tool == "ballpoint":
            pressure = sum(p.pressure for p in chunk) / len(chunk)
            color = _lighten(rgb, max(0.0, 0.25 - 0.3 * pressure))
        out.append(Segment([(p.x, p.y) for p in chunk], width, color, opacity, "round"))
    return out
