"""Path and directory management for Aura."""
import os
import stat
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "Aura"
APP_AUTHOR = "Aura"


def config_dir() -> Path:
    """Return the platform-specific user configuration directory for Aura."""
    override = os.environ.get("AURA_CONFIG_DIR")
    p = Path(override).expanduser() if override else Path(user_config_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def data_dir() -> Path:
    """Return the platform-specific user data directory for Aura."""
    override = os.environ.get("AURA_DATA_DIR")
    p = Path(override).expanduser() if override else Path(user_data_dir(APP_NAME, APP_AUTHOR))
    p.mkdir(parents=True, exist_ok=True)
    return p


def safe_relative_to(path: Path | str, root: Path | str) -> Path:
    """Safely compute relative path, handling Windows case-insensitivity."""
    import os
    p_path: Path = Path(path)
    p_root: Path = Path(root)
    try:
        return p_path.resolve().relative_to(p_root.resolve())
    except ValueError:
        try:
            return Path(os.path.relpath(p_path, p_root))
        except Exception:
            return p_path


def safe_is_relative_to(path: Path | str, root: Path | str) -> bool:
    """Safely check if a path is relative to (under) a root directory."""
    import os
    p_path: Path = Path(path)
    p_root: Path = Path(root)
    try:
        p_resolved: Path = p_path.resolve()
        r_resolved: Path = p_root.resolve()
        rel: str = os.path.relpath(p_resolved, r_resolved)
        return not (rel.startswith("..") or os.path.isabs(rel))
    except Exception:
        try:
            return p_path.is_relative_to(p_root)
        except Exception:
            return False


#: Windows reparse tags that make a path point somewhere else on disk. Other
#: reparse points (OneDrive placeholders, deduplicated files) are ordinary
#: files whose content is materialised on demand and must not be mistaken for
#: links. The literal fallbacks keep the policy identical on a platform whose
#: ``stat`` module does not publish the Windows tags, so a cross-platform test
#: of this decision exercises the real values.
_REDIRECTING_REPARSE_TAGS = frozenset(
    {
        getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
        getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
        getattr(stat, "IO_REPARSE_TAG_APPEXECLINK", 0x8000001B),
    }
)

_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link_like(path: Path | str) -> bool:
    """True when *path* is a symlink, a Windows junction, or another redirecting reparse point.

    The one canonical link detector for Aura's containment checks — callers
    that need to refuse a path that could point outside its own tree ask this
    and nothing else. It never follows the final component and never raises:
    a path that cannot be stat'ed (missing, permission denied, malformed) is
    simply not a link.

    Deliberately written against ``os.lstat`` rather than ``Path.is_junction``
    or ``Path.exists(follow_symlinks=...)`` — both are Python 3.12+, while
    Aura supports 3.10 — so the same decision is reached on 3.10 through 3.13.
    """
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    if not attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    tag = getattr(st, "st_reparse_tag", None)
    if tag is None:
        # Pre-3.8 shape, or a stat provider that omits the tag: the reparse
        # bit alone is all the evidence there is, so fail closed.
        return True
    return tag in _REDIRECTING_REPARSE_TAGS


def first_link_like_component(root: Path | str, relative_parts: tuple[str, ...]) -> Path | None:
    """Walk *relative_parts* under *root*, returning the first link-like hop.

    *root* itself is checked first, before any resolution, so evidence that
    the root is a junction is never resolved away. Returns ``None`` when the
    whole chain is made of real directories and files.
    """
    walked = Path(root)
    if is_link_like(walked):
        return walked
    for part in relative_parts:
        walked = walked / part
        if is_link_like(walked):
            return walked
    return None


def aura_root() -> Path:
    """Return the Aura source repository root directory."""
    return Path(__file__).resolve().parent.parent

