"""Tests for the local release helper."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from scripts.local_release import read_version, validate_tag, verify_zip_layout


def make_zip(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "content")


def test_validate_tag_returns_clean_version() -> None:
    assert validate_tag("v1.3.1") == "1.3.1"


@pytest.mark.parametrize(
    "tag",
    ["", "1.3.1", "v1.3", "v1.3.1.0", "release-v1.3.1", "v1.3.beta"],
)
def test_validate_tag_rejects_malformed_tags(tag: str) -> None:
    with pytest.raises(SystemExit, match="Invalid tag"):
        validate_tag(tag)


def test_read_version_extracts_string_literal(tmp_path: Path) -> None:
    version_file = tmp_path / "version.py"
    version_file.write_text(
        '"""Version information."""\n__version__ = "1.3.1"\n',
        encoding="utf-8",
    )

    assert read_version(version_file) == "1.3.1"


def test_verify_zip_layout_accepts_flattened_release_zip(tmp_path: Path) -> None:
    zip_path = tmp_path / "Aura-Windows-x64.zip"
    make_zip(zip_path, ["Aura.exe", "media/AurA.ico", "runtime.dll"])

    verify_zip_layout(zip_path)


def test_verify_zip_layout_rejects_nested_exe(tmp_path: Path) -> None:
    zip_path = tmp_path / "Aura-Windows-x64.zip"
    make_zip(zip_path, ["Aura.dist/Aura.exe", "media/AurA.ico"])

    with pytest.raises(SystemExit, match="Aura.exe is not at the ZIP root"):
        verify_zip_layout(zip_path)


def test_verify_zip_layout_rejects_aura_dist_entry(tmp_path: Path) -> None:
    zip_path = tmp_path / "Aura-Windows-x64.zip"
    make_zip(zip_path, ["Aura.exe", "Aura.dist/Aura.exe", "media/AurA.ico"])

    with pytest.raises(SystemExit, match="Aura.dist/Aura.exe must not be present"):
        verify_zip_layout(zip_path)


def test_verify_zip_layout_requires_media_icon(tmp_path: Path) -> None:
    zip_path = tmp_path / "Aura-Windows-x64.zip"
    make_zip(zip_path, ["Aura.exe"])

    with pytest.raises(SystemExit, match="media/AurA.ico"):
        verify_zip_layout(zip_path)
