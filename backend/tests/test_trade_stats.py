from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest import approx

from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.trade_stats_service import compute_trade_stats


_BASE_DT = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _trip(symbol: str, net: float, *, day_offset: int = 0, hold_seconds: float = 3600.0) -> ClosedRoundTrip:
    exit_at = _BASE_DT + timedelta(days=day_offset)
    return ClosedRoundTrip(
        symbol=symbol,
        side="long",
        entry_order_id=1,
        exit_order_id=2,
        entry_at=exit_at - timedelta(seconds=hold_seconds),
        exit_at=exit_at,
        entry_price=10.0,
        exit_price=10.0,
        quantity=10.0,
        gross_pnl=net,
        est_fees=0.0,
        net_pnl=net,
        holding_seconds=hold_seconds,
    )


class TestComputeTradeStats:
    def test_empty(self) -> None:
        s = compute_trade_stats([])
        assert s.total_trades == 0
        assert s.win_count == 0
        assert s.loss_count == 0
        assert s.win_rate == 0.0
        assert s.profit_factor is None
        assert s.payoff_ratio is None
        assert s.current_streak_type == "none"
        assert s.current_streak_count == 0
        assert s.max_win_streak == 0
        assert s.max_loss_streak == 0
        assert s.avg_hold_seconds is None

    def test_all_wins(self) -> None:
        trips = [_trip("A", 100, day_offset=0), _trip("A", 50, day_offset=1)]
        s = compute_trade_stats(trips)
        assert s.total_trades == 2
        assert s.win_count == 2
        assert s.loss_count == 0
        assert s.win_rate == approx(100.0)
        assert s.total_net_pnl == approx(150.0)
        assert s.expectancy == approx(75.0)
        assert s.profit_factor is None  # no losses
        assert s.current_streak_type == "win"
        assert s.current_streak_count == 2
        assert s.max_win_streak == 2

    def test_profit_factor_payoff_and_expectancy(self) -> None:
        # wins: +200, +100 ; losses: -80, -20
        trips = [
            _trip("A", 200, day_offset=0),
            _trip("A", -80, day_offset=1),
            _trip("A", 100, day_offset=2),
            _trip("A", -20, day_offset=3),
        ]
        s = compute_trade_stats(trips)
        assert s.win_count == 2
        assert s.loss_count == 2
        assert s.win_rate == approx(50.0)
        assert s.total_net_pnl == approx(200.0)
        assert s.expectancy == approx(50.0)  # 200/4
        gross_win = 300.0
        gross_loss = 100.0
        assert s.profit_factor == approx(gross_win / gross_loss)
        avg_win = 150.0
        avg_loss = 50.0
        assert s.payoff_ratio == approx(avg_win / avg_loss)
        assert s.largest_win == approx(200.0)
        assert s.largest_loss == approx(-80.0)

    def test_streaks_current_and_max(self) -> None:
        # sequence by exit day: W W L L L W
        trips = [
            _trip("A", 10, day_offset=0),
            _trip("A", 10, day_offset=1),
            _trip("A", -5, day_offset=2),
            _trip("A", -5, day_offset=3),
            _trip("A", -5, day_offset=4),
            _trip("A", 8, day_offset=5),
        ]
        s = compute_trade_stats(trips)
        assert s.max_win_streak == 2
        assert s.max_loss_streak == 3
        assert s.current_streak_type == "win"
        assert s.current_streak_count == 1

    def test_current_streak_is_loss(self) -> None:
        trips = [_trip("A", 10, day_offset=0), _trip("A", -5, day_offset=1), _trip("A", -5, day_offset=2)]
        s = compute_trade_stats(trips)
        assert s.current_streak_type == "loss"
        assert s.current_streak_count == 2
        assert s.max_loss_streak == 2

    def test_breakeven_neither_win_nor_loss(self) -> None:
        trips = [_trip("A", 0.0, day_offset=0), _trip("A", 10, day_offset=1)]
        s = compute_trade_stats(trips)
        assert s.total_trades == 2
        assert s.win_count == 1
        assert s.loss_count == 0
        assert s.breakeven_count == 1

    def test_trailing_breakeven_resets_current_streak(self) -> None:
        # W, W, breakeven — the breakeven breaks the active streak.
        trips = [_trip("A", 10, day_offset=0), _trip("A", 10, day_offset=1), _trip("A", 0.0, day_offset=2)]
        s = compute_trade_stats(trips)
        assert s.current_streak_type == "none"
        assert s.current_streak_count == 0
        assert s.max_win_streak == 2

    def test_avg_hold_seconds(self) -> None:
        trips = [_trip("A", 10, hold_seconds=3600), _trip("A", 10, hold_seconds=7200)]
        s = compute_trade_stats(trips)
        assert s.avg_hold_seconds == approx(5400.0)
