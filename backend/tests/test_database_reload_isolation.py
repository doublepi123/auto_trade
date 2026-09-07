from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Final

import pytest


_ISOLATION_PROBE: Final = """
import os
import sys
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from tests import conftest
from app import database

original_engine = database.engine
original_factory = database.SessionLocal
original_violation = database.SessionReentrancyViolation
expected_url = os.environ["AUTO_TRADE_DATABASE_URL"]
temporary_engines: list[Engine] = []
disposed_engines: list[Engine] = []
inject_failure = sys.argv[1] == "failure"

class Probe:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Iterator[None]:
        yield
        if database.SessionReentrancyViolation is not original_violation:
            temporary_engines.append(database.engine)
            event.listen(database.engine, "engine_disposed", disposed_engines.append)
        if inject_failure and item.name == "test_sqlite_wal_and_busy_timeout_enabled":
            raise AssertionError("intentional failure after database reload")

result = pytest.main([
    "tests/test_database.py", "-o", "addopts=", "-p", "no:cacheprovider", "-q",
], plugins=[Probe()])
assert result == (1 if inject_failure else 0), result
assert str(database.engine.url) == expected_url, database.engine.url
assert database.engine is original_engine
assert database.SessionLocal is original_factory
assert database.SessionReentrancyViolation is original_violation
assert len(temporary_engines) == 7, len(temporary_engines)
assert disposed_engines == temporary_engines
with database.SessionLocal() as session:
    assert session.get_bind() is original_engine
print("RESTORED_SUITE_DATABASE", database.engine.url)
"""


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_database_module_is_restored_after_test_file(outcome: str) -> None:
    # Given a fresh pytest process, independent of the parent's shard and DB.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AUTO_TRADE_TEST_")
    }
    env["AUTO_TRADE_ENV"] = "test"

    # When the migration module completes normally or a reloading test fails.
    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE, outcome],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # Then its real pytest teardown restores bindings and closes all seven pools.
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTORED_SUITE_DATABASE" in result.stdout
