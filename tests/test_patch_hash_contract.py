"""``read_file`` -> ``patch_file`` hash-round-trip contract.

The model's normal loop is: call ``read_file``, get back ``content_hash``,
then pass that same value straight through as ``expected_file_hash`` on
``patch_file`` for the same, unchanged file. That round trip must always be
accepted — for LF files, CRLF files, and (as an intentionally-supported
compatibility path) a logical/newline-normalized hash. On a genuine mismatch,
``fs_write.py`` (the single owner of patch hash validation) must return
enough deterministic facts to tell a stale/wrong hash apart from a changed
file apart from newline-normalization relevance, without dumping file
contents or adding a second diagnostic tool.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry

_APPROVE = lambda _req: ApprovalDecision(action="approve")  # noqa: E731


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(workspace_root=root, mode="single")


# ---------------------------------------------------------------------------
# read_file's content_hash is always accepted back by patch_file
# ---------------------------------------------------------------------------


def test_read_file_content_hash_round_trips_into_patch_file_for_lf_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\ny = 2\n")
    reg = _registry(tmp_path)

    read_result = reg.execute("read_file", {"path": "a.py"}, _APPROVE)
    assert read_result.ok is True
    content_hash = read_result.payload["content_hash"]

    patch_result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 1", "new": "x = 100"}],
            "expected_file_hash": content_hash,
        },
        _APPROVE,
        False,
    )

    assert patch_result.ok is True
    assert target.read_bytes() == b"x = 100\ny = 2\n"


def test_read_file_content_hash_round_trips_into_patch_file_for_crlf_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\r\ny = 2\r\n")
    reg = _registry(tmp_path)

    read_result = reg.execute("read_file", {"path": "a.py"}, _APPROVE)
    assert read_result.ok is True
    content_hash = read_result.payload["content_hash"]
    # The content_hash is the raw-byte hash — CRLF bytes and all.
    assert content_hash == hashlib.sha256(target.read_bytes()).hexdigest()

    patch_result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 1", "new": "x = 100"}],
            "expected_file_hash": content_hash,
        },
        _APPROVE,
        False,
    )

    assert patch_result.ok is True
    assert target.read_bytes() == b"x = 100\r\ny = 2\r\n"


def test_read_file_content_hash_round_trips_into_multi_file_patch_transaction(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"x = 1\n")
    b.write_bytes(b"y = 1\r\n")
    reg = _registry(tmp_path)

    hash_a = reg.execute("read_file", {"path": "a.py"}, _APPROVE).payload["content_hash"]
    hash_b = reg.execute("read_file", {"path": "b.py"}, _APPROVE).payload["content_hash"]

    result = reg._handle_patch_file(
        {
            "files": [
                {
                    "path": "a.py",
                    "edits": [{"old": "x = 1", "new": "x = 2"}],
                    "expected_file_hash": hash_a,
                },
                {
                    "path": "b.py",
                    "edits": [{"old": "y = 1", "new": "y = 2"}],
                    "expected_file_hash": hash_b,
                },
            ],
        },
        _APPROVE,
        False,
    )

    assert result.ok is True
    assert a.read_bytes() == b"x = 2\n"
    assert b.read_bytes() == b"y = 2\r\n"


# ---------------------------------------------------------------------------
# logical/newline-normalized hash compatibility remains intentionally
# supported alongside the raw-byte hash
# ---------------------------------------------------------------------------


def test_logical_newline_normalized_hash_is_also_accepted(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\r\ny = 2\r\n")
    reg = _registry(tmp_path)

    # The hash of the LF-normalized text differs from the raw CRLF hash, but
    # is still accepted as a compatibility path.
    logical_hash = hashlib.sha256(b"x = 1\ny = 2\n").hexdigest()
    raw_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    assert logical_hash != raw_hash

    result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 1", "new": "x = 100"}],
            "expected_file_hash": logical_hash,
        },
        _APPROVE,
        False,
    )

    assert result.ok is True
    assert target.read_bytes() == b"x = 100\r\ny = 2\r\n"


# ---------------------------------------------------------------------------
# a genuine mismatch returns deterministic, self-diagnosing facts
# ---------------------------------------------------------------------------


def test_hash_mismatch_reports_deterministic_diagnostic_facts(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\n")
    reg = _registry(tmp_path)
    raw_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 1", "new": "x = 2"}],
            "expected_file_hash": "not-a-real-hash",
        },
        _APPROVE,
        False,
    )

    assert result.ok is False
    payload = result.payload
    assert payload["failure_class"] == "patch_file_hash_mismatch"
    assert payload["path"] == "a.py"
    assert payload["supplied_hash"] == "not-a-real-hash"
    assert payload["current_raw_hash"] == raw_hash
    assert payload["current_logical_hash"] == hashlib.sha256(b"x = 1\n").hexdigest()
    assert payload["raw_hash_matches_supplied"] is False
    assert payload["logical_hash_matches_supplied"] is False
    # Nothing was written and no file contents were dumped into the payload.
    assert target.read_bytes() == b"x = 1\n"
    assert "content" not in payload


def test_hash_mismatch_after_real_file_change_reflects_the_new_raw_hash(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_bytes(b"x = 1\n")
    reg = _registry(tmp_path)

    stale_hash = reg.execute("read_file", {"path": "a.py"}, _APPROVE).payload["content_hash"]
    target.write_bytes(b"x = 999\n")
    new_raw_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    result = reg._handle_patch_file(
        {
            "path": "a.py",
            "edits": [{"old": "x = 999", "new": "x = 2"}],
            "expected_file_hash": stale_hash,
        },
        _APPROVE,
        False,
    )

    assert result.ok is False
    payload = result.payload
    assert payload["failure_class"] == "patch_file_hash_mismatch"
    assert payload["supplied_hash"] == stale_hash
    # The raw file genuinely changed since the caller's read_file call — the
    # diagnostic reflects that rather than the stale value the caller sent.
    assert payload["current_raw_hash"] == new_raw_hash
    assert payload["current_raw_hash"] != stale_hash
