from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aura.paths import aura_root, safe_is_relative_to


@dataclass(frozen=True)
class TargetRelation:
    """Result of classifying a target path relative to Aura's own root."""

    target_root: Path
    self_root: Path
    relation: str

    @property
    def overlaps_self(self) -> bool:
        """True for every relation except 'separate'."""
        return self.relation != "separate"

    @property
    def is_separated(self) -> bool:
        """True only when relation is 'separate'."""
        return self.relation == "separate"


def classify_target(target_root: Path | str) -> TargetRelation:
    """Classify a target path's relationship to the Aura installation root.

    Returns a TargetRelation with one of: "same", "target_under_self",
    "self_under_target", "separate", or "indeterminate".
    """
    # Step 1: resolve, with garbage-input guard
    try:
        target = Path(target_root).resolve()
    except (ValueError, TypeError, OSError):
        try:
            unresolved = Path(target_root)
        except Exception:
            unresolved = Path(".")
        return TargetRelation(
            target_root=unresolved,
            self_root=aura_root(),
            relation="indeterminate",
        )

    # Step 2: non-existent path → indeterminate
    if not target.exists():
        return TargetRelation(
            target_root=target,
            self_root=aura_root(),
            relation="indeterminate",
        )

    self_root = aura_root()

    # Step 4: same path
    if target == self_root:
        return TargetRelation(
            target_root=target,
            self_root=self_root,
            relation="same",
        )

    # Step 5: containment via safe_is_relative_to
    if safe_is_relative_to(target, self_root):
        return TargetRelation(
            target_root=target,
            self_root=self_root,
            relation="target_under_self",
        )
    if safe_is_relative_to(self_root, target):
        return TargetRelation(
            target_root=target,
            self_root=self_root,
            relation="self_under_target",
        )

    # Step 6: provably separate
    return TargetRelation(
        target_root=target,
        self_root=self_root,
        relation="separate",
    )
