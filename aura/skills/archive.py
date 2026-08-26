"""Safe ZIP extraction and skill-root discovery for imported archives.

Whole-archive refusal, not per-member skipping: an archive that contains one
unsafe entry is not one to install the rest of. Limits are centralized here
so import tests exercise the same constants production uses.
"""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

#: Hard ceiling on member count — a pathological archive cannot be used to
#: exhaust inodes or extraction time.
MAX_ARCHIVE_MEMBERS = 2000

#: Hard ceiling on total uncompressed bytes — guards against a zip bomb.
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 50 * 1024 * 1024

#: Hard ceiling on any single member's uncompressed size.
MAX_ARCHIVE_MEMBER_BYTES = 20 * 1024 * 1024

#: Hard ceiling on a *compressed* archive fetched over the network, applied
#: while it streams. Every other limit here can only be checked once a
#: complete ZIP exists on disk, which is too late to stop an endless
#: response from filling memory or the disk. Lives beside the others so
#: there is one place to read what an import is allowed to consume.
MAX_COMPRESSED_DOWNLOAD_BYTES = 25 * 1024 * 1024

_SYMLINK_MODE = 0xA000


class ArchiveError(Exception):
    """A ZIP archive failed a safety or shape check."""


def _reject_unsafe_member(member: zipfile.ZipInfo, destination: Path) -> Path:
    """Return the safe extraction target for *member*, or raise ArchiveError."""
    raw = member.filename.replace("\\", "/")
    pure = Path(raw.rstrip("/")) if raw.endswith("/") else Path(raw)

    if (member.external_attr >> 16) & _SYMLINK_MODE == _SYMLINK_MODE:
        raise ArchiveError(f"archive contains a symlink member ({member.filename!r})")
    if pure.drive or re.match(r"^[A-Za-z]:", raw):
        raise ArchiveError(f"archive contains a drive-qualified path ({member.filename!r})")
    if pure.is_absolute() or raw.startswith("/"):
        raise ArchiveError(f"archive contains an absolute path ({member.filename!r})")
    if not raw or raw in (".", "./"):
        raise ArchiveError("archive contains an empty member name")
    if ".." in pure.parts:
        raise ArchiveError(f"archive contains a traversal path ({member.filename!r})")

    target = (destination / pure).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise ArchiveError(f"archive member escapes the staging directory ({member.filename!r})")
    return target


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    """Extract *archive_path* into *destination*, refusing anything unsafe.

    Rejects absolute paths, ``..`` traversal, symlink members, and archives
    that are malformed, oversized, or carry too many entries. Every member is
    validated before anything is written, so a rejected archive leaves
    *destination* untouched.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as zf:
            members = zf.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ArchiveError(
                    f"archive has too many entries ({len(members)} > {MAX_ARCHIVE_MEMBERS})"
                )
            total_bytes = 0
            targets: list[tuple[zipfile.ZipInfo, Path]] = []
            for member in members:
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ArchiveError(
                        f"archive member {member.filename!r} is too large "
                        f"({member.file_size} > {MAX_ARCHIVE_MEMBER_BYTES} bytes)"
                    )
                total_bytes += member.file_size
                if total_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ArchiveError(
                        f"archive is too large uncompressed (> {MAX_ARCHIVE_UNCOMPRESSED_BYTES} bytes)"
                    )
                targets.append((member, _reject_unsafe_member(member, destination)))

            for member, target in targets:
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, open(target, "wb") as handle:
                    shutil.copyfileobj(source, handle)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a valid ZIP archive: {exc}") from exc


def find_skill_root(extracted_dir: Path) -> Path:
    """Locate the one unambiguous skill directory inside an extracted tree.

    Searches the whole tree for ``SKILL.md`` files. Exactly one match is
    required — zero means the archive does not contain a skill, more than
    one is an ambiguous archive and neither is installed automatically.
    """
    matches = sorted(p for p in extracted_dir.rglob("SKILL.md") if p.is_file())
    if not matches:
        raise ArchiveError("no SKILL.md found in the archive")
    if len(matches) > 1:
        names = ", ".join(str(m.parent.relative_to(extracted_dir)) for m in matches)
        raise ArchiveError(f"ambiguous archive: multiple SKILL.md directories found ({names})")
    return matches[0].parent
