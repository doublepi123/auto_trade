from __future__ import annotations

import os
import tempfile


os.environ["AUTO_TRADE_DATABASE_URL"] = os.environ.get(
    "AUTO_TRADE_TEST_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_pytest_{os.getpid()}.db",
)

for name in (
    "AUTO_TRADE_API_KEY",
    "CREDENTIAL_MASTER_KEY",
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
):
    os.environ[name] = ""
