from __future__ import annotations

import pytest

from tests.conftest import (
    RELIABILITY_DB_GROUP,
    TEST_LEVEL_SAFE_MODULES,
    xdist_group_for,
)


_RELIABILITY_MODULES_UNDER_TEST = frozenset(
    {
        "tests/test_order_lifecycle_reliability.py",
        "tests/test_position_probe_reliability.py",
        "tests/test_reconciliation_reliability.py",
    }
)

_QUANT_V6_WALL_CLOCK_MODULES_UNDER_TEST = frozenset(
    {
        "tests/test_watchlist_quant_v6_cron.py",
        "tests/test_watchlist_quant_v6_deadline.py",
        "tests/test_watchlist_quant_v6_evaluation_service.py",
        "tests/test_watchlist_quant_v6_historical_provider.py",
        "tests/test_watchlist_quant_v6_publication_service.py",
        "tests/test_watchlist_quant_v6_spawn_supervisor.py",
    }
)
"""Modules whose tests assert on elapsed wall-clock while spawning processes.

Each keeps its own module group. They must never be freed to test granularity:
their timing bounds already sit close to the margin on a 4-vCPU runner, and
spreading one module's spawn-heavy tests across all workers at once is the
contention that breaks them.
"""


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

    tests/test_order_lifecycle_reliability.py:10-19 and its two siblings set the
    identical `AUTO_TRADE_DATABASE_URL` at import time, deleting the db/-wal/-shm
    files on first import. Interleaving them across workers corrupts fixture
    state, so they must land on a single worker.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "tests/test_order_lifecycle_reliability.py",
            "tests/test_position_probe_reliability.py",
            "tests/test_reconciliation_reliability.py",
        ],
    )
    def test_module_maps_to_the_shared_reliability_group(self, module: str) -> None:
        assert xdist_group_for(f"{module}::test_anything") == RELIABILITY_DB_GROUP

    def test_all_three_resolve_to_exactly_one_group(self) -> None:
        groups = {
            xdist_group_for(f"{module}::test_anything")
            for module in (
                "tests/test_order_lifecycle_reliability.py",
                "tests/test_position_probe_reliability.py",
                "tests/test_reconciliation_reliability.py",
            )
        }
        assert groups == {RELIABILITY_DB_GROUP}


class TestAuditedModulesDistributePerTest:
    """Audited modules carry no group, so their tests spread across workers.

    These are the long files that set the floor under `--dist loadfile`: the run
    cannot finish before the single slowest file does.
    """

    @pytest.mark.parametrize(
        "module",
        [
            "tests/test_strategy_v2_shadow_service.py",
            "tests/test_opening_momentum_shadow_service.py",
            "tests/test_trade_execution_service.py",
            "tests/test_universe_selection_service.py",
            "tests/test_research_observation_health_service.py",
            "tests/test_broker.py",
            "tests/test_strategy_v2_portfolio_service.py",
            "tests/test_e2e_restart.py",
            "tests/platform/test_api_risk_portfolio.py",
        ],
    )
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
        assert not (TEST_LEVEL_SAFE_MODULES & _QUANT_V6_WALL_CLOCK_MODULES_UNDER_TEST)

    def test_reliability_modules_are_never_freed(self) -> None:
        assert not (TEST_LEVEL_SAFE_MODULES & _RELIABILITY_MODULES_UNDER_TEST)


class TestNodeidsWithoutASeparator:
    def test_bare_module_nodeid_is_its_own_group(self) -> None:
        assert xdist_group_for("tests/test_fees.py") == "tests/test_fees.py"
