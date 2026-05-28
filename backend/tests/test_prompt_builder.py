from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_prompt_builder.db",
)

import pytest
from app.domain.prompt.base import PromptModule
from app.domain.prompt.system_module import SystemModule


class TestPromptModule:
    def test_base_class_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            PromptModule()  # type: ignore[abstract]

    def test_system_module_renders_role(self) -> None:
        module = SystemModule()
        result = module.render({})
        assert "量化交易顾问" in result
        assert len(result) > 10
