"""Smoke 2: tool registry against a tmp workspace.

Verifies:
- read_file / list_directory / glob basic correctness
- write_file approval flow + backup creation
- edit_file: error on 0 / 2+ matches; success on exactly 1
- jail: rejects '..', absolute path outside root, escape attempts
- read-only mode disables write tools entirely (and removes them from defs)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from aura.conversation.tools import ApprovalDecision, ApprovalRequest, ToolRegistry


def auto_approve(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def auto_reject(_req: ApprovalRequest) -> ApprovalDecision:
    return ApprovalDecision(action="reject")


FAILURES: list[str] = []


def expect(label: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        FAILURES.append(label)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        (root / "README.md").write_text("hello world\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        (root / "src" / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
        (root / "twice.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        (root / ".hidden").write_text("hidden\n", encoding="utf-8")
        (root / "mod.import").write_text("test import\n", encoding="utf-8")

        reg = ToolRegistry(root, read_only=False)

        print("\n-- read_file --")
        r = reg.execute("read_file", {"path": "README.md"}, auto_approve)
        expect(
            "read_file ok",
            r.ok and "hello world" in (r.payload.get("content") or ""),
            detail=str(r.payload),
        )

        print("-- list_directory --")
        r = reg.execute("list_directory", {"path": "."}, auto_approve)
        expect("list root", r.ok)
        names = set(r.payload.get("files", [])) | set(r.payload.get("directories", []))
        expect(".hidden excluded", ".hidden" not in names)
        expect("mod.import excluded", "mod.import" not in names)
        expect("README listed", "README.md" in names)
        expect("src/ listed", "src/" in names)

        print("-- glob --")
        r = reg.execute("glob", {"pattern": "**/*.py"}, auto_approve)
        expect("glob finds 3 .py files", r.ok and len(r.payload["matches"]) == 3,
               detail=str(r.payload))

        print("-- jail rejections --")
        r = reg.execute("read_file", {"path": "../escape.txt"}, auto_approve)
        expect("rejects ..", not r.ok and "not allowed" in r.payload["error"].lower(),
               detail=str(r.payload))
        r = reg.execute("read_file", {"path": "C:/Windows/System32/cmd.exe"}, auto_approve)
        expect("rejects abs outside root", not r.ok, detail=str(r.payload))

        print("-- write_file approve --")
        r = reg.execute(
            "write_file",
            {"path": "new/created.txt", "content": "fresh\n"},
            auto_approve,
        )
        expect("write_file approved", r.ok and (root / "new" / "created.txt").exists())
        expect("first write has no backup (new file)", r.ok and r.payload.get("backup") is None)

        print("-- write_file reject --")
        r = reg.execute(
            "write_file",
            {"path": "new/created.txt", "content": "should not stick\n"},
            auto_reject,
        )
        expect("write_file rejected", not r.ok)
        expect(
            "rejected write didn't change file",
            "fresh" in (root / "new" / "created.txt").read_text(),
        )

        print("-- write_file overwrite creates backup --")
        r = reg.execute(
            "write_file",
            {"path": "new/created.txt", "content": "v2\n"},
            auto_approve,
        )
        expect("write_file v2 approved", r.ok and r.payload.get("backup") is not None)
        backup_rel = r.payload["backup"]
        expect("backup file exists", (root / backup_rel).exists())
        expect(
            "backup contains old content",
            "fresh" in (root / backup_rel).read_text(),
        )

        print("-- edit_file unique match --")
        r = reg.execute(
            "edit_file",
            {"path": "src/a.py", "old_str": "return 1", "new_str": "return 42"},
            auto_approve,
        )
        expect("edit_file unique-match approved", r.ok)
        expect(
            "edit applied",
            "return 42" in (root / "src" / "a.py").read_text(),
        )

        print("-- edit_file zero matches --")
        r = reg.execute(
            "edit_file",
            {"path": "src/a.py", "old_str": "NOT THERE", "new_str": "X"},
            auto_approve,
        )
        expect("edit_file 0 matches errors", not r.ok and "not found" in r.payload["error"])

        print("-- edit_file multiple matches --")
        r = reg.execute(
            "edit_file",
            {"path": "twice.py", "old_str": "x = 1", "new_str": "x = 2"},
            auto_approve,
        )
        expect("edit_file 2+ matches errors", not r.ok and "match" in r.payload["error"])

        print("-- read-only mode --")
        reg.set_read_only(True)
        defs = reg.tool_defs()
        names = {d["function"]["name"] for d in defs}
        expect("read-only: write_file removed", "write_file" not in names)
        expect("read-only: edit_file removed", "edit_file" not in names)
        expect("read-only: read_file present", "read_file" in names)
        # Even if model tries the tool name, dispatch must refuse.
        r = reg.execute(
            "write_file",
            {"path": "x.txt", "content": "y"},
            auto_approve,
        )
        expect("read-only: write_file dispatch refused", not r.ok)

        print("\n-- summary --")
        if FAILURES:
            print(f"FAIL ({len(FAILURES)}): {FAILURES}")
            return 1
        print("All tool tests PASS")
        return 0


if __name__ == "__main__":
    sys.exit(main())
