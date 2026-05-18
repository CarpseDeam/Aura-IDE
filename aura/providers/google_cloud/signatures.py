import base64
from typing import Any


def encode_signature_safe(sig: Any) -> str:
    """Encode a signature value to a JSON-safe string.

    - bytes → base64 (never uses str(bytes))
    - str   → str
    - other → repr(str(other))
    """
    if isinstance(sig, bytes):
        return base64.b64encode(sig).decode("ascii")
    if isinstance(sig, str):
        return sig
    return repr(str(sig))


def decode_signature(encoded: str) -> bytes | str:
    """Decode a value that was encoded with encode_signature_safe.

    Attempts base64 decode first; returns the raw string on failure.
    """
    try:
        return base64.b64decode(encoded)
    except Exception:
        return encoded


def make_message_json_safe(msg: dict) -> dict:
    """Recursively sanitize a message dict so it contains no raw bytes.

    All bytes values are converted to base64 strings.  Structure is
    preserved.  Signature parts are never merged or concatenated.
    """
    if isinstance(msg, dict):
        return {k: make_message_json_safe(v) for k, v in msg.items()}
    if isinstance(msg, list):
        return [make_message_json_safe(item) for item in msg]
    if isinstance(msg, tuple):
        return tuple(make_message_json_safe(item) for item in msg)
    if isinstance(msg, bytes):
        return base64.b64encode(msg).decode("ascii")
    if isinstance(msg, (int, float, str, bool)) or msg is None:
        return msg
    return str(msg)
