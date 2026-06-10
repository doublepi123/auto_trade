from __future__ import annotations

import importlib.abc
import os
import sys
import tempfile


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
    "LONGPORT_APP_KEY",
    "LONGPORT_APP_SECRET",
    "LONGPORT_ACCESS_TOKEN",
    "LONGBRIDGE_APP_KEY",
    "LONGBRIDGE_APP_SECRET",
    "LONGBRIDGE_ACCESS_TOKEN",
):
    os.environ[name] = ""
