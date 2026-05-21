"""Tests for ReferenceChecker workspace indexing."""

import pytest
from pathlib import Path
from aura.craft.reference_checker import ReferenceChecker

class MockCapsule:
    def __init__(self, proposed_code: str, language: str = "python"):
        self.proposed_code = proposed_code
        self.language = language

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
