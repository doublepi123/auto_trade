"""Per-trade performance statistics over closed round trips.

Pure aggregation over ``ClosedRoundTrip`` rows produced by
``DailyPnlService.pair_round_trips`` — sequential run analysis (win/loss
streaks), expectancy, profit factor and payoff ratio that the aggregate
``/api/metrics/summary`` endpoint structurally cannot provide.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.daily_pnl_service import ClosedRoundTrip


@dataclass(frozen=True)
class TradeStatsResult:
    total_trades: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: float
    total_gross_pnl: float
    total_net_pnl: float
    avg_win: float | None
    avg_loss: float | None
    expectancy: float
    profit_factor: float | None
    payoff_ratio: float | None
    largest_win: float | None
    largest_loss: float | None
    current_streak_type: str  # "win" | "loss" | "none"
    current_streak_count: int
    max_win_streak: int
    max_loss_streak: int
    avg_hold_seconds: float | None
    total_fees: float = 0.0
    actual_fee_coverage_pct: float = 0.0
    avg_slippage_bps: float | None = None
    avg_ack_latency_ms: float | None = None


def compute_trade_stats(trips: list[ClosedRoundTrip]) -> TradeStatsResult:
    # Walk trades in chronological exit order so streaks reflect the real
    # sequence a trader experienced.
    ordered = sorted(trips, key=lambda t: t.exit_at)
    total = len(ordered)
    if total == 0:
        return TradeStatsResult(
            total_trades=0,
            win_count=0,
            loss_count=0,
            breakeven_count=0,
            win_rate=0.0,
            total_gross_pnl=0.0,
            total_net_pnl=0.0,
            avg_win=None,
            avg_loss=None,
            expectancy=0.0,
            profit_factor=None,
            payoff_ratio=None,
            largest_win=None,
            largest_loss=None,
            current_streak_type="none",
            current_streak_count=0,
            max_win_streak=0,
            max_loss_streak=0,
            avg_hold_seconds=None,
            total_fees=0.0,
            actual_fee_coverage_pct=0.0,
            avg_slippage_bps=None,
            avg_ack_latency_ms=None,
        )

    # Win/loss is classified on NET PnL (true take-home), so an estimated fee
    # large enough to flip a near-breakeven trade counts as a loss. Breakeven
    # (exactly zero) is neither.
    wins = [t for t in ordered if t.net_pnl > 0]
    losses = [t for t in ordered if t.net_pnl < 0]
    breakeven = total - len(wins) - len(losses)

    net_win = sum(t.net_pnl for t in wins)
    net_loss = abs(sum(t.net_pnl for t in losses))
    total_gross = sum(t.gross_pnl for t in ordered)
    total_net = sum(t.net_pnl for t in ordered)

    avg_win = (net_win / len(wins)) if wins else None
    avg_loss = (net_loss / len(losses)) if losses else None

    profit_factor = (net_win / net_loss) if net_loss > 0 else None
    payoff_ratio = (avg_win / avg_loss) if (avg_win is not None and avg_loss not in (None, 0.0)) else None

    largest_win = max((t.net_pnl for t in wins), default=None)
    largest_loss = min((t.net_pnl for t in losses), default=None)

    # Streaks over the net-PnL sign sequence.
    current_type = "none"
    current_count = 0
    max_win = 0
    max_loss = 0
    run_type = "none"
    run_count = 0
    for t in ordered:
        sign = "win" if t.net_pnl > 0 else ("loss" if t.net_pnl < 0 else "none")
        if sign == "none":
            # A breakeven trade breaks the current run without starting a new one.
            run_type = "none"
            run_count = 0
        elif sign == run_type:
            run_count += 1
        else:
            run_type = sign
            run_count = 1
        if sign == "win":
            max_win = max(max_win, run_count)
        elif sign == "loss":
            max_loss = max(max_loss, run_count)
        # current_* must reflect the run AFTER this trade — including a run
        # broken by a breakeven (none/0), otherwise a trailing breakeven would
        # report a stale active streak.
        current_type = run_type
        current_count = run_count

    hold_values = [t.holding_seconds for t in ordered if t.holding_seconds and t.holding_seconds > 0]
    avg_hold = (sum(hold_values) / len(hold_values)) if hold_values else None
    slippage_values = [
        t.slippage_bps for t in ordered if t.slippage_bps is not None
    ]
    ack_latency_values = [
        t.ack_latency_ms for t in ordered if t.ack_latency_ms is not None
    ]
    actual_fee_count = len([t for t in ordered if t.fee_source == "ACTUAL"])

    return TradeStatsResult(
        total_trades=total,
        win_count=len(wins),
        loss_count=len(losses),
        breakeven_count=breakeven,
        win_rate=round((len(wins) / total) * 100.0, 4),
        total_gross_pnl=round(total_gross, 2),
        total_net_pnl=round(total_net, 2),
        avg_win=round(avg_win, 2) if avg_win is not None else None,
        avg_loss=round(avg_loss, 2) if avg_loss is not None else None,
        expectancy=round(total_net / total, 2),
        profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
        payoff_ratio=round(payoff_ratio, 4) if payoff_ratio is not None else None,
        largest_win=round(largest_win, 2) if largest_win is not None else None,
        largest_loss=round(largest_loss, 2) if largest_loss is not None else None,
        current_streak_type=current_type,
        current_streak_count=current_count,
        max_win_streak=max_win,
        max_loss_streak=max_loss,
        avg_hold_seconds=round(avg_hold, 2) if avg_hold is not None else None,
        total_fees=round(sum(t.est_fees for t in ordered), 2),
        actual_fee_coverage_pct=round(actual_fee_count / total * 100, 2),
        avg_slippage_bps=(
            round(sum(slippage_values) / len(slippage_values), 4)
            if slippage_values
            else None
        ),
        avg_ack_latency_ms=(
            round(sum(ack_latency_values) / len(ack_latency_values), 2)
            if ack_latency_values
            else None
        ),
    )
