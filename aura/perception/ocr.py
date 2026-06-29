"""Step 2 — OCR: pytesseract wrapper for region-level transcription."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

try:
    import pytesseract

    HAS_OCR = True
except ImportError:  # pragma: no cover
    HAS_OCR = False


DEFAULT_WINDOWS_TESSERACT_CMD = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
TESSERACT_CMD_ENV = "AURA_TESSERACT_CMD"


@dataclass
class Token:
    """A single recognised text token with its bounding box."""

    text: str
    bbox: tuple[int, int, int, int]  # left, top, width, height
    confidence: float


def _existing_file(path: Path) -> Path | None:
    try:
        expanded = path.expanduser()
        return expanded if expanded.is_file() else None
    except OSError:
        return None


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve(strict=False)).lower()
        except OSError:
            key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _app_root_candidates() -> list[Path]:
    """Return plausible roots for a bundled app and source checkout."""
    candidates: list[Path] = []
    executable = getattr(sys, "executable", "")
    if executable:
        try:
            candidates.append(Path(executable).resolve().parent)
        except OSError:
            pass
    if sys.argv and sys.argv[0]:
        try:
            candidates.append(Path(sys.argv[0]).resolve().parent)
        except OSError:
            pass
    candidates.append(Path(__file__).resolve().parents[2])
    return _dedupe_paths(candidates)


def _resolve_tesseract_cmd(
    *,
    app_roots: Iterable[Path] | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """Resolve a Tesseract executable without relying on PATH alone."""
    env = environ if environ is not None else os.environ

    configured = env.get(TESSERACT_CMD_ENV)
    if configured:
        cmd = _existing_file(Path(configured))
        if cmd is not None:
            return cmd

    roots = list(app_roots) if app_roots is not None else _app_root_candidates()
    for root in roots:
        cmd = _existing_file(root / "tesseract" / "tesseract.exe")
        if cmd is not None:
            return cmd

    default_cmd = _existing_file(DEFAULT_WINDOWS_TESSERACT_CMD)
    if default_cmd is not None:
        return default_cmd

    found = which("tesseract")
    if found:
        cmd = _existing_file(Path(found))
        if cmd is not None:
            return cmd

    return None


def _configure_tesseract() -> bool:
    """Configure pytesseract for the resolved executable and tessdata."""
    try:
        cmd = _resolve_tesseract_cmd()
    except Exception:
        return False
    if cmd is None:
        return False

    try:
        pytesseract.pytesseract.tesseract_cmd = str(cmd)
        tessdata_dir = cmd.parent / "tessdata"
        if tessdata_dir.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
    except Exception:
        return False
    return True


def _as_float(value: object, default: float = -1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def transcribe(region_img: Image.Image) -> list[Token]:
    """Run OCR on *region_img* and return a list of recognised tokens.

    Returns an empty list if pytesseract is not installed or if OCR fails.
    Never raises.
    """
    if not HAS_OCR:
        return []
    if not _configure_tesseract():
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
    except Exception:
        return []

    tokens: list[Token] = []
    try:
        text_values = data.get("text", [])
        n = len(text_values)
        for i in range(n):
            text = (text_values[i] or "").strip()
            conf = _as_float(data.get("conf", ["-1"] * n)[i])
            if not text or conf < 10:
                continue
            left = _as_int(data.get("left", [0] * n)[i])
            top = _as_int(data.get("top", [0] * n)[i])
            width = _as_int(data.get("width", [0] * n)[i])
            height = _as_int(data.get("height", [0] * n)[i])
            tokens.append(Token(text=text, bbox=(left, top, width, height), confidence=conf))
    except Exception:
        return []

    return tokens
