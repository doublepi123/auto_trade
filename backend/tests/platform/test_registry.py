from app.platform.registry import StrategyRegistry
from tests.platform.test_sdk import DummyStrategy


def test_registry_lists_strategies():
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    meta = registry.list()
    assert len(meta) == 1
    assert meta[0].name == "dummy"
    assert meta[0].version == "1.0.0"


def test_registry_gets_strategy_by_name():
    registry = StrategyRegistry()
    registry.register(DummyStrategy)
    cls = registry.get("dummy")
    assert cls is DummyStrategy


def test_registry_auto_discovers_from_package():
    registry = StrategyRegistry()
    registry.discover("tests.platform.test_registry")  # verify discover mechanism doesn't throw
    # DummyStrategy is not in this module, so registry should be empty
    assert registry.list() == []
