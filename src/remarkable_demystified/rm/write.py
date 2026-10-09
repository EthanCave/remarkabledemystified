"""Create v6 ``.rm`` pages: typed text from Markdown-ish input.

The generated blocks follow ``rmscene.simple_text_document`` (which mirrors a
page created on the tablet) and add paragraph styles: headings, bold lines,
bullets and checkboxes, which the tablet's keyboard/Type Folio can also produce.
"""

from __future__ import annotations

import io
import re
import uuid

PARAGRAPH_PATTERNS = [
    (re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(.*)$"), "CHECKBOX_CHECKED"),
    (re.compile(r"^\s*[-*+]\s+\[\s\]\s+(.*)$"), "CHECKBOX"),
    (re.compile(r"^\s{2,}[-*+]\s+(.*)$"), "BULLET2"),
    (re.compile(r"^[-*+]\s+(.*)$"), "BULLET"),
    (re.compile(r"^#{1,6}\s+(.*)$"), "HEADING"),
    (re.compile(r"^\*\*(.+)\*\*\s*$"), "BOLD"),
]


def parse_markdown(markdown: str) -> list[tuple[str, str]]:
    """Split text into ``(style, text)`` paragraphs the tablet understands."""
    out = []
    for line in markdown.replace("\r\n", "\n").split("\n"):
        style, text = "PLAIN", line
        for pattern, name in PARAGRAPH_PATTERNS:
            m = pattern.match(line)
            if m:
                style, text = name, m.group(1)
                break
        out.append((style, text))
    while out and not out[-1][1].strip():
        out.pop()
    return out or [("PLAIN", "")]


def text_page(markdown: str, *, author: uuid.UUID | None = None) -> bytes:
    """A complete v6 ``.rm`` file containing ``markdown`` as typed text."""
    from rmscene import CrdtId, LwwValue, write_blocks
    from rmscene import scene_items as si
    from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem
    from rmscene.scene_stream import (
        AuthorIdsBlock,
        MigrationInfoBlock,
        PageInfoBlock,
        RootTextBlock,
        SceneGroupItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
    )

    paragraphs = parse_markdown(markdown)
    text = "\n".join(t for _, t in paragraphs)
    first_char = 16
    styles = {CrdtId(0, 0): LwwValue(CrdtId(1, 15), si.ParagraphStyle[paragraphs[0][0]])}
    offset = 0
    for i, (_style, para) in enumerate(paragraphs):
        offset += len(para)
        if i + 1 < len(paragraphs):
            # the "\n" ending this paragraph carries the style of the next one
            newline_id = CrdtId(1, first_char + offset)
            styles[newline_id] = LwwValue(CrdtId(1, 15), si.ParagraphStyle[paragraphs[i + 1][0]])
            offset += 1

    blocks = [
        AuthorIdsBlock(author_uuids={1: author or uuid.uuid4()}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(
            loads_count=1,
            merges_count=0,
            text_chars_count=len(text) + 1,
            text_lines_count=text.count("\n") + 1,
        ),
        SceneTreeBlock(tree_id=CrdtId(0, 11), node_id=CrdtId(0, 0), is_update=True, parent_id=CrdtId(0, 1)),
        RootTextBlock(
            block_id=CrdtId(0, 0),
            value=si.Text(
                items=CrdtSequence([
                    CrdtSequenceItem(
                        item_id=CrdtId(1, first_char),
                        left_id=CrdtId(0, 0),
                        right_id=CrdtId(0, 0),
                        deleted_length=0,
                        value=text,
                    )
                ]) if text else CrdtSequence([]),
                styles=styles,
                pos_x=-468.0,
                pos_y=234.0,
                width=936.0,
            ),
        ),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 11), label=LwwValue(CrdtId(0, 12), "Layer 1"))),
        SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(
                item_id=CrdtId(0, 13),
                left_id=CrdtId(0, 0),
                right_id=CrdtId(0, 0),
                deleted_length=0,
                value=CrdtId(0, 11),
            ),
        ),
    ]
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()
