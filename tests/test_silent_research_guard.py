from __future__ import annotations

from aura.gui.silent_research_guard import _should_block_silent_research_surface


def test_silent_research_guard_blocks_work_surfaces_but_not_main_window():
    assert not _should_block_silent_research_surface(
        is_owner=True,
        is_owner_window=False,
        is_explicitly_blocked=True,
        is_window=True,
    )
    assert not _should_block_silent_research_surface(
        is_owner=False,
        is_owner_window=True,
        is_explicitly_blocked=True,
        is_window=True,
    )
    assert _should_block_silent_research_surface(
        is_owner=False,
        is_owner_window=False,
        is_explicitly_blocked=True,
        is_window=False,
    )
    assert _should_block_silent_research_surface(
        is_owner=False,
        is_owner_window=False,
        is_explicitly_blocked=False,
        is_window=True,
    )
