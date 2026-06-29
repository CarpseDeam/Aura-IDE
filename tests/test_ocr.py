"""Tests for OCR runtime resolution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import aura.perception.ocr as ocr


def _write_tesseract(root: Path) -> Path:
    exe = root / "tesseract.exe"
    tessdata = root / "tessdata"
    tessdata.mkdir(parents=True)
    exe.write_text("exe", encoding="utf-8")
    (tessdata / "eng.traineddata").write_text("eng", encoding="utf-8")
    return exe


class FakePytesseract:
    class Output:
        DICT = "dict"

    class TesseractError(Exception):
        pass

    def __init__(self, data: dict[str, list[object]] | None = None, error: Exception | None = None) -> None:
        self.pytesseract = SimpleNamespace(tesseract_cmd="")
        self.data = data or {}
        self.error = error
        self.calls = 0

    def image_to_data(self, image: Image.Image, *, output_type: str, lang: str) -> dict[str, list[object]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert output_type == "dict"
        assert lang == "eng"
        return self.data


def test_resolve_tesseract_prefers_env_command(tmp_path: Path) -> None:
    env_exe = _write_tesseract(tmp_path / "env")
    bundled_exe = _write_tesseract(tmp_path / "app" / "tesseract")

    resolved = ocr._resolve_tesseract_cmd(
        app_roots=[tmp_path / "app"],
        environ={"AURA_TESSERACT_CMD": str(env_exe)},
        which=lambda _name: str(bundled_exe),
    )

    assert resolved == env_exe


def test_resolve_tesseract_uses_bundled_before_default_and_path(
    monkeypatch, tmp_path: Path
) -> None:
    bundled_exe = _write_tesseract(tmp_path / "app" / "tesseract")
    default_exe = _write_tesseract(tmp_path / "default")
    path_exe = _write_tesseract(tmp_path / "path")
    monkeypatch.setattr(ocr, "DEFAULT_WINDOWS_TESSERACT_CMD", default_exe)

    resolved = ocr._resolve_tesseract_cmd(
        app_roots=[tmp_path / "app"],
        environ={},
        which=lambda _name: str(path_exe),
    )

    assert resolved == bundled_exe


def test_resolve_tesseract_uses_default_before_path(monkeypatch, tmp_path: Path) -> None:
    default_exe = _write_tesseract(tmp_path / "default")
    path_exe = _write_tesseract(tmp_path / "path")
    monkeypatch.setattr(ocr, "DEFAULT_WINDOWS_TESSERACT_CMD", default_exe)

    resolved = ocr._resolve_tesseract_cmd(
        app_roots=[tmp_path / "missing-app"],
        environ={},
        which=lambda _name: str(path_exe),
    )

    assert resolved == default_exe


def test_transcribe_configures_tesseract_and_tessdata(monkeypatch, tmp_path: Path) -> None:
    exe = _write_tesseract(tmp_path / "runtime")
    fake = FakePytesseract(
        {
            "text": ["", "Hello"],
            "conf": ["-1", "96.5"],
            "left": ["0", "11"],
            "top": ["0", "22"],
            "width": ["0", "33"],
            "height": ["0", "44"],
        }
    )
    monkeypatch.setattr(ocr, "HAS_OCR", True)
    monkeypatch.setattr(ocr, "pytesseract", fake, raising=False)
    monkeypatch.setenv("AURA_TESSERACT_CMD", str(exe))
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)

    tokens = ocr.transcribe(Image.new("RGB", (20, 20), "white"))

    assert fake.pytesseract.tesseract_cmd == str(exe)
    assert tokens == [ocr.Token(text="Hello", bbox=(11, 22, 33, 44), confidence=96.5)]
    assert fake.calls == 1
    assert Path(ocr.os.environ["TESSDATA_PREFIX"]) == exe.parent / "tessdata"


def test_transcribe_returns_empty_when_tesseract_unresolved(monkeypatch) -> None:
    fake = FakePytesseract({"text": ["Hello"]})
    monkeypatch.setattr(ocr, "HAS_OCR", True)
    monkeypatch.setattr(ocr, "pytesseract", fake, raising=False)
    monkeypatch.setattr(ocr, "_resolve_tesseract_cmd", lambda: None)

    assert ocr.transcribe(Image.new("RGB", (20, 20), "white")) == []
    assert fake.calls == 0


def test_transcribe_returns_empty_on_ocr_failure(monkeypatch, tmp_path: Path) -> None:
    exe = _write_tesseract(tmp_path / "runtime")
    fake = FakePytesseract(error=FakePytesseract.TesseractError("failed"))
    monkeypatch.setattr(ocr, "HAS_OCR", True)
    monkeypatch.setattr(ocr, "pytesseract", fake, raising=False)
    monkeypatch.setenv("AURA_TESSERACT_CMD", str(exe))

    assert ocr.transcribe(Image.new("RGB", (20, 20), "white")) == []
