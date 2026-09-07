from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Literal

from app.core.market_calendar import trade_day_for


@dataclass(frozen=True, slots=True)
class BoardLotRecord:
    lot_size: int
    validated_for_session: date
    observed_at: datetime
    validated_in_rth: bool


@dataclass(frozen=True, slots=True)
class BoardLotResolution:
    symbol: str
    lot_size: int | None
    source: Literal["FRESH", "STALE", "UNKNOWN"]
    stale_lot_size: int | None = None
    validated_for_session: date | None = None

    @classmethod
    def for_unresolved(cls, symbol: str) -> BoardLotResolution:
        if symbol.endswith(".HK"):
            return cls(symbol, None, "UNKNOWN")
        return cls(symbol, 1, "FRESH")


def quantize_to_board_lot(quantity: Decimal, lot_size: int) -> Decimal:
    """Floor to a lot multiple. NEVER rounds up."""
    if lot_size < 1:
        raise ValueError("lot_size must be positive")
    if not quantity.is_finite() or quantity < 0:
        raise ValueError("quantity must be finite and nonnegative")
    # Integer arithmetic avoids Decimal context rounding across a lot boundary.
    numerator, denominator = quantity.as_integer_ratio()
    return Decimal((numerator // (denominator * lot_size)) * lot_size)


class BoardLotCache:
    """Store metadata under a private lock; derive freshness from the session day."""

    def __init__(self, *, session_day_for: Callable[[str], date] = trade_day_for) -> None:
        self._session_day_for = session_day_for
        self._lock = Lock()
        self._records: dict[str, BoardLotRecord] = {}

    def put(
        self,
        symbol: str,
        lot_size: int,
        *,
        session_day: date,
        in_rth: bool,
        observed_at: datetime | None = None,
    ) -> None:
        if lot_size < 1:
            raise ValueError("lot_size must be positive")
        record = BoardLotRecord(
            lot_size, session_day, observed_at or datetime.now(timezone.utc), in_rth
        )
        with self._lock:
            self._records[symbol] = record

    def resolve(self, symbol: str) -> BoardLotResolution:
        if not symbol.endswith(".HK"):
            return BoardLotResolution.for_unresolved(symbol)
        session_day = self._session_day_for("HK")
        with self._lock:
            record = self._records.get(symbol)
        if record is None:
            return BoardLotResolution.for_unresolved(symbol)
        if record.validated_for_session == session_day:
            return BoardLotResolution(
                symbol, record.lot_size, "FRESH",
                validated_for_session=record.validated_for_session,
            )
        return BoardLotResolution(
            symbol, None, "STALE", record.lot_size, record.validated_for_session
        )

    def missing_or_stale_hk(self, symbols: Iterable[str]) -> list[str]:
        return [
            symbol for symbol in symbols
            if symbol.endswith(".HK") and self.resolve(symbol).lot_size is None
        ]

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            records = sorted(self._records.items())
        return [
            {
                "symbol": symbol,
                "lot_size": record.lot_size,
                "validated_for_session": record.validated_for_session.isoformat(),
                "observed_at": record.observed_at.isoformat(),
                "validated_in_rth": record.validated_in_rth,
            }
            for symbol, record in records
        ]
