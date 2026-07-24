from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import get_session, is_trading_hours
from app.domain.opening_momentum import (
    ALGORITHM_VERSION,
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
    shadow_round_trip_return_bps,
)
from app.models import (
    OpeningMomentumShadowRun,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.schemas import (
    OpeningMomentumRankResponse,
    OpeningMomentumShadowConfigResponse,
    OpeningMomentumShadowMetrics,
    OpeningMomentumShadowRunResponse,
    OpeningMomentumShadowStatusResponse,
)


logger = logging.getLogger("auto_trade.opening_momentum_shadow")

_CANDLE_COUNT = 500
_BAR_DURATION = timedelta(minutes=1)
_SETTLEMENT_GRACE = timedelta(seconds=5)
_DECISION_WINDOW = timedelta(minutes=5)


class CandleProvider(Protocol):
    def get_candlesticks(
        self,
        symbol: str,
        period: str,
        count: int,
    ) -> list[Any]: ...


@dataclass(frozen=True)
class _Candle:
    timestamp: datetime
    open: float
    close: float


class OpeningMomentumShadowService:
    """Collect one daily cross-sectional observation without placing orders."""

    def __init__(
        self,
        db: Session,
        candle_provider: CandleProvider | None = None,
        *,
        config: OpeningMomentumConfig | None = None,
    ) -> None:
        self.db = db
        self.candle_provider = candle_provider
        self.config = config or OpeningMomentumConfig()

    def tick(
        self,
        *,
        now: datetime | None = None,
    ) -> OpeningMomentumShadowStatusResponse:
        current = _as_utc(now or datetime.now(timezone.utc))
        open_run = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(OpeningMomentumShadowRun.status == "OPEN")
            .order_by(OpeningMomentumShadowRun.session_date.asc())
            .first()
        )
        if open_run is not None:
            self._close_if_due(open_run, current)
            return self.get_status()

        if not settings.opening_momentum_shadow_enabled:
            return self.get_status()
        if not is_trading_hours("US", current):
            return self.get_status()

        session = get_session("US")
        local = session.local(current)
        session_open = datetime.combine(
            local.date(),
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        entry_at = session_open + timedelta(
            minutes=self.config.signal_minutes
        )
        decision_start = entry_at + _BAR_DURATION + _SETTLEMENT_GRACE
        decision_end = decision_start + _DECISION_WINDOW
        if current < decision_start or current > decision_end:
            return self.get_status()

        config_version = self.config.version_hash()
        existing = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(
                OpeningMomentumShadowRun.session_date == local.date(),
                OpeningMomentumShadowRun.config_version == config_version,
            )
            .first()
        )
        if existing is not None:
            return self.get_status()
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )

        symbols, selection_run_id, universe_source = (
            self._selected_universe()
        )
        observations: list[OpeningMomentumObservation] = []
        excluded: dict[str, str] = {}
        signal_at = entry_at - _BAR_DURATION
        expected_signal_bars = {
            session_open + timedelta(minutes=index)
            for index in range(self.config.signal_minutes)
        }
        for symbol in symbols:
            try:
                bars = self.candle_provider.get_candlesticks(
                    symbol,
                    "MIN_1",
                    _CANDLE_COUNT,
                )
                by_timestamp = {
                    bar.timestamp: bar
                    for bar in self._coerce_candles(bars)
                }
                missing_signal_bars = (
                    expected_signal_bars - by_timestamp.keys()
                )
                if missing_signal_bars:
                    excluded[symbol] = (
                        "SIGNAL_BARS_MISSING:"
                        f"{len(missing_signal_bars)}"
                    )
                    continue
                opening_bar = by_timestamp.get(session_open)
                signal_bar = by_timestamp.get(signal_at)
                entry_bar = by_timestamp.get(entry_at)
                if opening_bar is None or signal_bar is None:
                    excluded[symbol] = "SIGNAL_BARS_MISSING"
                    continue
                observations.append(
                    OpeningMomentumObservation(
                        symbol=symbol,
                        session_open=opening_bar.open,
                        signal_close=signal_bar.close,
                        entry_open=(
                            entry_bar.open
                            if entry_bar is not None
                            else None
                        ),
                    )
                )
            except Exception as exc:
                excluded[symbol] = (
                    f"BROKER_ERROR:{type(exc).__name__}"
                )
                logger.warning(
                    "opening momentum candle fetch failed for %s: %s",
                    symbol,
                    exc,
                )

        decision = evaluate_opening_momentum(
            observations,
            self.config,
        )
        data_complete = not excluded
        status = (
            "OPEN"
            if decision.action == "ENTER_LONG" and data_complete
            else "SKIPPED"
        )
        reason = (
            decision.reason
            if data_complete
            else "DATA_INCOMPLETE"
        )
        row = OpeningMomentumShadowRun(
            session_date=local.date(),
            algorithm_version=ALGORITHM_VERSION,
            config_version=config_version,
            status=status,
            reason=reason,
            signal_at=signal_at,
            observed_at=current,
            selection_run_id=selection_run_id,
            universe_source=universe_source,
            universe_size=decision.universe_size,
            universe_json=json.dumps(
                symbols,
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            excluded_symbols_json=json.dumps(
                excluded,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            ranking_json=json.dumps(
                [asdict(item) for item in decision.ranking],
                ensure_ascii=True,
                separators=(",", ":"),
            ),
            candidate_symbol=decision.candidate_symbol,
            market_return_bps=decision.market_return_bps,
            candidate_return_bps=decision.candidate_return_bps,
            excess_return_bps=decision.excess_return_bps,
            entry_at=(
                entry_at
                if status == "OPEN"
                else None
            ),
            entry_price=(
                decision.entry_price if status == "OPEN" else None
            ),
            exit_due_at=(
                entry_at + timedelta(
                    minutes=self.config.holding_minutes
                )
                if status == "OPEN"
                else None
            ),
            estimated_cost_bps=self.config.round_trip_cost_bps,
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
        return self.get_status()

    def get_status(self) -> OpeningMomentumShadowStatusResponse:
        latest = (
            self.db.query(OpeningMomentumShadowRun)
            .order_by(
                OpeningMomentumShadowRun.session_date.desc(),
                OpeningMomentumShadowRun.id.desc(),
            )
            .first()
        )
        if latest is not None and latest.status == "OPEN":
            state = "OPEN"
        elif not settings.opening_momentum_shadow_enabled:
            state = "DISABLED"
        elif latest is None:
            state = "WAITING"
        else:
            state = "COLLECTING"
        return OpeningMomentumShadowStatusResponse(
            config=self._config_response(),
            state=state,
            latest=self._run_response(latest) if latest else None,
            metrics=self._metrics(),
        )

    def list_runs(
        self,
        *,
        limit: int = 100,
    ) -> list[OpeningMomentumShadowRunResponse]:
        rows = (
            self.db.query(OpeningMomentumShadowRun)
            .order_by(
                OpeningMomentumShadowRun.session_date.desc(),
                OpeningMomentumShadowRun.id.desc(),
            )
            .limit(limit)
            .all()
        )
        return [self._run_response(row) for row in rows]

    def _selected_universe(
        self,
    ) -> tuple[list[str], int | None, str]:
        run = (
            self.db.query(UniverseSelectionRun)
            .filter(
                UniverseSelectionRun.status == "COMPLETE",
                UniverseSelectionRun.selected_count
                >= self.config.minimum_universe_size,
            )
            .order_by(
                UniverseSelectionRun.as_of_date.desc(),
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )
        if run is None:
            return [], None, "NONE"
        candidates = (
            self.db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.selected.is_(True),
                UniverseSelectionCandidate.market == "US",
            )
            .order_by(
                UniverseSelectionCandidate.rank.asc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.symbol.asc(),
            )
            .all()
        )
        return (
            list(dict.fromkeys(row.symbol for row in candidates)),
            run.id,
            "UNIVERSE_SELECTION",
        )

    def _close_if_due(
        self,
        row: OpeningMomentumShadowRun,
        current: datetime,
    ) -> None:
        if row.exit_due_at is None or row.candidate_symbol is None:
            return
        exit_due_at = _as_utc(row.exit_due_at)
        if current < exit_due_at + _BAR_DURATION + _SETTLEMENT_GRACE:
            return
        if self.candle_provider is None:
            raise RuntimeError(
                "opening momentum shadow candle provider is unavailable"
            )
        bars = self.candle_provider.get_candlesticks(
            row.candidate_symbol,
            "MIN_1",
            _CANDLE_COUNT,
        )
        exit_bar = {
            bar.timestamp: bar
            for bar in self._coerce_candles(bars)
        }.get(exit_due_at)
        if exit_bar is None:
            history_reader = getattr(
                self.candle_provider,
                "get_history_candlesticks_by_offset",
                None,
            )
            if callable(history_reader):
                historical = history_reader(
                    row.candidate_symbol,
                    "MIN_1",
                    10,
                    exit_due_at - _BAR_DURATION,
                )
                if isinstance(historical, list):
                    exit_bar = {
                        bar.timestamp: bar
                        for bar in self._coerce_candles(historical)
                    }.get(exit_due_at)
        if exit_bar is None:
            logger.warning(
                "opening momentum exit bar unavailable for %s at %s",
                row.candidate_symbol,
                exit_due_at.isoformat(),
            )
            return
        if row.entry_price is None:
            raise ValueError(
                "open opening-momentum run has no entry price"
            )
        gross_return_bps, _ = (
            shadow_round_trip_return_bps(
                entry_price=row.entry_price,
                exit_price=exit_bar.open,
                config=self.config,
            )
        )
        net_return_bps = (
            gross_return_bps - float(row.estimated_cost_bps)
        )
        row.status = "CLOSED"
        row.reason = "FIXED_HOLD_EXIT"
        row.exit_at = exit_due_at
        row.exit_price = exit_bar.open
        row.gross_return_bps = gross_return_bps
        row.net_return_bps = net_return_bps
        self.db.add(row)
        self.db.commit()

    def _config_response(
        self,
    ) -> OpeningMomentumShadowConfigResponse:
        return OpeningMomentumShadowConfigResponse(
            enabled=settings.opening_momentum_shadow_enabled,
            algorithm_version=ALGORITHM_VERSION,
            config_version=self.config.version_hash(),
            signal_minutes=self.config.signal_minutes,
            holding_minutes=self.config.holding_minutes,
            minimum_universe_size=self.config.minimum_universe_size,
            minimum_market_return_bps=(
                self.config.minimum_market_return_bps
            ),
            minimum_candidate_return_bps=(
                self.config.minimum_candidate_return_bps
            ),
            minimum_excess_return_bps=(
                self.config.minimum_excess_return_bps
            ),
            one_side_fee_rate=self.config.one_side_fee_rate,
            one_side_slippage_bps=(
                self.config.one_side_slippage_bps
            ),
            round_trip_cost_bps=self.config.round_trip_cost_bps,
        )

    def _metrics(self) -> OpeningMomentumShadowMetrics:
        rows = (
            self.db.query(OpeningMomentumShadowRun)
            .filter(
                OpeningMomentumShadowRun.config_version
                == self.config.version_hash()
            )
            .order_by(OpeningMomentumShadowRun.session_date.asc())
            .all()
        )
        closed = [row for row in rows if row.status == "CLOSED"]
        net_values = [
            float(row.net_return_bps)
            for row in closed
            if row.net_return_bps is not None
        ]
        wins = sum(value > 0 for value in net_values)
        cumulative = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in net_values:
            cumulative += value
            peak = max(peak, cumulative)
            max_drawdown = max(max_drawdown, peak - cumulative)
        gains = sum(value for value in net_values if value > 0)
        losses = -sum(value for value in net_values if value < 0)
        return OpeningMomentumShadowMetrics(
            observed_sessions=len(rows),
            skipped_sessions=sum(
                row.status == "SKIPPED" for row in rows
            ),
            signals=sum(
                row.status in {"OPEN", "CLOSED"} for row in rows
            ),
            open_trades=sum(row.status == "OPEN" for row in rows),
            closed_trades=len(closed),
            wins=wins,
            win_rate=(wins / len(net_values)) if net_values else 0.0,
            mean_net_return_bps=(
                sum(net_values) / len(net_values)
                if net_values
                else 0.0
            ),
            cumulative_net_return_bps=sum(net_values),
            max_drawdown_bps=max_drawdown,
            profit_factor=(
                gains / losses
                if losses > 0
                else None
            ),
        )

    @staticmethod
    def _coerce_candles(values: list[Any]) -> list[_Candle]:
        by_timestamp: dict[datetime, _Candle] = {}
        for value in values:
            try:
                timestamp = _as_utc(getattr(value, "timestamp"))
                open_price = float(getattr(value, "open"))
                close_price = float(getattr(value, "close"))
            except (AttributeError, TypeError, ValueError):
                continue
            if any(
                not math.isfinite(price) or price <= 0
                for price in (open_price, close_price)
            ):
                continue
            by_timestamp[timestamp] = _Candle(
                timestamp=timestamp,
                open=open_price,
                close=close_price,
            )
        return [
            by_timestamp[timestamp]
            for timestamp in sorted(by_timestamp)
        ]

    @staticmethod
    def _run_response(
        row: OpeningMomentumShadowRun,
    ) -> OpeningMomentumShadowRunResponse:
        universe = _json_list(row.universe_json)
        excluded = _json_dict(row.excluded_symbols_json)
        ranking_raw = _json_value(row.ranking_json, [])
        ranking = (
            [
                OpeningMomentumRankResponse.model_validate(item)
                for item in ranking_raw
                if isinstance(item, dict)
            ]
            if isinstance(ranking_raw, list)
            else []
        )
        return OpeningMomentumShadowRunResponse(
            id=row.id,
            session_date=row.session_date,
            algorithm_version=row.algorithm_version,
            config_version=row.config_version,
            status=cast(
                Literal["SKIPPED", "OPEN", "CLOSED"],
                row.status,
            ),
            reason=row.reason,
            signal_at=_as_utc(row.signal_at),
            observed_at=_as_utc(row.observed_at),
            selection_run_id=row.selection_run_id,
            universe_source=row.universe_source,
            universe_size=row.universe_size,
            universe=universe,
            excluded_symbols=excluded,
            ranking=ranking,
            candidate_symbol=row.candidate_symbol,
            market_return_bps=row.market_return_bps,
            candidate_return_bps=row.candidate_return_bps,
            excess_return_bps=row.excess_return_bps,
            entry_at=_optional_utc(row.entry_at),
            entry_price=row.entry_price,
            exit_due_at=_optional_utc(row.exit_due_at),
            exit_at=_optional_utc(row.exit_at),
            exit_price=row.exit_price,
            gross_return_bps=row.gross_return_bps,
            estimated_cost_bps=row.estimated_cost_bps,
            net_return_bps=row.net_return_bps,
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value is not None else None


def _json_value(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _json_list(raw: str) -> list[str]:
    value = _json_value(raw, [])
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _json_dict(raw: str) -> dict[str, str]:
    value = _json_value(raw, {})
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
    }
