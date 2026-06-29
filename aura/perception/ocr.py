"""Step 2 — OCR: pytesseract wrapper for region-level transcription."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

try:
    import pytesseract

    HAS_OCR = True
except ImportError:  # pragma: no cover
    HAS_OCR = False


@dataclass
class Token:
    """A single recognised text token with its bounding box."""

    text: str
    bbox: tuple[int, int, int, int]  # left, top, width, height
    confidence: float


def transcribe(region_img: Image.Image) -> list[Token]:
    """Run OCR on *region_img* and return a list of recognised tokens.

    Returns an empty list if pytesseract is not installed or if OCR fails.
    Never raises.
    """
    if not HAS_OCR:
        return []

    try:
        data = pytesseract.image_to_data(
            region_img,
            output_type=pytesseract.Output.DICT,
            lang="eng",
        )
    except pytesseract.TesseractError:
        # Tesseract not installed or misconfigured — return empty per spec
        return []
    except OSError:
        # File-system-level failure — return empty per spec
        return []

    tokens: list[Token] = []
    n = len(data.get("text", []))
    for i in range(n):
        text = (data.get("text", [""] * n)[i] or "").strip()
        conf_str = data.get("conf", ["-1"] * n)[i]
        try:
            conf = int(conf_str)
        except (ValueError, TypeError):
            conf = -1
        if not text or conf < 10:
            continue
        left = int(data.get("left", [0] * n)[i])
        top = int(data.get("top", [0] * n)[i])
        width = int(data.get("width", [0] * n)[i])
        height = int(data.get("height", [0] * n)[i])
        tokens.append(Token(text=text, bbox=(left, top, width, height), confidence=float(conf)))

    return tokens
