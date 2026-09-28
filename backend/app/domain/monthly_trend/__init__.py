"""Monthly-trend research lane (pure computation).

Currently holds the frozen rule ``SPY_MONTHLY_SMA10_CASH_V1`` (see
``app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md``).  Everything in
this package is pure: data in, values out — no DB, no network, no
settings, no wall clock.

``data/splits.json`` is sealed split EVIDENCE (registered 2026-09-28,
decision 14.7 item 7): SPY and QQQ executed no split inside
2010-01-01..2021-12-31.  It is tracked in git and hashed into the pin.
"""
