from __future__ import annotations

import pytest

from tests.conftest import (
    RELIABILITY_DB_GROUP,
    TEST_LEVEL_SAFE_MODULES,
    WALL_CLOCK_MODULES,
    assign_modules_to_shards,
    xdist_group_for,
)

_RELIABILITY_MODULES_UNDER_TEST = frozenset(
    {
        "tests/test_order_lifecycle_reliability.py",
        "tests/test_position_probe_reliability.py",
        "tests/test_reconciliation_reliability.py",
    }
)

_CI_FAILED_WALL_CLOCK_MODULES = frozenset(
    {
        "tests/test_watchlist_quant_v6_cron.py",
        "tests/test_watchlist_quant_v6_publication_service.py",
        "tests/test_watchlist_quant_v6_spawn_supervisor.py",
    }
)
"""The modules that actually lost tests to missed deadlines on CI."""


class TestRealtimeLaneIsSeparated:
    """Wall-clock tests need the machine, not a share of it.

    These spawn their own compute processes and assert bounds like
    `2.5 <= elapsed < 8`. They pass serially and fail under `-n 4` on a 4-vCPU
    runner: runs 33418588994 and 33533855333 lost exactly these to missed
    deadlines while every other test passed. CI gives them their own serial job,
    so the numbered shards must not also run them.
    """

    def test_declared_wall_clock_modules_match_the_known_offenders(self) -> None:
        assert WALL_CLOCK_MODULES == _CI_FAILED_WALL_CLOCK_MODULES

    @pytest.mark.parametrize("module", sorted(_CI_FAILED_WALL_CLOCK_MODULES))
    def test_wall_clock_module_is_excluded_from_numbered_shards(
        self, module: str
    ) -> None:
        shardable = {name: 1.0 for name in _shardable(_CI_FAILED_WALL_CLOCK_MODULES)}
        assert module not in assign_modules_to_shards(shardable, shard_count=4)

    def test_wall_clock_modules_are_never_freed_to_test_level(self) -> None:
        assert not (TEST_LEVEL_SAFE_MODULES & WALL_CLOCK_MODULES)


def _shardable(modules: frozenset[str]) -> set[str]:
    return {module for module in modules if module not in WALL_CLOCK_MODULES}


class TestDefaultsToModuleGroup:
    """Every module is its own group, reproducing `--dist loadfile` exactly."""

    def test_plain_module_groups_by_its_own_nodeid(self) -> None:
        assert xdist_group_for("tests/test_engine.py::test_flat_to_long") == (
            "tests/test_engine.py"
        )

    def test_class_based_test_still_groups_by_module(self) -> None:
        nodeid = "tests/platform/test_kelly.py::TestKelly::test_half_kelly"
        assert xdist_group_for(nodeid) == "tests/platform/test_kelly.py"

    def test_parametrised_id_containing_colons_groups_by_module(self) -> None:
        nodeid = "tests/test_fees.py::test_round_trip[a::b-c]"
        assert xdist_group_for(nodeid) == "tests/test_fees.py"


class TestReliabilityTrioSharesOneGroup:
    """The three modules deliberately share one pid-scoped SQLite file.

    Each sets the identical `AUTO_TRADE_DATABASE_URL` at import time, deleting
    the db/-wal/-shm files on first import. Interleaving them across workers
    corrupts fixture state, so they must land on a single worker.
    """

    @pytest.mark.parametrize("module", sorted(_RELIABILITY_MODULES_UNDER_TEST))
    def test_module_maps_to_the_shared_reliability_group(self, module: str) -> None:
        assert xdist_group_for(f"{module}::test_anything") == RELIABILITY_DB_GROUP


class TestAuditedModulesDistributePerTest:
    """Audited modules carry no group, so their tests spread across workers.

    These are the long files that set the floor under `--dist loadfile`: the run
    cannot finish before the single slowest file does.
    """

    @pytest.mark.parametrize("module", sorted(TEST_LEVEL_SAFE_MODULES))
    def test_audited_module_carries_no_group(self, module: str) -> None:
        assert xdist_group_for(f"{module}::test_anything") is None

    @pytest.mark.parametrize(
        "module",
        [
            "tests/test_api.py",
            "tests/test_main.py",
            "tests/test_runner.py",
        ],
    )
    def test_order_dependent_modules_keep_their_module_group(
        self, module: str
    ) -> None:
        assert xdist_group_for(f"{module}::test_anything") == module

    def test_wall_clock_modules_are_never_freed(self) -> None:
        assert not (TEST_LEVEL_SAFE_MODULES & WALL_CLOCK_MODULES)

    def test_reliability_modules_are_never_freed(self) -> None:
        assert not (TEST_LEVEL_SAFE_MODULES & _RELIABILITY_MODULES_UNDER_TEST)


class TestShardingKeepsModulesWhole:
    """CI splits the suite over runners, and a module must not be split with it.

    Several modules only pass in file order: test_api.py reuses one TestClient
    without resetting tables between tests, and some modules set process-wide
    env a later test in the same file depends on. Splitting a module across
    runners breaks both, so shards are assigned whole modules.
    """

    def test_every_module_lands_on_exactly_one_shard(self) -> None:
        weights = {f"m{index}.py": float(index) for index in range(50)}
        assignment = assign_modules_to_shards(weights, shard_count=4)
        assert set(assignment) == set(weights)
        assert all(0 <= shard < 4 for shard in assignment.values())

    def test_assignment_does_not_depend_on_input_order(self) -> None:
        weights = {f"m{index}.py": float(index % 7) for index in range(40)}
        reversed_weights = dict(reversed(list(weights.items())))
        assert assign_modules_to_shards(weights, 4) == assign_modules_to_shards(
            reversed_weights, 4
        )

    def test_equally_heavy_modules_are_spread_across_shards(self) -> None:
        weights = {f"heavy{index}.py": 100.0 for index in range(4)}
        assignment = assign_modules_to_shards(weights, shard_count=4)
        assert sorted(assignment.values()) == [0, 1, 2, 3]

    def test_shard_loads_stay_within_one_module_of_each_other(self) -> None:
        weights = {f"m{index}.py": float(index) for index in range(1, 101)}
        assignment = assign_modules_to_shards(weights, shard_count=4)
        totals = [0.0] * 4
        for module, shard in assignment.items():
            totals[shard] += weights[module]
        assert max(totals) - min(totals) < max(weights.values())

    def test_single_shard_keeps_everything(self) -> None:
        weights = {"a.py": 1.0, "b.py": 2.0}
        assert set(assign_modules_to_shards(weights, shard_count=1).values()) == {0}

    def test_no_modules_yields_no_assignment(self) -> None:
        assert assign_modules_to_shards({}, shard_count=4) == {}

    def test_modules_missing_a_weight_are_still_assigned(self) -> None:
        assignment = assign_modules_to_shards({"a.py": 0.0, "b.py": 0.0}, 2)
        assert sorted(assignment.values()) == [0, 1]
