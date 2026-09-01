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

    def test_test_job_splits_by_the_matrix_group(self) -> None:
        commands = _run_commands("backend-test")
        assert "--splits 4" in commands
        assert "--group ${{ matrix.group }}" in commands

    def test_test_job_reads_the_committed_durations(self) -> None:
        assert "--durations-path" in _run_commands("backend-test")

    def test_shards_are_balanced_by_measured_duration(self) -> None:
        assert "--splitting-algorithm least_duration" in _run_commands(
            "backend-test"
        )

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

    def test_coverage_job_waits_for_every_shard(self) -> None:
        assert _jobs()["backend-coverage"]["needs"] == ["backend-test"]


class TestReleaseGateCoversEveryCheck:
    """Every gate job must block the image push, or a red check ships anyway."""

    def test_dockerhub_needs_all_gate_jobs(self) -> None:
        needs = set(cast("list[str]", _jobs()["dockerhub"]["needs"]))
        assert needs == {
            "backend-test",
            "backend-coverage",
            "backend-typecheck",
            "frontend-check",
            "frontend-e2e",
        }

    @pytest.mark.parametrize(
        "job",
        [
            "backend-test",
            "backend-coverage",
            "backend-typecheck",
            "frontend-check",
            "frontend-e2e",
        ],
    )
    def test_every_gate_job_is_defined(self, job: str) -> None:
        assert job in _jobs()
