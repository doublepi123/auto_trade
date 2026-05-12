import shutil
import tempfile
from pathlib import Path

import os

from app.config import Settings


class TestSettings:
    def test_default_values(self) -> None:
        os.environ.pop("AUTO_TRADE_DATABASE_URL", None)
        s = Settings()
        assert s.env == "dev"
        assert s.database_url == "sqlite:///data/auto_trade.db"

    def test_default_strategy_empty(self) -> None:
        s = Settings()
        assert s.default_strategy["symbol"] == ""
        assert s.default_strategy["market"] == "US"

    def test_ensure_data_dir(self) -> None:
        s = Settings()
        data_dir = Path("data")
        s.ensure_data_dir()
        assert data_dir.exists()
