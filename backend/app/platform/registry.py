from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass
from typing import Any

from app.platform.sdk import Strategy


@dataclass(frozen=True)
class StrategyMeta:
    name: str
    version: str
    parameter_schema: dict[str, Any]
    strategy_class: type[Strategy]


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, type[Strategy]] = {}

    def register(self, strategy_class: type[Strategy]) -> None:
        instance = strategy_class()
        name = instance.name
        if name in self._strategies:
            raise ValueError(f"Strategy '{name}' already registered")
        self._strategies[name] = strategy_class

    def get(self, name: str) -> type[Strategy]:
        if name not in self._strategies:
            raise KeyError(f"Strategy '{name}' not found")
        return self._strategies[name]

    def list(self) -> list[StrategyMeta]:
        result = []
        for name, cls in self._strategies.items():
            instance = cls()
            result.append(
                StrategyMeta(
                    name=instance.name,
                    version=instance.version,
                    parameter_schema=instance.parameter_schema,
                    strategy_class=cls,
                )
            )
        return sorted(result, key=lambda m: m.name)

    def discover(self, package_name: str = "app.strategies") -> None:
        package = importlib.import_module(package_name)
        path = getattr(package, "__path__", None)
        if path is None:
            return
        for _, module_name, _ in pkgutil.iter_modules(path, package.__name__ + "."):
            module = importlib.import_module(module_name)
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if obj is Strategy:
                    continue
                if issubclass(obj, Strategy):
                    self.register(obj)


def get_default_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    registry.discover("app.strategies")
    return registry
