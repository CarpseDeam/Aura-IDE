"""Contract checking for UI-tree snapshot artifacts."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContractFinding:
    assertion: dict
    status: str
    detail: str


@dataclass(frozen=True)
class ContractReport:
    findings: list[ContractFinding]
    passed: int
    failed: int
    inconclusive: int

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.inconclusive == 0


_CONSTRAINT_FIELDS = frozenset({"role", "name", "object_name"})

_VALID_TYPES = frozenset({"node_exists", "node_absent"})


def _matches(assertion: dict, node: dict) -> bool:
    """Return True when every present constraint matches the node."""
    for field in _CONSTRAINT_FIELDS:
        if field in assertion:
            if node.get(field) != assertion[field]:
                return False
    return True


def _count_matching(root: dict | None, assertion: dict) -> int:
    """Recursively walk the tree rooted at *root* and count matching nodes."""
    if root is None:
        return 0
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if _matches(assertion, node):
            count += 1
        stack.extend(node.get("children", []))
    return count


def check_contract(artifact: dict, contract: dict) -> ContractReport:
    """Evaluate *contract* assertions against a UI-tree *artifact*.

    Returns a frozen ContractReport with per-assertion findings.
    """
    truncated: bool = artifact.get("truncated", False)
    root: dict | None = artifact.get("root")

    assertions: list[dict] = contract.get("assertions", [])
    findings: list[ContractFinding] = []

    for assertion in assertions:
        atype = assertion.get("type")
        if atype not in _VALID_TYPES:
            findings.append(
                ContractFinding(
                    assertion=assertion,
                    status="fail",
                    detail=f"unrecognized assertion type: {atype!r}",
                )
            )
            continue

        # Check whether any constraint field is present at all.
        has_constraint = any(f in assertion for f in _CONSTRAINT_FIELDS)
        if not has_constraint:
            findings.append(
                ContractFinding(
                    assertion=assertion,
                    status="fail",
                    detail="assertion has no constraint fields — cannot match",
                )
            )
            continue

        n = _count_matching(root, assertion)

        if atype == "node_exists":
            if n > 0:
                findings.append(
                    ContractFinding(
                        assertion=assertion,
                        status="pass",
                        detail=f"matched {n} node(s)",
                    )
                )
            elif truncated:
                findings.append(
                    ContractFinding(
                        assertion=assertion,
                        status="inconclusive",
                        detail="no matching node found, but tree is truncated"
                        " — node may exist in dropped subtree",
                    )
                )
            else:
                findings.append(
                    ContractFinding(
                        assertion=assertion,
                        status="fail",
                        detail="no matching node found",
                    )
                )

        else:  # node_absent
            if n == 0:
                if truncated:
                    findings.append(
                        ContractFinding(
                            assertion=assertion,
                            status="inconclusive",
                            detail="node not found, but tree is truncated"
                            " — cannot confirm absence",
                        )
                    )
                else:
                    findings.append(
                        ContractFinding(
                            assertion=assertion,
                            status="pass",
                            detail="node confirmed absent",
                        )
                    )
            else:
                findings.append(
                    ContractFinding(
                        assertion=assertion,
                        status="fail",
                        detail=f"node found but expected absent"
                        f" — matched {n} node(s)",
                    )
                )

    passed = sum(1 for f in findings if f.status == "pass")
    failed = sum(1 for f in findings if f.status == "fail")
    inconclusive = sum(1 for f in findings if f.status == "inconclusive")

    return ContractReport(
        findings=findings,
        passed=passed,
        failed=failed,
        inconclusive=inconclusive,
    )
