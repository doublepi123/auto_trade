from __future__ import annotations

import importlib.abc
import os
import sys
import tempfile
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable


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


# tryfirst: xdist's worker-side hook appends the group name to each nodeid, and
# it only sees markers already attached when it runs.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: Iterable[pytest.Item]) -> None:
    for item in items:
        group = xdist_group_for(item.nodeid)
        if group is not None:
            item.add_marker(pytest.mark.xdist_group(group))
