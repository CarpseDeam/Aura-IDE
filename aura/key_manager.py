"""Hardware-tethered API key storage using Fernet encryption.

Keys are encrypted with a machine-derived key (MAC address + username)
and stored in ~/.config/Aura/keys.json. The file is permission-locked
to 0o600 and the config directory to 0o700.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from aura.config import config_dir

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Machine-key derivation (module-private)
# ---------------------------------------------------------------------------


def _derive_machine_key() -> bytes:
    """Deterministic Fernet key derived from hardware MAC and login username.

    Returns a 44-character URL-safe base64-encoded byte string suitable for
    ``Fernet(key)``. The key is stable for the same machine+user combination
    but differs across machines, providing hardware-tethering.
    """
    try:
        node = str(uuid.getnode())
    except Exception:
        node = "0"
    try:
        user = os.getlogin()
    except Exception:
        import getpass

        user = getpass.getuser()
    raw = f"{node}:{user}"
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


# ---------------------------------------------------------------------------
# KeyManager — file-backed, encrypted, atomic writes
# ---------------------------------------------------------------------------

_FERNET_PREFIX = "gAAAAA"


class KeyManager:
    """Hardware-tethered API key storage in ``~/.config/Aura/keys.json``.

    Every key is encrypted with a machine-derived Fernet key before being
    written to disk. Legacy (plaintext) keys are transparently migrated to
    encrypted form on first read.

    Test cases:
    - get_key returns None when keys.json does not exist.
    - get_key auto-migrates a legacy plaintext value and re-writes it encrypted.
    - set_key writes a Fernet token (starts with ``gAAAAA``) to the file.
    - delete_key removes the entry and deletes the file when empty.
    """

    def __init__(self) -> None:
        """Set up paths and the Fernet cipher.

        Does **not** fail if the keys file is missing — that is handled
        lazily by the read methods.
        """
        self._path: Path = config_dir() / "keys.json"
        self._fernet: Fernet = Fernet(_derive_machine_key())

    # ---- public API -------------------------------------------------------

    def get_key(self, provider: str) -> str | None:
        """Return a decrypted API key for *provider*, or ``None``.

        If the stored value is a legacy plaintext (not a Fernet token), it
        is transparently encrypted and re-saved (best-effort) while still
        returning the plaintext.
        """
        if not self._path.exists():
            return None
        try:
            data: dict = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        ciphertext = data.get(provider)
        if not isinstance(ciphertext, str):
            return None

        # Fernet-token branch — try to decrypt.
        if ciphertext.startswith(_FERNET_PREFIX):
            try:
                return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
            except Exception:
                # InvalidToken or any other crypto failure — fall through.
                pass

        # Legacy plaintext (or decryption failed) — return as-is and migrate.
        try:
            self.set_key(provider, ciphertext)
        except Exception:
            _logger.warning(
                "Failed to migrate legacy key for %s to encrypted storage", provider
            )
        return ciphertext

    def set_key(self, provider: str, api_key: str) -> None:
        """Encrypt *api_key* and store it for *provider*.

        Writes atomically via a temporary file followed by ``os.replace``.
        Permissions are locked to ``0o600`` (file) and ``0o700`` (directory).
        """
        data: dict = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

        encrypted = self._fernet.encrypt(api_key.encode("utf-8")).decode("utf-8")
        data[provider] = encrypted

        # Atomic write via temp file.
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, self._path)
        os.chmod(self._path, 0o600)

        # Lock down the config directory (best-effort).
        try:
            os.chmod(self._path.parent, 0o700)
        except OSError:
            pass

    def delete_key(self, provider: str) -> None:
        """Remove the stored key for *provider*.

        If the file becomes empty after removal, the file itself is deleted.
        """
        data: dict = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}

        data.pop(provider, None)

        if not data:
            self._path.unlink(missing_ok=True)
        else:
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp_path, self._path)
            os.chmod(self._path, 0o600)

    def has_key(self, provider: str) -> bool:
        """Return ``True`` if a stored key exists for *provider*."""
        return self.get_key(provider) is not None


# ---------------------------------------------------------------------------
# Module-level singleton and convenience helpers
# ---------------------------------------------------------------------------

_key_manager: KeyManager | None = None


def get_key_manager() -> KeyManager:
    """Return the module-level :class:`KeyManager` singleton."""
    global _key_manager
    if _key_manager is None:
        _key_manager = KeyManager()
    return _key_manager


def get_key(provider: str) -> str | None:
    """Convenience: return the stored key for *provider*, or ``None``."""
    return get_key_manager().get_key(provider)


def set_key(provider: str, api_key: str) -> None:
    """Convenience: encrypt and store *api_key* for *provider*."""
    get_key_manager().set_key(provider, api_key)


def has_key(provider: str) -> bool:
    """Convenience: return ``True`` if a stored key exists for *provider*."""
    return get_key_manager().has_key(provider)
