"""Public-GitHub skill import source.

Downloading is isolated behind :class:`GitHubSkillFetcher` so import tests can
inject a fake fetcher — or a mock transport, to exercise the real streaming
and limit logic — and never touch the network. Only public repositories are
supported: no authentication, no private-repo access.

The download is streamed and counted rather than buffered. A remote server
decides how many bytes it sends and what it claims in ``Content-Length``, so
neither the archive's own limits (which apply only once a complete ZIP
exists) nor the declared size can be the only defence.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from aura.skills.archive import MAX_COMPRESSED_DOWNLOAD_BYTES, ArchiveError, safe_extract_zip

_ROOT_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_TREE_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"/tree/(?P<ref>[^/]+)/(?P<path>.+?)/?$"
)

_DEFAULT_REF = "HEAD"
_FETCH_TIMEOUT = 30.0
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_ARCHIVE_FILENAME = "github-download.zip"
_EXTRACT_DIRNAME = "github-download"


class GitHubImportError(Exception):
    """A GitHub URL could not be parsed, downloaded, or resolved to a skill."""


@dataclass(frozen=True)
class GitHubTarget:
    owner: str
    repo: str
    ref: str
    subpath: str  # "" for the repository root


def parse_github_url(url: str) -> GitHubTarget:
    """Parse a repository-root or ``tree/<ref>/<path>`` GitHub URL.

    Raises :class:`GitHubImportError` for anything else — private-repo URLs,
    blob URLs, non-GitHub hosts, and malformed input all fail the same way.
    """
    text = str(url or "").strip()
    match = _TREE_RE.match(text)
    if match:
        return GitHubTarget(
            owner=match.group("owner"),
            repo=match.group("repo"),
            ref=match.group("ref"),
            subpath=match.group("path"),
        )
    match = _ROOT_RE.match(text)
    if match:
        return GitHubTarget(owner=match.group("owner"), repo=match.group("repo"), ref=_DEFAULT_REF, subpath="")
    raise GitHubImportError(
        f"'{url}' is not a supported GitHub URL — expected a repository root "
        "or a 'tree/<ref>/<path>' directory URL"
    )


class GitHubSkillFetcher:
    """Downloads one public repository's zipball and extracts it to staging.

    The single network call lives in :meth:`_download_to`. Tests either
    replace the whole fetcher or pass an ``httpx`` ``transport`` double,
    which keeps the real streaming, counting, and cleanup code under test
    without any network access.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=_FETCH_TIMEOUT, follow_redirects=True)

    def _download_to(self, target: GitHubTarget, destination: Path) -> int:
        """Stream the zipball to *destination*, returning the byte count written.

        A ``Content-Length`` over the limit is refused before a single body
        byte is read; the stream is then counted independently, because that
        header is the remote server's claim and not a fact.
        """
        url = f"https://codeload.github.com/{target.owner}/{target.repo}/zip/{target.ref}"
        written = 0
        with self._client() as client, client.stream("GET", url) as response:
            if response.status_code != 200:
                raise GitHubImportError(
                    f"could not download {target.owner}/{target.repo}@{target.ref} "
                    f"(HTTP {response.status_code}); confirm the repository is public"
                )
            declared = _declared_length(response.headers.get("content-length"))
            if declared is not None and declared > MAX_COMPRESSED_DOWNLOAD_BYTES:
                raise GitHubImportError(
                    f"download is too large ({declared} > {MAX_COMPRESSED_DOWNLOAD_BYTES} bytes)"
                )
            with open(destination, "wb") as handle:
                for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    written += len(chunk)
                    if written > MAX_COMPRESSED_DOWNLOAD_BYTES:
                        raise GitHubImportError(
                            f"download exceeded the {MAX_COMPRESSED_DOWNLOAD_BYTES} byte limit"
                        )
                    handle.write(chunk)
        return written

    def fetch(self, target: GitHubTarget, staging_root: Path) -> Path:
        """Download and extract *target*, returning the resolved skill directory.

        The returned path is the subdirectory named by ``target.subpath``
        (or the repository root) inside the extracted zipball — the caller
        still validates it actually contains a ``SKILL.md``. Every failure
        path removes the partial download and the partial extraction.
        """
        staging_root.mkdir(parents=True, exist_ok=True)
        archive_path = staging_root / _ARCHIVE_FILENAME
        extract_dir = staging_root / _EXTRACT_DIRNAME
        try:
            try:
                self._download_to(target, archive_path)
            except httpx.HTTPError as exc:
                raise GitHubImportError(f"network error fetching {target.owner}/{target.repo}: {exc}") from exc

            try:
                safe_extract_zip(archive_path, extract_dir)
            except ArchiveError as exc:
                raise GitHubImportError(f"downloaded archive is invalid: {exc}") from exc

            # GitHub zipballs wrap everything in one '<repo>-<ref>/' directory.
            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if len(roots) != 1:
                raise GitHubImportError("downloaded archive did not contain the expected single repository folder")
            repo_root = roots[0]

            target_dir = (repo_root / target.subpath) if target.subpath else repo_root
            try:
                resolved = target_dir.resolve()
                if repo_root.resolve() != resolved and repo_root.resolve() not in resolved.parents:
                    raise GitHubImportError("resolved path escapes the downloaded repository")
            except OSError as exc:
                raise GitHubImportError(f"could not resolve target directory: {exc}") from exc
            if not resolved.is_dir():
                raise GitHubImportError(f"'{target.subpath}' was not found in {target.owner}/{target.repo}@{target.ref}")
            return resolved
        except BaseException:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise
        finally:
            archive_path.unlink(missing_ok=True)


def _declared_length(raw: str | None) -> int | None:
    """Parse a ``Content-Length`` header, or None when absent or malformed."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
