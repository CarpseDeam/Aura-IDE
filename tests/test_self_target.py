from __future__ import annotations

from pathlib import Path

import pytest

from aura.paths import aura_root
from aura.self_target import TargetRelation, classify_target


# ---------------------------------------------------------------------------
# TargetRelation contract
# ---------------------------------------------------------------------------


def test_is_frozen() -> None:
    tr = TargetRelation(
        target_root=Path("/a"),
        self_root=Path("/b"),
        relation="separate",
    )
    with pytest.raises(AttributeError):
        tr.relation = "same"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# classify_target — relation classification
# ---------------------------------------------------------------------------


def test_same() -> None:
    """target is aura_root() exactly → relation == 'same'."""
    tr = classify_target(aura_root())
    assert tr.relation == "same"
    assert tr.overlaps_self is True
    assert tr.is_separated is False


def test_target_under_self() -> None:
    """target is aura_root() / 'aura' → relation == 'target_under_self'."""
    tr = classify_target(aura_root() / "aura")
    assert tr.relation == "target_under_self"
    assert tr.overlaps_self is True
    assert tr.is_separated is False


def test_self_under_target() -> None:
    """target is aura_root().parent → relation == 'self_under_target'."""
    tr = classify_target(aura_root().parent)
    assert tr.relation == "self_under_target"
    assert tr.overlaps_self is True
    assert tr.is_separated is False


def test_separate(tmp_path: Path) -> None:
    """target is an unrelated directory → relation == 'separate'."""
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    tr = classify_target(unrelated)
    assert tr.relation == "separate"
    assert tr.overlaps_self is False
    assert tr.is_separated is True


def test_indeterminate_non_existent(tmp_path: Path) -> None:
    """target path that does not exist → relation == 'indeterminate'."""
    nonexistent = tmp_path / "does-not-exist"
    tr = classify_target(nonexistent)
    assert tr.relation == "indeterminate"
    assert tr.overlaps_self is True
    assert tr.is_separated is False


# ---------------------------------------------------------------------------
# is_separated — only True for "separate"
# ---------------------------------------------------------------------------


def test_is_separated_false_for_all_non_separate() -> None:
    """All relations except 'separate' must have is_separated == False."""
    for relation in ("same", "target_under_self", "self_under_target", "indeterminate"):
        tr = TargetRelation(
            target_root=Path("/tmp"),
            self_root=aura_root(),
            relation=relation,
        )
        assert tr.is_separated is False, f"is_separated should be False for {relation}"
        # overlaps_self must be True for all non-separate
        assert tr.overlaps_self is True, f"overlaps_self should be True for {relation}"


# ---------------------------------------------------------------------------
# Accepts both str and Path
# ---------------------------------------------------------------------------


def test_accepts_str_path() -> None:
    """classify_target works with both str and Path arguments."""
    tr_path = classify_target(aura_root())
    tr_str = classify_target(str(aura_root()))
    assert tr_path.relation == tr_str.relation == "same"


# ---------------------------------------------------------------------------
# Indeterminate — garbage input that cannot be resolved
# ---------------------------------------------------------------------------


def test_indeterminate_null_bytes() -> None:
    """Input with embedded null bytes resolves to 'indeterminate'."""
    tr = classify_target("/tmp\0foo")
    assert tr.relation == "indeterminate"
    assert tr.overlaps_self is True
    assert tr.is_separated is False
    # self_root should still be aura_root()
    assert tr.self_root == aura_root()
