"""Step 4 — Serialization: emit an indented pseudo-markup representation."""

from __future__ import annotations

from aura.perception.reconstruct import OrderedNode


def serialize(node: OrderedNode, indent: int = 0) -> str:
    """Emit an indented pseudo-markup string for *node* and its children.

    Never raises.
    """
    prefix = "  " * indent
    bbox_str = _fmt_bbox(node.bbox)

    if node.region_type == "root":
        line = f"{prefix}(root) {bbox_str}"
    elif node.region_type == "text":
        text_repr = _fmt_text(node.text)
        line = f"{prefix}(text) {bbox_str} {text_repr}"
    elif node.region_type == "image":
        line = f"{prefix}(image) {bbox_str} [image-region bbox={bbox_str}]"
    elif node.region_type == "fill":
        line = f"{prefix}(fill) {bbox_str}"
    elif node.region_type == "divider":
        line = f"{prefix}(divider) {bbox_str}"
    else:
        line = f"{prefix}({node.region_type}) {bbox_str}"

    # Relations
    rel_parts: list[str] = []
    if node.relations.get("right_of"):
        bboxes = ", ".join(_fmt_bbox(b) for b in node.relations["right_of"])
        rel_parts.append(f"right_of=({bboxes})")
    if node.relations.get("below"):
        bboxes = ", ".join(_fmt_bbox(b) for b in node.relations["below"])
        rel_parts.append(f"below=({bboxes})")
    if node.relations.get("contains"):
        bboxes = ", ".join(_fmt_bbox(b) for b in node.relations["contains"])
        rel_parts.append(f"contains=({bboxes})")

    if rel_parts:
        line += f"\n{prefix}  relations: {' '.join(rel_parts)}"

    lines = [line]
    for child in node.children:
        lines.append(serialize(child, indent + 1))

    return "\n".join(lines)


def _fmt_bbox(bbox: tuple[int, int, int, int]) -> str:
    return f"[{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}]"


def _fmt_text(text: str) -> str:
    """Truncate long text for display."""
    if not text:
        return '""'
    # Take first line, truncate at 80 chars
    first_line = text.split("\n")[0]
    if len(first_line) > 80:
        first_line = first_line[:77] + "..."
    # Escape quotes and backslashes
    escaped = first_line.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
