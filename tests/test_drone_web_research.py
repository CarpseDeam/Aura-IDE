"""Tests for the bundled Web Research Drone (Phase 4)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from aura.drones.store import DroneStore
from aura.drones.sync_runner import run_read_only_drone_sync


def _load_web_research_main():
    path = (
        Path(__file__).resolve().parent.parent
        / "aura"
        / "drones"
        / "bundled"
        / "web-research"
        / "main.py"
    )
    spec = importlib.util.spec_from_file_location("web_research_main_for_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_aura_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aura.drones.store.aura_root", lambda: tmp_path / "aura_root")
    monkeypatch.setenv("_AURA_MOCK_WEB_RESEARCH", "1")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def web_research_folder(tmp_path: Path) -> Path:
    bundled = (
        Path(__file__).resolve().parent.parent
        / "aura"
        / "drones"
        / "bundled"
        / "web-research"
    )
    assert bundled.is_dir(), f"Source drone folder not found at {bundled}"

    target = (
        tmp_path
        / "aura_root"
        / "aura"
        / "drones"
        / "bundled"
        / "web-research"
    )
    target.mkdir(parents=True)

    (target / "drone.json").write_text(
        (bundled / "drone.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (target / "main.py").write_text(
        (bundled / "main.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return target


@pytest.fixture
def drone(workspace: Path, web_research_folder: Path) -> DroneStore:
    d = DroneStore.load_drone(workspace, "web-research")
    assert d is not None, "web-research drone should be loadable from the store"
    return d


# ---------------------------------------------------------------------------
# Store / manifest tests
# ---------------------------------------------------------------------------


class TestStoreLoading:
    def test_web_research_loads_from_store(self, workspace: Path, drone: DroneStore) -> None:
        assert drone.id == "web-research"
        assert drone.kind == "command"
        assert drone.write_policy == "read_only"
        assert drone.entrypoint["protocol"] == "json-stdio"

    def test_web_research_appears_in_list_drones(
        self, workspace: Path, web_research_folder: Path
    ) -> None:
        ids = [d.id for d in DroneStore.list_drones(workspace)]
        assert "web-research" in ids

    def test_manifest_validates_as_command_read_only_drone(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        assert drone.kind == "command"
        assert drone.write_policy == "read_only"
        assert drone.entrypoint["protocol"] == "json-stdio"
        assert isinstance(drone.output_contract, dict)
        assert drone.budget.timeout_seconds == 120


# ---------------------------------------------------------------------------
# Execution tests  (run through the real runner)
# ---------------------------------------------------------------------------


class TestRunWithRunner:
    def test_run_with_query_line_goal(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(
            workspace,
            "web-research",
            drone,
            "query: World Cup matches today\nfreshness: today",
        )
        assert result["ok"] is True
        assert result["status"] == "completed"
        assert "Completed live web research" in result["summary"]

    def test_plain_goal_text_as_query(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        goal = "What is the latest Python version?"
        result = run_read_only_drone_sync(workspace, "web-research", drone, goal)
        assert result["ok"] is True
        assert result["status"] == "completed"
        assert "Completed live web research" in result["summary"]

    def test_empty_goal_returns_failure(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(workspace, "web-research", drone, "")
        assert result["ok"] is False
        assert result["status"] == "failed"
        assert "no query" in result["summary"].lower()

    def test_whitespace_only_goal_returns_failure(
        self, workspace: Path, drone: DroneStore
    ) -> None:
        result = run_read_only_drone_sync(
            workspace, "web-research", drone, "   \t  \n  "
        )
        assert result["ok"] is False
        assert result["status"] == "failed"
        assert "no query" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Output shape  (run the drone directly to capture full stdout JSON)
# ---------------------------------------------------------------------------


class TestOutputShape:
    _PAYLOAD = {
        "goal": "What is the capital of France?",
        "workspace_root": "/tmp",
        "drone_id": "web-research",
        "input": {},
        "upstream": {},
    }

    def _run_drone(
        self, folder: Path, goal: str, monkeypatch, block_real_subprocess
    ) -> dict:
        payload = {**self._PAYLOAD, "goal": goal}
        monkeypatch.setattr(subprocess, "run", block_real_subprocess)
        monkeypatch.setenv("_AURA_MOCK_WEB_RESEARCH", "1")
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=folder,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        return json.loads(proc.stdout.strip())

    def test_returned_cargo_shape(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "What is the capital of France?",
            monkeypatch, block_real_subprocess,
        )

        required_keys = {
            "ok", "summary", "query", "answer",
            "verified_facts", "sources", "evidence", "gaps",
            "confidence", "trace", "route_used",
        }
        assert required_keys.issubset(data.keys()), (
            f"Missing keys: {required_keys - data.keys()}"
        )

        assert isinstance(data["verified_facts"], list)
        assert isinstance(data["gaps"], list)
        assert isinstance(data["trace"], list)
        assert isinstance(data["route_used"], dict)

        assert data["ok"] is True
        assert isinstance(data["answer"], str)
        assert data["confidence"] in {"low", "none"}
        assert data["query"] == "What is the capital of France?"
        assert "live_research_ready" not in data

    def test_empty_goal_direct_output(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "", monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is False
        assert "query is required" in data["error"]
        assert "no query" in data["summary"].lower()

    def test_mocked_world_cup_schedule_result(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "What time are World Cup matches today?",
            monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is True
        assert data["confidence"] != "none"
        assert "USA vs England" in data["answer"]
        assert "8:00 PM GMT" in data["answer"]
        assert any(
            "USA vs England" in fact and "8:00 PM GMT" in fact
            for fact in data["verified_facts"]
        )
        assert data["sources"]
        assert data["evidence"]
        assert any("World Cup Matches Today: USA vs ENG" in ev["excerpt"] for ev in data["evidence"])
        assert any("fifa.com" in src["url"] for src in data["sources"])

    def test_mocked_schedule_evidence_without_parse_returns_gap(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "What time are matches today?",
            monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is True
        assert data.get("answer", "") == ""
        assert data["confidence"] in ["none", "low"]
        assert data["sources"]
        assert data["evidence"]
        assert any("No extractable schedule match and time" in gap for gap in data["gaps"])

    def test_mocked_no_evidence_result(
        self, web_research_folder: Path, monkeypatch, block_real_subprocess
    ) -> None:
        data = self._run_drone(
            web_research_folder, "fail query",
            monkeypatch, block_real_subprocess,
        )
        assert data["ok"] is True
        assert data["confidence"] in ["none", "low"]
        assert len(data["gaps"]) > 0
        assert "HTTP fetch error" in data["gaps"][0]


class TestScheduleExtraction:
    def test_world_cup_query_keeps_world_cup_subject(self) -> None:
        module = _load_web_research_main()
        answer, facts, evidence, gaps, confidence = module.extract_schedule_answer(
            "What time are World Cup matches today?",
            "World Cup Matches Today: USA vs ENG 8:00 PM GMT",
            dt.datetime.now(dt.timezone.utc),
        )

        assert answer == "World Cup matches today: USA vs England at 8:00 PM GMT."
        assert facts == ["USA vs England is listed at 8:00 PM GMT."]
        assert evidence
        assert confidence == "medium"

    def test_generic_schedule_query_does_not_mention_world_cup(self) -> None:
        module = _load_web_research_main()
        answer, _, _, _, confidence = module.extract_schedule_answer(
            "What time are matches today?",
            "World Cup Matches Today: USA vs ENG 8:00 PM GMT",
            dt.datetime.now(dt.timezone.utc),
        )

        assert answer == "Matches today: USA vs England at 8:00 PM GMT."
        assert "World Cup" not in answer
        assert confidence == "medium"

    def test_play_next_query_uses_next_match_subject(self) -> None:
        module = _load_web_research_main()
        answer, _, _, _, confidence = module.extract_schedule_answer(
            "Who does the USA play next?",
            "Upcoming: USA vs ENG 8:00 PM GMT",
            dt.datetime.now(dt.timezone.utc),
        )

        assert answer == "Next match: USA vs England at 8:00 PM GMT."
        assert confidence == "medium"

    def test_team_play_query_uses_team_subject(self) -> None:
        module = _load_web_research_main()
        answer, _, _, _, confidence = module.extract_schedule_answer(
            "What time does Arsenal play tomorrow?",
            "Fixture: Arsenal vs Chelsea 3:00 PM ET",
            dt.datetime.now(dt.timezone.utc),
        )

        assert answer == "Arsenal match tomorrow: Arsenal vs Chelsea at 3:00 PM ET."
        assert confidence == "medium"

    def test_no_parse_path_returns_gap_and_low_confidence(self) -> None:
        module = _load_web_research_main()
        answer, facts, evidence, gaps, confidence = module.extract_schedule_answer(
            "What time are matches today?",
            "Schedule information is available but no time appears here.",
            dt.datetime.now(dt.timezone.utc),
        )

        assert answer == ""
        assert facts == []
        assert evidence
        assert confidence in {"none", "low"}
        assert any("No extractable schedule match and time" in gap for gap in gaps)


class TestMultiSourcePipeline:
    def test_world_cup_multiple_matches_return_all_times(self) -> None:
        module = _load_web_research_main()
        target = module.SourceTarget("https://www.fifa.com/en/match-center", "FIFA Match Centre")
        fetched = [
            module.FetchedSource(
                target=target,
                title="FIFA Match Centre",
                text=(
                    "World Cup Matches Today: Panama vs England - 5:00 PM ET. "
                    "Croatia vs Ghana at 8:00 PM ET."
                ),
                fetched_at="2026-06-27T12:00:00+00:00",
                ok=True,
                excerpt="Panama vs England - 5:00 PM ET. Croatia vs Ghana at 8:00 PM ET.",
            )
        ]

        extracted = module.extract_answer(
            "What time are World Cup matches today?",
            ["world_cup", "schedule", "today"],
            fetched,
            dt.datetime.now(dt.timezone.utc),
        )

        assert extracted.answer == (
            "World Cup matches today: Panama vs England at 5:00 PM ET; "
            "Croatia vs Ghana at 8:00 PM ET."
        )
        assert len(extracted.verified_facts) == 2
        assert len(extracted.evidence) == 2
        assert extracted.confidence == "medium"

    def test_first_source_failure_second_source_success_returns_answer(self) -> None:
        module = _load_web_research_main()
        failed = module.SourceTarget("https://example.test/fail", "Failed Source")
        good = module.SourceTarget("https://example.test/schedule", "Schedule Source")
        fetched = [
            module.FetchedSource(
                target=failed,
                title="Failed Source",
                text="",
                fetched_at="2026-06-27T12:00:00+00:00",
                ok=False,
                error="HTTP fetch error: timeout",
            ),
            module.FetchedSource(
                target=good,
                title="Schedule Source",
                text="World Cup Matches Today: USA v England 8:00 PM GMT",
                fetched_at="2026-06-27T12:00:01+00:00",
                ok=True,
                excerpt="World Cup Matches Today: USA v England 8:00 PM GMT",
            ),
        ]
        query = "What time are World Cup matches today?"
        tags = ["world_cup", "schedule", "today"]

        extracted = module.extract_answer(query, tags, fetched, dt.datetime.now(dt.timezone.utc))
        result = module.build_result(query, tags, [failed, good], fetched, extracted)

        assert result["answer"] == "World Cup matches today: USA vs England at 8:00 PM GMT."
        assert result["confidence"] == "medium"
        assert any("Could not reach https://example.test/fail" in gap for gap in result["gaps"])
        assert any("another source supplied extractable evidence" in gap for gap in result["gaps"])
        assert len(result["sources"]) == 2

    def test_all_sources_fail_returns_confidence_none_and_no_answer(self) -> None:
        module = _load_web_research_main()
        targets = [
            module.SourceTarget("https://example.test/one", "One"),
            module.SourceTarget("https://example.test/two", "Two"),
        ]
        fetched = [
            module.FetchedSource(
                target=targets[0],
                title="One",
                text="",
                fetched_at="2026-06-27T12:00:00+00:00",
                ok=False,
                error="HTTP fetch error: timeout",
            ),
            module.FetchedSource(
                target=targets[1],
                title="Two",
                text="",
                fetched_at="2026-06-27T12:00:01+00:00",
                ok=False,
                error="HTTP fetch error: 500",
            ),
        ]
        query = "What time are World Cup matches today?"
        tags = ["world_cup", "schedule", "today"]

        extracted = module.extract_answer(query, tags, fetched, dt.datetime.now(dt.timezone.utc))
        result = module.build_result(query, tags, targets, fetched, extracted)

        assert result["answer"] == ""
        assert result["confidence"] == "none"
        assert result["evidence"] == []
        assert any("No useful evidence" in gap for gap in result["gaps"])

    def test_evidence_without_parse_returns_low_confidence_and_gap(self) -> None:
        module = _load_web_research_main()
        target = module.SourceTarget("https://example.test/schedule", "Schedule Source")
        fetched = [
            module.FetchedSource(
                target=target,
                title="Schedule Source",
                text="Schedule information is available, but no kickoff time appears.",
                fetched_at="2026-06-27T12:00:00+00:00",
                ok=True,
                excerpt="Schedule information is available, but no kickoff time appears.",
            )
        ]

        extracted = module.extract_answer(
            "What time are matches today?",
            ["schedule", "today"],
            fetched,
            dt.datetime.now(dt.timezone.utc),
        )

        assert extracted.answer == ""
        assert extracted.confidence == "low"
        assert extracted.evidence
        assert any("No extractable schedule match and time" in gap for gap in extracted.gaps)

    def test_generic_current_info_does_not_claim_medium_without_extraction(self) -> None:
        module = _load_web_research_main()
        target = module.SourceTarget("https://example.test/current", "Current Source")
        fetched = [
            module.FetchedSource(
                target=target,
                title="Current Source",
                text="A current page was fetched, but it does not clearly answer the question.",
                fetched_at="2026-06-27T12:00:00+00:00",
                ok=True,
                excerpt="A current page was fetched, but it does not clearly answer the question.",
            )
        ]

        extracted = module.extract_answer(
            "What is the latest Python version?",
            ["current_info"],
            fetched,
            dt.datetime.now(dt.timezone.utc),
        )

        assert extracted.answer == ""
        assert extracted.verified_facts == []
        assert extracted.confidence == "low"

    def test_retired_research_paths_are_absent(self) -> None:
        retired = [
            "run" + "_research",
            "research" + "_current_info",
            "_research" + "_mixin",
            "_research" + "_schemas",
        ]
        root = Path(__file__).resolve().parent.parent
        candidates = list((root / "aura").rglob("*.py"))
        haystack = "\n".join(path.read_text(encoding="utf-8") for path in candidates)

        for symbol in retired:
            assert symbol not in haystack
