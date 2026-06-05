"""Tests for ReferenceChecker workspace indexing."""

from pathlib import Path
from aura.craft.reference_checker import ReferenceChecker


class MockCapsule:
    def __init__(self, proposed_code: str, language: str = "python", path: Path | None = None):
        self.proposed_code = proposed_code
        self.language = language
        self.path = path


def test_reference_checker_skips_directories(tmp_workspace: Path):
    rc = ReferenceChecker()

    # Create valid python files in skipped dirs
    venv_dir = tmp_workspace / ".venv" / "lib" / "site-packages" / "badpkg"
    venv_dir.mkdir(parents=True)
    (venv_dir / "badfile.py").write_text("def skipped_func(): pass")

    # Create valid python files in project dirs
    aura_dir = tmp_workspace / "aura"
    aura_dir.mkdir(exist_ok=True)
    (aura_dir / "goodfile.py").write_text("def indexed_func(): pass")

    # Run _build_workspace_index
    rc._build_workspace_index(tmp_workspace)

    # Check that skipped_func is NOT indexed
    assert "lib.site-packages.badpkg.badfile" not in rc._workspace_modules
    assert ".venv.lib.site-packages.badpkg.badfile" not in rc._workspace_modules

    # Check that indexed_func IS indexed
    assert "aura.goodfile" in rc._workspace_modules
    assert "indexed_func" in rc._workspace_symbols["aura.goodfile"]


def test_reference_checker_reexports_local_imports(tmp_workspace: Path):
    rc = ReferenceChecker()
    aura_dir = tmp_workspace / "aura"
    aura_dir.mkdir(exist_ok=True)

    (aura_dir / "utils.py").write_text("def my_util(): pass")
    (aura_dir / "__init__.py").write_text("from .utils import my_util")

    rc._build_workspace_index(tmp_workspace)

    assert "aura.utils" in rc._workspace_modules
    assert "my_util" in rc._workspace_symbols["aura.utils"]
    assert "aura" in rc._workspace_modules
    assert "my_util" in rc._workspace_symbols["aura"]


def test_capsule_check_ignores_skipped_dirs(tmp_workspace: Path):
    rc = ReferenceChecker()

    # Put a function in a skipped directory
    venv_dir = tmp_workspace / ".venv" / "somepkg"
    venv_dir.mkdir(parents=True)
    (venv_dir / "foo.py").write_text("def bad_func(): pass")

    # The reference checker should not see bad_func from .venv
    capsule = MockCapsule(proposed_code="bad_func()")
    issues = rc.check(capsule, workspace_root=tmp_workspace)

    assert any(i.code == "undefined-name" and "bad_func" in i.message for i in issues)


def test_relative_imports_resolved(tmp_workspace: Path):
    rc = ReferenceChecker()
    aura_dir = tmp_workspace / "aura"
    craft_dir = aura_dir / "craft"
    conversation_dir = aura_dir / "conversation" / "tools"
    conversation_dir.mkdir(parents=True)
    craft_dir.mkdir(parents=True)

    (aura_dir / "utils.py").write_text("def my_util(): pass")
    (aura_dir / "__init__.py").write_text("from .utils import my_util")
    (craft_dir / "types.py").write_text("class ProposalCapsule: pass")
    (craft_dir / "compiler.py").write_text("class CompilerService: pass")
    (aura_dir / "paths.py").write_text("def safe_relative_to(): pass")
    (conversation_dir / "_write_mixin.py").write_text("class WriteMixin: pass")

    rc._build_workspace_index(tmp_workspace)

    # 1. from .types import ProposalCapsule in aura/craft/compiler.py
    cap1 = MockCapsule(proposed_code="from .types import ProposalCapsule\n", path=craft_dir / "compiler.py")
    issues1 = rc.check(cap1, workspace_root=tmp_workspace)
    assert not any(i.code == "broken-import" for i in issues1)

    # 2. from .utils import my_util in aura/__init__.py
    cap2 = MockCapsule(proposed_code="from .utils import my_util\n", path=aura_dir / "__init__.py")
    issues2 = rc.check(cap2, workspace_root=tmp_workspace)
    assert not any(i.code == "broken-import" for i in issues2)

    # 3. from ...paths import safe_relative_to in aura/conversation/tools/_write_mixin.py
    # Here package is ['aura', 'conversation', 'tools']. Level 3 takes us to ['aura']
    # + 'paths' -> aura.paths
    cap3 = MockCapsule(
        proposed_code="from ...paths import safe_relative_to\n", path=conversation_dir / "_write_mixin.py"
    )
    issues3 = rc.check(cap3, workspace_root=tmp_workspace)
    assert not any(i.code == "broken-import" for i in issues3)


def test_relative_imports_missing_symbol(tmp_workspace: Path):
    rc = ReferenceChecker()
    aura_dir = tmp_workspace / "aura"
    craft_dir = aura_dir / "craft"
    craft_dir.mkdir(parents=True)
    (craft_dir / "types.py").write_text("class ProposalCapsule: pass")
    rc._build_workspace_index(tmp_workspace)

    cap = MockCapsule(proposed_code="from .types import MissingSymbol\n", path=craft_dir / "compiler.py")
    issues = rc.check(cap, workspace_root=tmp_workspace)
    assert any(i.code == "broken-import" and "MissingSymbol" in i.message for i in issues)


def test_relative_imports_missing_module(tmp_workspace: Path):
    rc = ReferenceChecker()
    fake_dir = tmp_workspace / "fake_pkg"
    craft_dir = fake_dir / "craft"
    craft_dir.mkdir(parents=True)
    rc._build_workspace_index(tmp_workspace)

    cap = MockCapsule(proposed_code="from .missing_module import Something\n", path=craft_dir / "compiler.py")
    issues = rc.check(cap, workspace_root=tmp_workspace)
    assert any(i.code == "broken-import" and "missing_module" in i.message for i in issues)


def test_type_checking_imports_are_not_runtime_broken_imports(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .missing_module import Thing\n"
        "\n"
        "def build() -> str:\n"
        "    return 'ok'\n"
    )
    cap = MockCapsule(proposed_code=code, path=tmp_workspace / "pkg" / "module.py")

    issues = rc.check(cap, workspace_root=tmp_workspace)

    assert not any(i.code == "broken-import" and "missing_module" in i.message for i in issues)


def test_import_error_guarded_imports_are_not_runtime_broken_imports(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "try:\n"
        "    import optional_missing\n"
        "except ModuleNotFoundError:\n"
        "    optional_missing = None\n"
        "\n"
        "def build() -> bool:\n"
        "    return optional_missing is None\n"
    )

    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)

    assert not any(i.code == "broken-import" and "optional_missing" in i.message for i in issues)


def test_invalidate_workspace_index_clears_cache(tmp_workspace: Path):
    rc = ReferenceChecker()
    rc._build_workspace_index(tmp_workspace)
    assert rc._workspace_symbols is not None
    assert rc._workspace_modules is not None
    assert rc._workspace_root is not None

    rc.invalidate_workspace_index(tmp_workspace)
    assert rc._workspace_symbols is None
    assert rc._workspace_modules is None
    assert rc._workspace_root is None


def test_exception_handler_alias_counts_as_local_definition(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = "try:\n    risky()\nexcept ValueError as url_err:\n    print(url_err)\n"

    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)

    assert not any(issue.code == "undefined-name" and "url_err" in issue.message for issue in issues)


def test_exception_alias_defined_locally(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "import urllib.error\n"
        "import socket\n"
        "def test():\n"
        "    try:\n"
        "        pass\n"
        "    except urllib.error.URLError as exc:\n"
        "        if isinstance(exc.reason, socket.timeout):\n"
        '            return "timeout"\n'
        '    return "ok"\n'
    )
    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)
    assert not any(i.code == "undefined-name" for i in issues)


def test_module_dunder_globals_are_defined(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parent\n"
        'if __name__ == "__main__":\n'
        "    print(ROOT)\n"
    )

    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)

    assert not any(i.code == "undefined-name" and "__file__" in i.message for i in issues)
    assert not any(i.code == "undefined-name" and "__name__" in i.message for i in issues)


def test_urllib_exception_alias_is_defined(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "import urllib.error\n"
        "try:\n"
        "    raise urllib.error.URLError('x')\n"
        "except urllib.error.URLError as url_err:\n"
        "    print(url_err)\n"
    )

    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)

    assert not any(i.code == "undefined-name" and "url_err" in i.message for i in issues)


def test_multiple_exception_aliases(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "try:\n"
        "    pass\n"
        "except ValueError as url_err:\n"
        "    print(url_err)\n"
        "except KeyError as http_err:\n"
        "    print(http_err)\n"
    )
    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)
    assert not any(
        issue.code == "undefined-name" and name in issue.message for issue in issues for name in ("url_err", "http_err")
    )


def test_nested_try_except_aliases(tmp_workspace: Path):
    rc = ReferenceChecker()
    code = (
        "try:\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as inner_err:\n"
        "        print(inner_err)\n"
        "except KeyError as outer_err:\n"
        "    print(outer_err)\n"
    )
    issues = rc.check(MockCapsule(proposed_code=code), workspace_root=tmp_workspace)
    assert not any(
        issue.code == "undefined-name" and name in issue.message
        for issue in issues
        for name in ("inner_err", "outer_err")
    )


def test_builtin_open_does_not_resolve_to_unrelated_workspace_method(tmp_workspace: Path):
    rc = ReferenceChecker()
    drawer_dir = tmp_workspace / "aura" / "gui"
    drawer_dir.mkdir(parents=True)
    (drawer_dir / "terminal_drawer.py").write_text(
        "class TerminalDrawer:\n"
        "    def open(self):\n"
        "        pass\n"
    )
    target = tmp_workspace / "aura" / "updater.py"
    code = (
        "def write_payload(path, payload):\n"
        "    with open(path, \"wb\") as handle:\n"
        "        handle.write(payload)\n"
    )

    issues = rc.check(MockCapsule(proposed_code=code, path=target), workspace_root=tmp_workspace)

    assert not any(issue.code == "call-signature" and "TerminalDrawer.open" in issue.message for issue in issues)
    assert not any(issue.code == "call-signature" and "open" in issue.message for issue in issues)
