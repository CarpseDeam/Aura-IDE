"""Public-GitHub skill import source.

Downloading is isolated behind :class:`GitHubSkillFetcher` so import tests can
inject a fake fetcher and never touch the network. Only public repositories
are supported — no authentication, no private-repo access.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from aura.skills.archive import ArchiveError, safe_extract_zip

_ROOT_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
_TREE_RE = re.compile(
    r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?"
    r"/tree/(?P<ref>[^/]+)/(?P<path>.+?)/?$"
)

_DEFAULT_REF = "HEAD"
_FETCH_TIMEOUT = 30.0


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

    The single network call lives in :meth:`_download` so a test double can
    override just that method (or replace the whole fetcher) and never
    exercise real HTTP.
    """

    def _download(self, target: GitHubTarget) -> bytes:
        url = f"https://codeload.github.com/{target.owner}/{target.repo}/zip/{target.ref}"
        response = httpx.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True)
        if response.status_code != 200:
            raise GitHubImportError(
                f"could not download {target.owner}/{target.repo}@{target.ref} "
                f"(HTTP {response.status_code}); confirm the repository is public"
            )
        return response.content

    def fetch(self, target: GitHubTarget, staging_root: Path) -> Path:
        """Download and extract *target*, returning the resolved skill directory.

        The returned path is the subdirectory named by ``target.subpath``
        (or the repository root) inside the extracted zipball — the caller
        still validates it actually contains a ``SKILL.md``.
        """
        try:
            payload = self._download(target)
        except httpx.HTTPError as exc:
            raise GitHubImportError(f"network error fetching {target.owner}/{target.repo}: {exc}") from exc

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(payload)
            archive_path = Path(tmp.name)
        try:
            extract_dir = staging_root / "github-download"
            try:
                safe_extract_zip(archive_path, extract_dir)
            except ArchiveError as exc:
                raise GitHubImportError(f"downloaded archive is invalid: {exc}") from exc
        finally:
            archive_path.unlink(missing_ok=True)

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
