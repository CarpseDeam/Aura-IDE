"""Tests for ReadOnlyDroneBackgroundRunner — per-workspace scoping and queue draining."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from aura.drones.background_runner import (
    ReadOnlyDroneBackgroundRunner,
    get_background_runner,
    _runners,
)


@pytest.fixture(autouse=True)
def clear_runners():
    """Clear cached runners between tests."""
    _runners.clear()
    yield


def make_drone(drone_id: str = "test-drone", name: str = "Test Drone"):
    drone = MagicMock()
    drone.id = drone_id
    drone.name = name
    drone.write_policy = "read_only"
    drone.budget.timeout_seconds = 30
    drone.allowed_tools = None
    return drone


class TestGetBackgroundRunner:
    def test_returns_same_runner_for_same_root(self, tmp_path: Path):
        r1 = get_background_runner(tmp_path)
        r2 = get_background_runner(tmp_path)
        assert r1 is r2

    def test_different_roots_different_runners(self, tmp_path: Path):
        a = tmp_path / "project-a"
        b = tmp_path / "project-b"
        a.mkdir()
        b.mkdir()
        r1 = get_background_runner(a)
        r2 = get_background_runner(b)
        assert r1 is not r2

    def test_resolves_symlinks(self, tmp_path: Path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        r1 = get_background_runner(real)
        r2 = get_background_runner(link)
        assert r1 is r2


class TestMaxParallel:
    def test_respects_max_parallel(self):
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"), max_parallel=3)
        drone = make_drone()
        jobs = []
        for i in range(5):
            job = runner.launch(drone, f"goal-{i}")
            jobs.append(job)
        # Only 3 should be running, 2 queued
        assert runner.running_count <= 3
        assert runner.queued_count == max(0, 5 - 3)

    def test_can_configure_max_parallel(self):
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"), max_parallel=1)
        assert runner._max_parallel == 1


class TestQueueDraining:
    def test_queued_job_starts_when_running_finishes(self):
        """When a running job finishes, a queued job should start (slot frees)."""
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"), max_parallel=1)
        drone = make_drone()
        block_start = threading.Event()
        job1_started = threading.Event()

        with patch.object(runner, "_run_job") as mock_run:
            def _blocked_run(job, drone_def):
                job1_started.set()
                block_start.wait(timeout=5)
                job.status = "completed"
                job.ended_at = time.time()
                job._completion_event.set()
                runner._drain_queue()

            mock_run.side_effect = _blocked_run

            j1 = runner.launch(drone, "goal-1")
            assert job1_started.wait(timeout=5), "job1 never started"
            # Now job1 is in the blocked run — definitely still running

            j2 = runner.launch(drone, "goal-2")
            assert j1.status == "running"
            assert j2.status == "queued"

            # Release job1 so it completes and drains the queue
            block_start.set()
            # Wait for job2 to transition out of queued
            for _ in range(100):
                if j2.status != "queued":
                    break
                time.sleep(0.01)

            assert j2.status == "running" or j2.status == "completed"


class TestLaunchAndCheck:
    def test_launch_returns_job_immediately(self):
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"))
        drone = make_drone()
        job = runner.launch(drone, "test goal")
        assert job.run_id is not None
        assert job.status == "running"  # slot open, started immediately

    def test_get_returns_none_for_unknown(self):
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"))
        assert runner.get("nonexistent") is None

    def test_get_returns_job(self):
        runner = ReadOnlyDroneBackgroundRunner(Path("/tmp"))
        drone = make_drone()
        job = runner.launch(drone, "test")
        retrieved = runner.get(job.run_id)
        assert retrieved is not None
        assert retrieved.run_id == job.run_id
