from __future__ import annotations

import importlib.abc
import json
import os
import sys
import tempfile

import pytest


class _BlockBrokerSdkFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: object | None, target: object | None = None):
        if fullname == "longport" or fullname.startswith("longport."):
            raise ImportError("longport SDK imports are disabled in tests")
        if fullname == "longbridge" or fullname.startswith("longbridge."):
            raise ImportError("longbridge SDK imports are disabled in tests")
        return None


if os.environ.get("AUTO_TRADE_ALLOW_BROKER_SDK_IMPORTS") != "1":
    sys.meta_path.insert(0, _BlockBrokerSdkFinder())


os.environ["AUTO_TRADE_DATABASE_URL"] = os.environ.get(
    "AUTO_TRADE_TEST_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_pytest_{os.getpid()}.db",
)
# The strict session-reentrancy guard raises only under ``env == "test"``, so
# without this the suite ran at the ``dev`` default and the guard was installed,
# strict, and inert: a new nested session logged a warning into captured output
# and the run stayed green. Pin the environment so "a new re-entrancy fails CI"
# does not depend on how the runner happened to be invoked.
os.environ["AUTO_TRADE_ENV"] = "test"
os.environ["AUTO_TRADE_CREDENTIAL_KEY_PATH"] = os.path.join(
    tempfile.gettempdir(),
    f"auto_trade_cred_key_{os.getpid()}.pem",
)

for name in (
    "AUTO_TRADE_API_KEY",
    "CREDENTIAL_MASTER_KEY",
    "DEEPSEEK_API_KEY",
    "MINIMAX_API_KEY",
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
):
    os.environ[name] = ""

os.environ["AUTO_TRADE_LLM_PROVIDER"] = "deepseek"
os.environ["MINIMAX_BASE_URL"] = "https://api.minimaxi.com/v1"
os.environ["MINIMAX_API_URL"] = ""
os.environ["MINIMAX_MODEL"] = "MiniMax-M3"
os.environ["MINIMAX_THINKING_TYPE"] = "adaptive"
os.environ["MINIMAX_MAX_COMPLETION_TOKENS"] = "8192"
os.environ["AUTO_TRADE_BROKER_POSITION_SNAPSHOT_ISOLATION_ENABLED"] = "false"


# `--dist loadgroup` schedules by group name, so a group named after the module
# reproduces `--dist loadfile`. Grouping here rather than in addopts lets modules
# that must share a worker share one name, and lets audited modules carry no
# group at all so their tests distribute individually.

RELIABILITY_DB_GROUP = "reliability_shared_db"
"""One group for the three modules that deliberately share one SQLite file.

Each of tests/test_{order_lifecycle,position_probe,reconciliation}_reliability.py
sets the same pid-scoped `AUTO_TRADE_DATABASE_URL` at import time and deletes the
db/-wal/-shm files the first time it wins that race. Splitting them across
workers lets one module's import truncate another's live fixture data.
"""

_RELIABILITY_DB_MODULES = frozenset(
    {
        "tests/test_order_lifecycle_reliability.py",
        "tests/test_position_probe_reliability.py",
        "tests/test_reconciliation_reliability.py",
    }
)

WALL_CLOCK_MODULES = frozenset(
    {
        "tests/test_watchlist_quant_v6_cron.py",
        "tests/test_watchlist_quant_v6_publication_service.py",
        "tests/test_watchlist_quant_v6_spawn_supervisor.py",
    }
)
"""Modules that spawn compute processes and assert on elapsed wall-clock.

Bounds like `2.5 <= elapsed < 8` hold serially and miss under `-n 4` on a
4-vCPU runner, where four xdist workers plus each test's own spawned workers
oversubscribe the CPU. CI runs these in one serial job so they get the machine;
the numbered shards skip them.

Only the three modules that have actually failed that way are listed: run
33418588994 lost cron, publication_service and spawn_supervisor, and run
33533855333 lost cron and spawn_supervisor again. deadline, evaluation_service
and historical_provider assert on elapsed time too but have never missed, and
serialising them as well would cost more than it buys.
"""

_DURATIONS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".test_durations")

TEST_LEVEL_SAFE_MODULES: frozenset[str] = frozenset(
    {
        "tests/test_strategy_v2_shadow_service.py",
        "tests/test_opening_momentum_shadow_service.py",
        "tests/test_trade_execution_service.py",
        "tests/test_universe_selection_service.py",
        "tests/test_research_observation_health_service.py",
        "tests/test_broker.py",
        "tests/test_strategy_v2_portfolio_service.py",
        "tests/test_e2e_restart.py",
        "tests/platform/test_api_risk_portfolio.py",
    }
)
"""Modules audited as safe to distribute one test at a time.

Membership requires no module-level mutable state, no ordering dependency
between tests, per-test database ownership, and no wall-clock assertions. These
are the longest files in the suite, so grouping them puts their whole runtime on
the critical path. Emptying this set is the rollback.

test_api.py and test_main.py stay grouped despite their size: the first shares a
module-level TestClient across tests that never reset their tables, the second
mutates a module singleton and waits on threads.

test_runner.py stays grouped too. Its tests pass alone under `--dist load` but
four of the LLM-execution ones turn FILLED into SKIPPED once a worker also holds
tests from other modules, so something outside the file leaks into the execution
path. Grouping keeps that leak masked exactly as `--dist loadfile` did; freeing
the file needs the leak found first.
"""


def xdist_group_for(nodeid: str) -> str | None:
    """Return the xdist group for one test, or None to distribute it freely."""
    module = nodeid.split("::", 1)[0]
    if module in TEST_LEVEL_SAFE_MODULES:
        return None
    if module in _RELIABILITY_DB_MODULES:
        return RELIABILITY_DB_GROUP
    return module


def assign_modules_to_shards(
    module_weights: dict[str, float],
    shard_count: int,
) -> dict[str, int]:
    """Pack whole modules into shards, heaviest first onto the lightest shard.

    Modules are the unit because several only pass in file order, so a module
    split across runners fails. Sorting by weight then path keeps every runner's
    answer identical without them communicating.
    """
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    loads = [0.0] * shard_count
    counts = [0] * shard_count
    assignment: dict[str, int] = {}
    ordered = sorted(module_weights.items(), key=lambda item: (-item[1], item[0]))
    for module, weight in ordered:
        target = min(
            range(shard_count),
            key=lambda index: (loads[index], counts[index], index),
        )
        assignment[module] = target
        loads[target] += weight
        counts[target] += 1
    return assignment


def _module_weights(modules: set[str]) -> dict[str, float]:
    weights = {module: 0.0 for module in modules}
    try:
        with open(_DURATIONS_PATH, encoding="utf-8") as handle:
            recorded = json.load(handle)
    except (OSError, ValueError):
        return weights
    for nodeid, seconds in recorded.items():
        module = nodeid.split("::", 1)[0]
        if module in weights:
            weights[module] += float(seconds)
    return weights


def _shard_selection() -> tuple[int, int] | None:
    raw_index = os.environ.get("AUTO_TRADE_TEST_SHARD", "").strip()
    raw_count = os.environ.get("AUTO_TRADE_TEST_SHARD_COUNT", "").strip()
    if not raw_index or not raw_count:
        return None
    index, count = int(raw_index), int(raw_count)
    if count < 1 or not 1 <= index <= count:
        raise ValueError(
            f"AUTO_TRADE_TEST_SHARD={index} is outside 1..{count}"
        )
    return index - 1, count


def _realtime_only() -> bool:
    return os.environ.get("AUTO_TRADE_TEST_REALTIME_ONLY", "").strip() == "1"


def _selected_modules(modules: set[str]) -> set[str] | None:
    if _realtime_only():
        return modules & WALL_CLOCK_MODULES
    selection = _shard_selection()
    if selection is None:
        return None
    shard_index, shard_count = selection
    shardable = modules - WALL_CLOCK_MODULES
    assignment = assign_modules_to_shards(_module_weights(shardable), shard_count)
    return {
        module for module, shard in assignment.items() if shard == shard_index
    }


# tryfirst: xdist's worker-side hook appends the group name to each nodeid, and
# it only sees markers already attached when it runs.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    selected = _selected_modules({item.nodeid.split("::", 1)[0] for item in items})
    if selected is not None:
        keep, dropped = [], []
        for item in items:
            target = keep if item.nodeid.split("::", 1)[0] in selected else dropped
            target.append(item)
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = keep
    for item in items:
        group = xdist_group_for(item.nodeid)
        if group is not None:
            item.add_marker(pytest.mark.xdist_group(group))
