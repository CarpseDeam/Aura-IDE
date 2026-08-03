"""Obtaining the Windows MCP executable, safely.

Three ways the server command can be resolved, in strict order:

1. **A custom command.**  Whatever the user typed is used exactly.  No
   download, no checksum, no version check, no rewriting — they pointed Aura
   at a binary and Aura runs that binary.
2. **An existing managed install.**  A previously installed version under
   ``%LOCALAPPDATA%\\Aura\\tools\\windows-mcp\\<version>`` is reused as-is,
   including with no network at all.
3. **A fresh managed install** from the official ``sbroenne/mcp-windows``
   releases.

The download path treats the archive as hostile until it is proven otherwise:

* only a **stable** release counts — drafts and prereleases are skipped even
  when they are newer;
* the architecture asset is chosen from the running machine, not guessed;
* the official ``SHA256SUMS.txt`` is fetched and the ZIP is verified **before**
  a single member is read, so a corrupted or substituted archive is never even
  parsed;
* every member name is rejected if it is absolute, carries a drive letter,
  contains ``..``, or resolves outside the staging directory, and symlink
  members are rejected outright;
* extraction lands in a staging directory and the finished tree is moved into
  place in one ``os.replace``, so a half-extracted install is never visible;
* a usable existing install is never replaced silently — the caller has to ask
  for a repair;
* staging is removed on every failure path.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

_log = logging.getLogger(__name__)

#: The one official source.  Not configurable: "install a Windows input-
#: injection server from an arbitrary URL" is not a setting this product wants
#: to own.  A user who wants a different binary points the custom command at it.
GITHUB_RELEASES_URL = "https://api.github.com/repos/sbroenne/mcp-windows/releases"

#: The executable a managed install must produce.
SERVER_EXE_NAME = "Sbroenne.WindowsMcp.exe"

#: Official checksum manifest asset name, ``<sha256>␠␠<filename>`` per line.
CHECKSUM_ASSET_NAME = "SHA256SUMS.txt"

_ASSET_RE = re.compile(
    r"^windows-mcp-server-(?P<version>\d+\.\d+\.\d+)-win-(?P<arch>x64|arm64)\.zip$",
    re.IGNORECASE,
)

_DOWNLOAD_TIMEOUT = 600
_API_TIMEOUT = 30

ProgressCallback = Callable[[str], None]


class WindowsMcpInstallError(RuntimeError):
    """Managed installation could not produce a usable server executable."""


@dataclass(frozen=True)
class ManagedInstall:
    """A usable managed installation on disk."""

    version: str
    exe_path: Path


# ── locations ───────────────────────────────────────────────────────────────


def tools_root() -> Path:
    """``%LOCALAPPDATA%\\Aura\\tools\\windows-mcp``, the managed install root."""
    override = os.environ.get("AURA_WINDOWS_MCP_ROOT")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "Aura" / "tools" / "windows-mcp"


def _version_dir(version: str) -> Path:
    return tools_root() / _safe_version(version)


def _safe_version(version: str) -> str:
    """A version string reduced to something that cannot be a path trick."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(version or "").strip())
    cleaned = cleaned.strip(".")
    if not cleaned:
        raise WindowsMcpInstallError(f"Unusable release version: {version!r}")
    return cleaned


def installed_server_path() -> ManagedInstall | None:
    """Return the newest usable managed install, or ``None``.

    Pure filesystem inspection — this is what makes an offline launch work.
    """
    root = tools_root()
    try:
        entries = sorted(root.iterdir(), reverse=True)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        exe = entry / SERVER_EXE_NAME
        if exe.is_file():
            return ManagedInstall(version=entry.name, exe_path=exe)
    return None


# ── release selection ───────────────────────────────────────────────────────


def current_arch_tag() -> str:
    """``x64`` or ``arm64`` for the running machine."""
    machine = (platform.machine() or "").lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"amd64", "x86_64", "x64"}:
        return "x64"
    # A 32-bit Python on 64-bit Windows still reports the process architecture
    # here, so fall back to what the OS says the machine is.
    env_arch = (os.environ.get("PROCESSOR_ARCHITEW6432") or "").lower()
    if env_arch == "arm64":
        return "arm64"
    if env_arch in {"amd64", "x86_64"}:
        return "x64"
    raise WindowsMcpInstallError(
        f"No Windows MCP release is published for this architecture: {machine!r}"
    )


@dataclass(frozen=True)
class ReleaseAssets:
    version: str
    zip_name: str
    zip_url: str
    checksums_url: str


def select_stable_release(
    releases: list[dict], *, arch: str | None = None
) -> ReleaseAssets:
    """Pick the newest stable release that ships this machine's ZIP.

    ``draft`` and ``prerelease`` entries are skipped unconditionally.  The
    GitHub list endpoint is already newest-first, and this walks it in that
    order rather than re-sorting, so "newest stable" means what the API means.
    """
    arch_tag = arch or current_arch_tag()
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue

        zip_name = zip_url = checksums_url = ""
        version = ""
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if not name or not url:
                continue
            if name == CHECKSUM_ASSET_NAME:
                checksums_url = url
                continue
            match = _ASSET_RE.match(name)
            if match and match.group("arch").lower() == arch_tag:
                zip_name = name
                zip_url = url
                version = match.group("version")

        if zip_name and zip_url and checksums_url:
            return ReleaseAssets(
                version=version,
                zip_name=zip_name,
                zip_url=zip_url,
                checksums_url=checksums_url,
            )

    raise WindowsMcpInstallError(
        "No stable sbroenne/mcp-windows release publishes both a "
        f"win-{arch_tag} server archive and {CHECKSUM_ASSET_NAME}."
    )


def fetch_releases(timeout: int = _API_TIMEOUT) -> list[dict]:
    """Fetch the release list from GitHub."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(GITHUB_RELEASES_URL, params={"per_page": 20})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, list):
        raise WindowsMcpInstallError("GitHub returned an unexpected release list.")
    return data


# ── verification ────────────────────────────────────────────────────────────


def parse_checksums(text: str) -> dict[str, str]:
    """Parse ``<sha256>  <filename>`` lines into ``{filename: sha256}``."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        digest, name = parts
        # A binary-mode manifest writes "*name"; strip the marker.
        checksums[name.lstrip("*")] = digest.strip().lower()
    return checksums


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(archive: Path, expected_sha256: str) -> None:
    """Raise unless *archive* hashes to *expected_sha256*.

    There is no "no checksum published, carry on" branch here.  An archive
    whose hash is unknown is an archive nobody vouched for, and this one is
    about to be extracted into a directory Aura will later execute from.
    """
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        raise WindowsMcpInstallError(
            f"No published SHA256 checksum for {archive.name}; refusing to install it."
        )
    actual = sha256_of(archive)
    if actual != expected:
        raise WindowsMcpInstallError(
            f"Checksum mismatch for {archive.name}: expected {expected}, got "
            f"{actual}. The download was not the published release; nothing "
            "was extracted."
        )


_SYMLINK_MODE = 0xA000


def _reject_unsafe_member(member: zipfile.ZipInfo, destination: Path) -> Path:
    """Return the safe target path for *member*, or raise.

    Checked against the name the archive carries *and* against the resolved
    path, because those catch different attacks: the name check stops the
    obvious ``..\\..\\system32`` and ``C:\\`` entries, and the resolution check
    stops anything that only escapes once the OS has normalised it.
    """
    raw = member.filename.replace("\\", "/")
    if raw.endswith("/"):
        # Directory entries create nothing on their own but are still named.
        pure = Path(raw.rstrip("/"))
    else:
        pure = Path(raw)

    if (member.external_attr >> 16) & _SYMLINK_MODE == _SYMLINK_MODE:
        raise WindowsMcpInstallError(
            f"Release archive contains a symlink member ({member.filename!r}); "
            "refusing to extract it."
        )
    # Drive first: on Windows a drive-qualified name is *also* absolute, and
    # the more specific refusal is the more useful one to read.
    if pure.drive or re.match(r"^[A-Za-z]:", raw):
        raise WindowsMcpInstallError(
            f"Release archive contains a drive-qualified path ({member.filename!r})."
        )
    if pure.is_absolute() or raw.startswith("/"):
        raise WindowsMcpInstallError(
            f"Release archive contains an absolute path ({member.filename!r})."
        )
    if ".." in pure.parts:
        raise WindowsMcpInstallError(
            f"Release archive contains a traversal path ({member.filename!r})."
        )

    target = (destination / pure).resolve()
    root = destination.resolve()
    if target != root and root not in target.parents:
        raise WindowsMcpInstallError(
            f"Release archive member escapes the staging directory "
            f"({member.filename!r})."
        )
    return target


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract *archive* into *destination*, refusing anything unsafe.

    Whole-archive refusal, not per-member skipping: a release that contains one
    traversal entry is not a release to install the rest of.
    """
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        for member in members:
            _reject_unsafe_member(member, destination)
        for member in members:
            target = _reject_unsafe_member(member, destination)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle)


def _find_server_exe(root: Path) -> Path:
    """Locate the server executable anywhere in an extracted tree."""
    direct = root / SERVER_EXE_NAME
    if direct.is_file():
        return direct
    for candidate in sorted(root.rglob(SERVER_EXE_NAME)):
        if candidate.is_file():
            return candidate
    raise WindowsMcpInstallError(
        f"The release archive does not contain {SERVER_EXE_NAME}."
    )


# ── installation ────────────────────────────────────────────────────────────


def _force_remove(path: Path) -> None:
    """Remove a tree, clearing the read-only bits Windows leaves behind."""

    def _on_error(func, target, _exc_info):  # pragma: no cover - rare path
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            _log.warning("Could not remove %s during cleanup", target)

    shutil.rmtree(path, onerror=_on_error)


def install_release(
    *,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> ManagedInstall:
    """Install the newest stable official release, or reuse a usable one.

    ``force`` is the repair path: it is the only way an existing usable
    installation is replaced, so a routine enable never overwrites a working
    binary because a new version happened to appear.
    """

    def _say(message: str) -> None:
        _log.info("windows_mcp_install %s", message)
        if progress is not None:
            progress(message)

    existing = installed_server_path()
    if existing is not None and not force:
        _say(f"Reusing installed Windows MCP {existing.version}.")
        return existing

    _say("Checking official sbroenne/mcp-windows releases...")
    release = select_stable_release(fetch_releases())

    target_dir = _version_dir(release.version)
    target_exe = target_dir / SERVER_EXE_NAME
    if target_exe.is_file() and not force:
        _say(f"Windows MCP {release.version} is already installed.")
        return ManagedInstall(version=target_dir.name, exe_path=target_exe)

    tools_root().mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f"windows-mcp-{_safe_version(release.version)}-", dir=tools_root())
    )
    try:
        archive = staging / release.zip_name
        _say(f"Downloading {release.zip_name}...")
        with httpx.Client(follow_redirects=True, timeout=_DOWNLOAD_TIMEOUT) as client:
            with client.stream("GET", release.zip_url) as response:
                response.raise_for_status()
                with open(archive, "wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)

            _say("Fetching published checksums...")
            checksum_response = client.get(release.checksums_url)
            checksum_response.raise_for_status()
            checksums = parse_checksums(checksum_response.text)

        _say("Verifying checksum...")
        verify_download(archive, checksums.get(release.zip_name, ""))

        _say("Extracting...")
        extracted = staging / "extracted"
        safe_extract(archive, extracted)
        exe = _find_server_exe(extracted)

        # The install is the directory that *contains* the executable, so the
        # server keeps whatever runtime files ship beside it.
        payload = exe.parent
        ready = staging / "ready"
        payload.rename(ready)

        _say(f"Installing Windows MCP {release.version}...")
        if target_dir.exists():
            # Only reachable under force; the non-force paths returned above.
            _force_remove(target_dir)
        os.replace(ready, target_dir)
    except WindowsMcpInstallError:
        raise
    except Exception as exc:
        raise WindowsMcpInstallError(f"Windows MCP installation failed: {exc}") from exc
    finally:
        if staging.exists():
            _force_remove(staging)

    installed_exe = target_dir / SERVER_EXE_NAME
    if not installed_exe.is_file():
        raise WindowsMcpInstallError(
            f"Installation completed but {SERVER_EXE_NAME} is missing from {target_dir}."
        )
    _say(f"Windows MCP {release.version} installed.")
    return ManagedInstall(version=target_dir.name, exe_path=installed_exe)


def remove_installations() -> int:
    """Delete every managed install.  Returns how many were removed."""
    root = tools_root()
    if not root.is_dir():
        return 0
    removed = 0
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            _force_remove(entry)
            removed += 1
    return removed


def resolve_server_command(
    custom_command: str,
    *,
    allow_install: bool = True,
    progress: ProgressCallback | None = None,
) -> str:
    """Return the command string to launch, honouring a custom command first.

    A non-empty *custom_command* is returned verbatim.  It is not validated,
    version-checked, or compared against a managed install, and reaching it
    performs no network access and touches no managed directory at all.
    """
    custom = (custom_command or "").strip()
    if custom:
        return custom

    existing = installed_server_path()
    if existing is not None:
        return _quote_command(existing.exe_path)

    if not allow_install:
        raise WindowsMcpInstallError(
            "Windows Computer Use is not installed and installation was not requested."
        )
    return _quote_command(install_release(progress=progress).exe_path)


def _quote_command(exe_path: Path) -> str:
    """Quote a path for the registry's command parsing when it needs it."""
    text = str(exe_path)
    return f'"{text}"' if " " in text else text


__all__ = [
    "CHECKSUM_ASSET_NAME",
    "GITHUB_RELEASES_URL",
    "SERVER_EXE_NAME",
    "ManagedInstall",
    "ReleaseAssets",
    "WindowsMcpInstallError",
    "current_arch_tag",
    "fetch_releases",
    "install_release",
    "installed_server_path",
    "parse_checksums",
    "remove_installations",
    "resolve_server_command",
    "safe_extract",
    "select_stable_release",
    "sha256_of",
    "tools_root",
    "verify_download",
]
