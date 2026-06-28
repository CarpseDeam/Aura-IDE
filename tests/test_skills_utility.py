"""Tests for aura/skills/utility.py — derive-on-read utility meter."""
import tempfile
from pathlib import Path

import pytest

from aura.skills.utility import SourceUtility, _compute_utility_from_rows, derive_source_utility

# --- _compute_utility_from_rows ---

def test_both_arms_must_reach_min_arm():
    """Source with loaded_n >= min_arm but not_loaded_n < min_arm → insufficient."""
    rows = [
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '[]'},
    ]
    r = _compute_utility_from_rows(rows, min_arm=3)
    u = r.get("s1")
    assert u is not None
    assert u.loaded_n == 3, f"loaded_n={u.loaded_n}"
    assert u.not_loaded_n == 1, f"not_loaded_n={u.not_loaded_n}"
    assert u.status == "insufficient", f"status={u.status}"
    assert u.lift is None


def test_both_arms_at_min_arm_yields_measured():
    """Source with both arms >= min_arm → measured."""
    rows = [
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '[]'},
        {"status": "harness_error", "task_kind": "x", "included_source_ids": '[]'},
        {"status": "completed", "task_kind": "x", "included_source_ids": '[]'},
    ]
    r = _compute_utility_from_rows(rows, min_arm=3)
    u = r.get("s1")
    assert u is not None
    assert u.loaded_n == 3, f"loaded_n={u.loaded_n}"
    assert u.not_loaded_n == 3, f"not_loaded_n={u.not_loaded_n}"
    assert u.status == "measured", f"status={u.status}"
    assert u.lift is not None


def test_source_reports_terrain_where_it_loads_not_largest_band():
    rows = [
        {"status": "completed", "task_kind": "alpha", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "alpha", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "alpha", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "alpha", "included_source_ids": "[]"},
        {"status": "completed", "task_kind": "alpha", "included_source_ids": "[]"},
        {"status": "harness_error", "task_kind": "alpha", "included_source_ids": "[]"},
        {"status": "harness_error", "task_kind": "alpha", "included_source_ids": "[]"},
        *[
            {"status": "completed", "task_kind": "beta", "included_source_ids": "[]"}
            for _ in range(10)
        ],
    ]
    r = _compute_utility_from_rows(rows, min_arm=3)
    u = r.get("s1")
    assert u is not None
    assert u.task_kind == "alpha"
    assert u.status == "measured"
    assert u.loaded_n == 3
    assert u.not_loaded_n == 4
    assert u.lift == 0.5


def test_positive_lift():
    """Source that loads on successful dispatches → positive lift."""
    rows = [
        {"status": "completed", "task_kind": "t", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '["s1"]'},
        {"status": "harness_error", "task_kind": "t", "included_source_ids": '[]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '[]'},
    ]
    r = _compute_utility_from_rows(rows, min_arm=2)
    u = r.get("s1")
    assert u is not None and u.status == "measured"
    assert u.loaded_n == 3 and u.not_loaded_n == 2
    # loaded: 3/3 = 1.0, not_loaded: 1/2 = 0.5, lift = 0.5
    assert abs(u.lift - 0.5) < 0.001


def test_negative_lift():
    """Source that loads on failing dispatches → negative lift."""
    rows = [
        {"status": "harness_error", "task_kind": "t", "included_source_ids": '["s1"]'},
        {"status": "harness_error", "task_kind": "t", "included_source_ids": '["s1"]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '[]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '[]'},
        {"status": "completed", "task_kind": "t", "included_source_ids": '[]'},
    ]
    r = _compute_utility_from_rows(rows, min_arm=2)
    u = r.get("s1")
    assert u is not None and u.status == "measured"
    assert u.loaded_n == 2 and u.not_loaded_n == 3
    # loaded: 0/2 = 0.0, not_loaded: 3/3 = 1.0, lift = -1.0
    assert abs(u.lift - (-1.0)) < 0.001


def test_no_db_file():
    """derive_source_utility returns {} when no DB file exists."""
    with tempfile.TemporaryDirectory() as d:
        r = derive_source_utility(Path(d))
        assert r == {}


def test_empty_rows():
    """_compute_utility_from_rows returns {} for empty input."""
    assert _compute_utility_from_rows([]) == {}


def test_no_source_ids():
    """Rows with empty included_source_ids produce no sources."""
    rows = [{"status": "completed", "task_kind": "x", "included_source_ids": "[]"}]
    assert _compute_utility_from_rows(rows) == {}


def test_malformed_json_ids():
    """Malformed JSON in included_source_ids is treated as empty."""
    rows = [{"status": "completed", "task_kind": "x", "included_source_ids": "{bad json}"}]
    assert _compute_utility_from_rows(rows) == {}


def test_source_utility_frozen():
    """SourceUtility is truly frozen."""
    u = SourceUtility(source_id="s1", task_kind="x", loaded_n=5, not_loaded_n=5, lift=0.1, status="measured")
    import dataclasses
    assert dataclasses.is_dataclass(u)
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.loaded_n = 10  # frozen=True prevents mutation


def test_display_format():
    """Verify utility formatting matches what runtime.py display expects."""
    from aura.skills.utility import SourceUtility
    # measured positive
    u1 = SourceUtility(source_id="a", task_kind="x", loaded_n=10, not_loaded_n=20, lift=0.15, status="measured")
    sign = "+" if u1.lift >= 0 else ""
    assert f"{sign}{u1.lift:.1%}" == "+15.0%"
    # measured negative
    u2 = SourceUtility(source_id="b", task_kind="x", loaded_n=8, not_loaded_n=12, lift=-0.08, status="measured")
    sign = "+" if u2.lift >= 0 else ""
    assert f"{sign}{u2.lift:.1%}" == "-8.0%"
    # insufficient
    u3 = SourceUtility(source_id="c", task_kind="x", loaded_n=2, not_loaded_n=5, lift=None, status="insufficient")
    assert u3.lift is None
    assert u3.status == "insufficient"
