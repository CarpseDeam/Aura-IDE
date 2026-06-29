"""Entry point — screenshot decompiler that turns base64 PNG into an indented image AST."""

from __future__ import annotations

from aura.perception.ocr import transcribe
from aura.perception.reconstruct import reconstruct
from aura.perception.segment import RegionNode, segment
from aura.perception.serialize import serialize


def describe(image_b64: str, context: str | None = None) -> str:
    """Decompile a base64-encoded PNG screenshot into an indented image AST.

    *context* is accepted for API compatibility but not currently used.
    Never raises — returns a minimal placeholder on any failure.
    """
    try:
        root = segment(image_b64)

        # Walk leaves and run OCR on text-type regions
        tokens_map: dict[int, list[object]] = {}

        def _walk_and_ocr(node: RegionNode) -> None:
            if node.children:
                for c in node.children:
                    _walk_and_ocr(c)
            elif node.region_type == "text" and node.image is not None:
                tokens = transcribe(node.image)
                tokens_map[id(node)] = tokens

        _walk_and_ocr(root)

        ordered = reconstruct(root, tokens_map)
        return serialize(ordered)
    except Exception as exc:
        return f"(root) [0,0,0,0]\n  (image) [0,0,0,0] [image-region bbox=[0,0,0,0]]\n  -- error: {exc}"
