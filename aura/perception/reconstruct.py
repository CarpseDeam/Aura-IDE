"""Step 3 — Reconstruction: build an ordered semantic tree from regions + OCR tokens."""

from __future__ import annotations

from dataclasses import dataclass, field

from aura.perception.ocr import Token
from aura.perception.segment import RegionNode


@dataclass
class OrderedNode:
    """A node in the ordered, reading-order-aware tree."""

    region_type: str
    bbox: tuple[int, int, int, int]  # left, top, width, height
    text: str = ""
    children: list[OrderedNode] = field(default_factory=list)
    relations: dict[str, list[tuple[int, int, int, int]]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection-over-union of two bounding boxes (left, top, width, height)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(ax, bx)
    iy = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    inter = max(0, ix2 - ix) * max(0, iy2 - iy)
    area_a = aw * ah
    area_b = bw * bh
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _bbox_center(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    return (bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2)


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------

def reconstruct(root: RegionNode, tokens_map: dict[int, list[Token]]) -> OrderedNode:
    """Walk the region tree, group OCR tokens into lines/paragraphs, assign
    reading order, and attach spatial relations.

    *tokens_map* maps ``id(region_node)`` to its list of OCR tokens.
    """
    # First, assign a counter-based id to each RegionNode so we can look up tokens
    # even though RegionNodes are not hashable.
    _counter = 0
    _id_map: dict[int, RegionNode] = {}  # counter -> RegionNode
    _reverse_id: dict[int, int] = {}  # id(RegionNode) -> counter

    def _assign_ids(node: RegionNode) -> None:
        nonlocal _counter
        cid = _counter
        _id_map[cid] = node
        _reverse_id[id(node)] = cid
        _counter += 1
        for child in node.children:
            _assign_ids(child)

    _assign_ids(root)

    def _build_ordered(node: RegionNode) -> OrderedNode:
        tokens = tokens_map.get(id(node), [])

        # Group tokens into lines, then paragraphs
        text = _tokens_to_text(tokens)

        ordered_children: list[OrderedNode] = []
        for child in node.children:
            ordered_children.append(_build_ordered(child))

        # Sort children by reading order
        ordered_children = _sort_by_reading_order(ordered_children)

        # Attach spatial relations between siblings
        relations = _compute_relations(ordered_children)

        return OrderedNode(
            region_type=node.region_type,
            bbox=node.bbox,
            text=text,
            children=ordered_children,
            relations=relations,
        )

    ordered = _build_ordered(root)
    return ordered


# ---------------------------------------------------------------------------
# token grouping
# ---------------------------------------------------------------------------

def _tokens_to_text(tokens: list[Token]) -> str:
    """Group tokens into lines, then paragraphs, return concatenated text."""
    if not tokens:
        return ""

    # Sort by y-center
    sorted_tokens = sorted(tokens, key=lambda t: t.bbox[1] + t.bbox[3] // 2)

    # Compute median token height
    heights = [t.bbox[3] for t in sorted_tokens if t.bbox[3] > 0]
    if not heights:
        return " ".join(t.text for t in sorted_tokens)
    median_h = sorted(heights)[len(heights) // 2]

    # Group into lines: tokens whose y-centers differ by <= half median height
    lines: list[list[Token]] = []
    for tok in sorted_tokens:
        cy = tok.bbox[1] + tok.bbox[3] // 2
        if not lines:
            lines.append([tok])
            continue
        last_cy = lines[-1][-1].bbox[1] + lines[-1][-1].bbox[3] // 2
        if abs(cy - last_cy) <= median_h // 2:
            lines[-1].append(tok)
        else:
            lines.append([tok])

    # Within each line, sort left-to-right
    for line in lines:
        line.sort(key=lambda t: t.bbox[0] + t.bbox[2] // 2)

    # Group lines into paragraphs
    paragraphs: list[list[list[Token]]] = []
    for line in lines:
        if not paragraphs:
            paragraphs.append([line])
            continue
        prev_line = paragraphs[-1][-1]
        prev_left = prev_line[0].bbox[0]
        prev_bottom = max(t.bbox[1] + t.bbox[3] for t in prev_line)
        prev_line_h = max(t.bbox[3] for t in prev_line) or 1

        cur_left = line[0].bbox[0]
        cur_top = min(t.bbox[1] for t in line)

        # Same paragraph if x-start within 10px and vertical gap <= 2x prev line height
        if abs(cur_left - prev_left) <= 10 and (cur_top - prev_bottom) <= 2 * prev_line_h:
            paragraphs[-1].append(line)
        else:
            paragraphs.append([line])

    # Build text: lines within a paragraph joined by space, paragraphs by newline
    para_texts: list[str] = []
    for para in paragraphs:
        line_texts: list[str] = []
        for line in para:
            line_texts.append(" ".join(t.text for t in line))
        para_texts.append("\n".join(line_texts))

    return "\n\n".join(para_texts)


# ---------------------------------------------------------------------------
# reading order
# ---------------------------------------------------------------------------

def _sort_by_reading_order(children: list[OrderedNode]) -> list[OrderedNode]:
    """Sort sibling nodes by reading order (column-aware)."""
    if len(children) < 2:
        return children

    # Collect x-centers of text regions to detect columns
    text_centers = [
        _bbox_center(c.bbox)[0]
        for c in children
        if c.region_type == "text"
    ]
    if not text_centers:
        # No text nodes — sort by y then x
        return sorted(children, key=lambda c: (c.bbox[1], c.bbox[0]))

    # Two-column detection: split by median x
    sorted_x = sorted(text_centers)
    median_x = sorted_x[len(sorted_x) // 2]

    left_count = sum(1 for cx in text_centers if cx <= median_x)
    right_count = len(text_centers) - left_count

    # If both columns have significant content, use column-aware ordering
    if left_count >= 2 and right_count >= 2:
        def _column_order(node: OrderedNode) -> tuple[int, int, int]:
            cx = _bbox_center(node.bbox)[0]
            col = 0 if cx <= median_x else 1
            return (col, node.bbox[1], node.bbox[0])
        return sorted(children, key=_column_order)

    # Single column — sort by y then x
    return sorted(children, key=lambda c: (c.bbox[1], c.bbox[0]))


# ---------------------------------------------------------------------------
# spatial relations
# ---------------------------------------------------------------------------

def _compute_relations(
    children: list[OrderedNode],
) -> dict[str, list[tuple[int, int, int, int]]]:
    """Compute spatial relations between sibling nodes."""
    relations: dict[str, list[tuple[int, int, int, int]]] = {
        "contains": [],
        "right_of": [],
        "below": [],
    }

    for i, a in enumerate(children):
        for b in children[i + 1:]:
            ab = a.bbox
            bb = b.bbox

            # Contains (parent-child already handled; this is sibling containment)
            iou_val = _iou(ab, bb)
            if iou_val > 0:
                # One contains the other if IOU is high relative to the smaller area
                area_a = ab[2] * ab[3]
                area_b = bb[2] * bb[3]
                if area_a > 0 and area_b > 0:
                    if _iou(ab, bb) >= min(area_a, area_b) / max(area_a, area_b) * 0.5:
                        relations["contains"].append(bb)

            # Right of: a's left > b's right (within 10px tolerance)
            if ab[0] > bb[0] + bb[2] - 10:
                relations["right_of"].append(bb)

            # Below: a's top > b's bottom (within 10px tolerance)
            if ab[1] > bb[1] + bb[3] - 10:
                relations["below"].append(bb)

    return relations
