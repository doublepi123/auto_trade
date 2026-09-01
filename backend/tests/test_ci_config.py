from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "dockerhub.yml"
)


def _workflow() -> dict[str, Any]:
    with _WORKFLOW.open(encoding="utf-8") as handle:
        return cast(dict[str, Any], yaml.safe_load(handle))


def _jobs() -> dict[str, Any]:
    return cast(dict[str, Any], _workflow()["jobs"])


def _steps(job: str) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _jobs()[job]["steps"])


def _run_commands(job: str) -> str:
    return "\n".join(
        str(step.get("run", "")) for step in _steps(job) if "run" in step
    )


def _step_env(job: str, step_name: str) -> dict[str, Any]:
    for step in _steps(job):
        if step.get("name") == step_name:
            return cast(dict[str, Any], step.get("env", {}))
    raise AssertionError(f"{job} has no step named {step_name!r}")


class TestTypecheckRunsAsItsOwnJob:
    """basedpyright must not queue behind the test suite.

    It costs 49s against pytest's 36+ minutes, and when pytest fails the
    typecheck step is skipped entirely, so a run that breaks both reports only
    one of them.
    """

    def test_typecheck_job_exists(self) -> None:
        assert "backend-typecheck" in _jobs()

    def test_typecheck_job_runs_basedpyright(self) -> None:
        assert "basedpyright" in _run_commands("backend-typecheck")

    def test_test_job_no_longer_runs_basedpyright(self) -> None:
        assert "basedpyright" not in _run_commands("backend-test")

    def test_typecheck_job_does_not_wait_on_the_test_job(self) -> None:
        assert "needs" not in _jobs()["backend-typecheck"]


class TestBackendTestJobStillRunsTheSuite:
    def test_test_job_runs_pytest(self) -> None:
        assert "pytest" in _run_commands("backend-test")

    def test_test_job_works_in_the_backend_directory(self) -> None:
        defaults = cast(dict[str, Any], _jobs()["backend-test"]["defaults"])
        assert defaults["run"]["working-directory"] == "./backend"


class TestSuiteIsShardedAcrossRunners:
    """One 4-vCPU runner cannot go faster; more runners can.

    `-n 4` already saturates a standard runner, so the only remaining lever is
    splitting the suite over several of them.
    """

    def test_test_job_declares_a_shard_matrix(self) -> None:
        groups = _jobs()["backend-test"]["strategy"]["matrix"]["group"]
        assert groups == [1, 2, 3, 4]

    def test_shards_keep_running_when_one_fails(self) -> None:
        assert _jobs()["backend-test"]["strategy"]["fail-fast"] is False

    def test_each_shard_is_told_which_slice_to_run(self) -> None:
        env = _step_env("backend-test", "Run pytest")
        assert env["AUTO_TRADE_TEST_SHARD"] == "${{ matrix.group }}"
        assert str(env["AUTO_TRADE_TEST_SHARD_COUNT"]) == "4"

    def test_sharding_is_not_delegated_to_a_test_level_splitter(self) -> None:
        # Splitting per test tears order-dependent modules apart across
        # runners; conftest assigns whole modules instead.
        assert "--splits" not in _run_commands("backend-test")

    def test_durations_file_is_committed(self) -> None:
        assert (Path(__file__).resolve().parents[1] / ".test_durations").is_file()


class TestCoverageIsGatedOnTheCombinedTotal:
    """A shard only executes part of the suite, so it only sees part of the
    coverage. Gating per shard would fail every shard; the threshold has to move
    to a job that has combined all four."""

    def test_shards_do_not_gate_on_their_own_partial_coverage(self) -> None:
        assert "--cov-fail-under" not in _run_commands("backend-test")

    def test_shards_still_measure_coverage(self) -> None:
        assert "--cov=app" in _run_commands("backend-test")

    def test_coverage_job_combines_every_shard(self) -> None:
        assert "coverage combine" in _run_commands("backend-coverage")

    def test_coverage_job_enforces_the_original_threshold(self) -> None:
        assert "--fail-under=80" in _run_commands("backend-coverage")


class TestWallClockTestsGetTheirOwnSerialJob:
    """Deadline assertions need the runner, not a quarter of it."""

    def test_realtime_job_exists(self) -> None:
        assert "backend-realtime" in _jobs()

    def test_realtime_job_selects_only_the_wall_clock_modules(self) -> None:
        env = _step_env("backend-realtime", "Run pytest")
        assert env["AUTO_TRADE_TEST_REALTIME_ONLY"] == "1"

    def test_realtime_job_runs_without_parallel_workers(self) -> None:
        assert "-p no:xdist" in _run_commands("backend-realtime")

    def test_realtime_job_still_measures_coverage(self) -> None:
        assert "--cov=app" in _run_commands("backend-realtime")

    def test_realtime_job_does_not_gate_on_its_own_slice(self) -> None:
        assert "--cov-fail-under" not in _run_commands("backend-realtime")

    def test_coverage_gate_waits_for_the_realtime_job_too(self) -> None:
        assert _jobs()["backend-coverage"]["needs"] == [
            "backend-test",
            "backend-realtime",
        ]


class TestReleaseGateCoversEveryCheck:
    """Every gate job must block the image push, or a red check ships anyway."""

    def test_dockerhub_needs_all_gate_jobs(self) -> None:
        needs = set(cast("list[str]", _jobs()["dockerhub"]["needs"]))
        assert needs == {
            "backend-test",
            "backend-realtime",
            "backend-coverage",
            "backend-typecheck",
            "frontend-check",
            "frontend-e2e",
        }

    @pytest.mark.parametrize(
        "job",
        [
            "backend-test",
            "backend-realtime",
            "backend-coverage",
            "backend-typecheck",
            "frontend-check",
            "frontend-e2e",
        ],
    )
    def test_every_gate_job_is_defined(self, job: str) -> None:
        assert job in _jobs()
