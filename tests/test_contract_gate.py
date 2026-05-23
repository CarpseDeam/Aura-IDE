"""Tests for aura/craft/contract_gate.py — ContractGate dataclass field verification."""
from __future__ import annotations

import ast
from pathlib import Path

from aura.craft.contract_gate import ContractGate
from aura.craft.types import (
    ProposalCapsule, ExplicitSpecContract, CraftIssue, CraftIssueSeverity,
    OwnershipContext, ChangeIntent,
)


def _make_capsule(code: str, contract: ExplicitSpecContract) -> ProposalCapsule:
    tree = ast.parse(code)
    return ProposalCapsule(
        path=Path("test_module.py"),
        language="python",
        tool_name="worker",
        original_code="",
        proposed_code=code,
        changed_line_ranges=[],
        ast_tree=tree,
        contract=contract,
        ownership_context=OwnershipContext.AURA,
    )


def test_dataclass_fields_passing():
    """All expected fields present → no issues."""
    code = """
from dataclasses import dataclass

@dataclass
class MyModel:
    id: int
    name: str
    active: bool = True
"""
    contract = ExplicitSpecContract(
        expected_dataclass_fields={"MyModel": ["id", "name", "active"]},
    )
    capsule = _make_capsule(code, contract)
    gate = ContractGate()
    issues = gate.verify(capsule)
    # No CONTRACT_MISSING_DATACLASS_FIELD issues
    assert not any(i.code == "CONTRACT_MISSING_DATACLASS_FIELD" for i in issues)


def test_dataclass_fields_missing():
    """Missing fields → CONTRACT_MISSING_DATACLASS_FIELD issues."""
    code = """
from dataclasses import dataclass

@dataclass
class MyModel:
    id: int
"""
    contract = ExplicitSpecContract(
        expected_dataclass_fields={"MyModel": ["id", "name", "active"]},
    )
    capsule = _make_capsule(code, contract)
    gate = ContractGate()
    issues = gate.verify(capsule)
    dataclass_issues = [i for i in issues if i.code == "CONTRACT_MISSING_DATACLASS_FIELD"]
    assert len(dataclass_issues) == 2
    missing_names = {i.message.split("'")[1] for i in dataclass_issues}
    assert missing_names == {"MyModel"}
    missing_fields = set()
    for i in dataclass_issues:
        # Extract field name from message: "Dataclass 'MyModel' is missing expected field 'name'."
        field = i.message.split("'")[3]
        missing_fields.add(field)
    assert missing_fields == {"name", "active"}


def test_dataclass_fields_non_dataclass_class():
    """Non-@dataclass class should not be checked even if name matches."""
    code = """
class MyModel:
    id: int
"""
    contract = ExplicitSpecContract(
        expected_dataclass_fields={"MyModel": ["id", "name"]},
    )
    capsule = _make_capsule(code, contract)
    gate = ContractGate()
    issues = gate.verify(capsule)
    assert not any(i.code == "CONTRACT_MISSING_DATACLASS_FIELD" for i in issues)


def test_dataclass_fields_empty_contract():
    """Empty contract → no verification issues."""
    code = """
from dataclasses import dataclass

@dataclass
class X:
    a: int
"""
    contract = ExplicitSpecContract()
    capsule = _make_capsule(code, contract)
    gate = ContractGate()
    issues = gate.verify(capsule)
    assert not any(i.code == "CONTRACT_MISSING_DATACLASS_FIELD" for i in issues)


def test_dataclass_fields_no_contract():
    """No contract → no verification issues."""
    code = "x = 1"
    capsule = ProposalCapsule(
        path=Path("x.py"),
        language="python",
        tool_name="worker",
        original_code="",
        proposed_code=code,
        changed_line_ranges=[],
        ast_tree=ast.parse(code),
        contract=None,
    )
    gate = ContractGate()
    issues = gate.verify(capsule)
    assert not issues


def test_dataclass_fields_multiple_classes():
    """Multiple dataclasses checked independently."""
    code = """
from dataclasses import dataclass

@dataclass
class A:
    x: int
    y: str

@dataclass
class B:
    a: float
    b: bool
"""
    contract = ExplicitSpecContract(
        expected_dataclass_fields={
            "A": ["x", "y"],
            "B": ["a", "b", "c"],
        },
    )
    capsule = _make_capsule(code, contract)
    gate = ContractGate()
    issues = gate.verify(capsule)
    dataclass_issues = [i for i in issues if i.code == "CONTRACT_MISSING_DATACLASS_FIELD"]
    assert len(dataclass_issues) == 1
    assert "'c'" in dataclass_issues[0].message
    assert "'B'" in dataclass_issues[0].message
