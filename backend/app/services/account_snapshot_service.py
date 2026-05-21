from __future__ import annotations

import logging
import threading
from decimal import Decimal
from time import monotonic, perf_counter

from app.core.broker import BrokerGateway
from app.schemas import AccountResponse, CashBalanceSchema, PositionSchema

logger = logging.getLogger("auto_trade.account_snapshot")

_CACHE_TTL_SECONDS = 5.0
_CACHE_LOCK = threading.RLock()
_SNAPSHOT_CACHE: AccountResponse | None = None
_SNAPSHOT_CACHE_EXPIRES_AT = 0.0
_REFRESHING = False


def clear_account_snapshot_cache() -> None:
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_EXPIRES_AT, _REFRESHING
    with _CACHE_LOCK:
        _SNAPSHOT_CACHE = None
        _SNAPSHOT_CACHE_EXPIRES_AT = 0.0
        _REFRESHING = False


class AccountSnapshotService:
    def get_snapshot(self, broker: BrokerGateway) -> AccountResponse:
        global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_EXPIRES_AT, _REFRESHING

        now = monotonic()
        with _CACHE_LOCK:
            if _SNAPSHOT_CACHE is not None and now < _SNAPSHOT_CACHE_EXPIRES_AT:
                return _SNAPSHOT_CACHE
            if _REFRESHING and _SNAPSHOT_CACHE is not None:
                return _SNAPSHOT_CACHE
            _REFRESHING = True

        try:
            start = perf_counter()
            snapshot = self._load_snapshot(broker)
            elapsed_ms = (perf_counter() - start) * 1000
            logger.info("account snapshot refresh completed in %.1f ms", elapsed_ms)

            with _CACHE_LOCK:
                if snapshot.available:
                    _SNAPSHOT_CACHE = snapshot
                    _SNAPSHOT_CACHE_EXPIRES_AT = now + _CACHE_TTL_SECONDS
                    return snapshot

                if _SNAPSHOT_CACHE is not None:
                    logger.warning("account snapshot refresh failed; returning stale cached snapshot")
                    return _SNAPSHOT_CACHE

                return snapshot
        finally:
            with _CACHE_LOCK:
                _REFRESHING = False

    def _load_snapshot(self, broker: BrokerGateway) -> AccountResponse:
        available = True
        try:
            account_start = perf_counter()
            try:
                account = broker.get_account()
            finally:
                logger.info("account snapshot get_account completed in %.1f ms", (perf_counter() - account_start) * 1000)
            total_assets = float(account.total_assets)
            cash_balances = [
                CashBalanceSchema(
                    currency=cb.currency,
                    available_cash=float(cb.available_cash),
                    frozen_cash=float(cb.frozen_cash),
                )
                for cb in account.cash_balances
            ]
        except Exception:
            logger.exception("failed to get account balance")
            available = False
            total_assets = 0.0
            cash_balances = []

        try:
            positions_start = perf_counter()
            try:
                broker_positions = broker.get_positions()
            finally:
                logger.info("account snapshot get_positions completed in %.1f ms", (perf_counter() - positions_start) * 1000)
            positions: list[PositionSchema] = []
            for pos in broker_positions:
                try:
                    quote_start = perf_counter()
                    try:
                        quote = broker.get_quote(pos.symbol)
                    finally:
                        logger.info(
                            "account snapshot get_quote %s completed in %.1f ms",
                            pos.symbol,
                            (perf_counter() - quote_start) * 1000,
                        )
                    market_value = float(pos.quantity * Decimal(str(quote.last_price)))
                except Exception:
                    logger.warning("failed to get quote for %s, using avg_price fallback", pos.symbol)
                    market_value = float(pos.quantity * pos.avg_price)
                positions.append(PositionSchema(
                    symbol=pos.symbol,
                    side=pos.side,
                    quantity=float(pos.quantity),
                    avg_price=float(pos.avg_price),
                    market_value=market_value,
                ))
        except Exception:
            logger.exception("failed to get positions")
            available = False
            positions = []

        return AccountResponse(
            total_assets=total_assets,
            cash_balances=cash_balances,
            positions=positions,
            available=available,
            error=None if available else "Account data unavailable",
        )
