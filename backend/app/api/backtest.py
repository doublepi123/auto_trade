from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.backtest import BacktestBar, BacktestEngine, BacktestEngineParams, parse_backtest_csv
from app.schemas import (
    BacktestEquityPoint,
    BacktestFeeSensitivityPoint,
    BacktestMetrics,
    BacktestResult,
    BacktestRunRequest,
    BacktestSkippedSignal,
    BacktestTradeLog,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


@router.post("/run", response_model=BacktestResult)
def run_backtest(payload: BacktestRunRequest) -> BacktestResult:
    try:
        bars = _load_bars(payload)
        params = BacktestEngineParams(
            symbol=payload.params.symbol,
            buy_low=payload.params.buy_low,
            sell_high=payload.params.sell_high,
            short_selling=payload.params.short_selling,
            min_profit_amount=payload.params.min_profit_amount,
            max_daily_loss=payload.params.max_daily_loss,
            max_consecutive_losses=payload.params.max_consecutive_losses,
            quantity=payload.params.quantity,
            initial_cash=payload.params.initial_cash,
            fee_rate=payload.params.fee_rate,
            fixed_fee=payload.params.fixed_fee,
            slippage_pct=payload.params.slippage_pct,
            stop_loss_pct=payload.params.stop_loss_pct,
        )
        result = BacktestEngine(params).run(bars)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return BacktestResult(
        params=payload.params,
        metrics=BacktestMetrics(
            initial_cash=result.metrics.initial_cash,
            final_equity=result.metrics.final_equity,
            total_pnl=result.metrics.total_pnl,
            total_return_pct=result.metrics.total_return_pct,
            max_drawdown_pct=result.metrics.max_drawdown_pct,
            trade_count=result.metrics.trade_count,
            closed_trade_count=result.metrics.closed_trade_count,
            winning_trades=result.metrics.winning_trades,
            losing_trades=result.metrics.losing_trades,
            win_rate=result.metrics.win_rate,
            avg_holding_minutes=result.metrics.avg_holding_minutes,
            fees_paid=result.metrics.fees_paid,
            skipped_signals=result.metrics.skipped_signals,
            final_state=result.metrics.final_state,
        ),
        equity_curve=[
            BacktestEquityPoint(
                timestamp=point.timestamp,
                close=point.close,
                equity=point.equity,
                realized_pnl=point.realized_pnl,
                unrealized_pnl=point.unrealized_pnl,
                drawdown_pct=point.drawdown_pct,
                position=point.position,
            )
            for point in result.equity_curve
        ],
        trades=[
            BacktestTradeLog(
                timestamp=trade.timestamp,
                action=trade.action,
                price=trade.price,
                quantity=trade.quantity,
                fee=trade.fee,
                pnl=trade.pnl,
                state_after=trade.state_after,
                reason=trade.reason,
                holding_minutes=trade.holding_minutes,
            )
            for trade in result.trades
        ],
        skipped_signals=[
            BacktestSkippedSignal(
                timestamp=signal.timestamp,
                action=signal.action,
                price=signal.price,
                reason=signal.reason,
                state=signal.state,
                category=signal.category,
            )
            for signal in result.skipped_signals
        ],
        fee_sensitivity=[
            BacktestFeeSensitivityPoint(
                fee_rate=point.fee_rate,
                total_pnl=point.total_pnl,
                total_return_pct=point.total_return_pct,
                max_drawdown_pct=point.max_drawdown_pct,
            )
            for point in result.fee_sensitivity
        ],
    )


def _load_bars(payload: BacktestRunRequest) -> list[BacktestBar]:
    if payload.csv_text and payload.csv_text.strip():
        return parse_backtest_csv(payload.csv_text)
    return [
        BacktestBar(
            timestamp=point.timestamp,
            open=point.open,
            high=point.high,
            low=point.low,
            close=point.close,
            volume=point.volume,
        )
        for point in payload.price_points
    ]
