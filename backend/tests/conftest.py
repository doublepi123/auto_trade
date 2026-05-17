from __future__ import annotations

import os


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
