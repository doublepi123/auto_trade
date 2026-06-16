from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest import approx

from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.equity_curve_service import compute_equity_curve


_BASE_DT = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _trip(net: float, *, day_offset: int = 0, symbol: str = "AAPL.US") -> ClosedRoundTrip:
    exit_at = _BASE_DT + timedelta(days=day_offset)
    return ClosedRoundTrip(
        symbol=symbol,
        side="long",
        entry_order_id=1,
        exit_order_id=2,
        entry_at=exit_at - timedelta(hours=1),
        exit_at=exit_at,
        entry_price=10.0,
        exit_price=10.0,
        quantity=10.0,
        gross_pnl=net,
        est_fees=0.0,
        net_pnl=net,
        holding_seconds=3600.0,
    )


class TestComputeEquityCurve:
    def test_empty(self) -> None:
        r = compute_equity_curve([])
        assert r.points == []
        assert r.total_realized_pnl == 0.0
        assert r.max_drawdown == 0.0

    def test_single_trade(self) -> None:
        r = compute_equity_curve([_trip(100)])
        assert len(r.points) == 1
        p = r.points[0]
        assert p.realized_pnl == approx(100.0)
        assert p.cumulative_pnl == approx(100.0)
        assert p.drawdown == approx(0.0)
        assert p.trade_count == 1
        assert r.total_realized_pnl == approx(100.0)
        assert r.max_drawdown == approx(0.0)

    def test_multiple_days_cumulative_and_drawdown(self) -> None:
        # days: +100, -50, -30  -> cumulative 100, 50, 20 ; peak 100
        r = compute_equity_curve([_trip(100, day_offset=0), _trip(-50, day_offset=1), _trip(-30, day_offset=2)])
        assert [p.cumulative_pnl for p in r.points] == [approx(100.0), approx(50.0), approx(20.0)]
        assert [p.drawdown for p in r.points] == [approx(0.0), approx(50.0), approx(80.0)]
        assert r.max_drawdown == approx(80.0)
        assert r.total_realized_pnl == approx(20.0)

    def test_same_day_trades_bucketed(self) -> None:
        r = compute_equity_curve([_trip(100, day_offset=0), _trip(40, day_offset=0)])
        assert len(r.points) == 1
        assert r.points[0].realized_pnl == approx(140.0)
        assert r.points[0].cumulative_pnl == approx(140.0)
        assert r.points[0].trade_count == 2

    def test_all_losses_drawdown_from_zero_peak(self) -> None:
        # peak starts at 0; cumulative -20, -50 -> drawdown 20, 50
        r = compute_equity_curve([_trip(-20, day_offset=0), _trip(-30, day_offset=1)])
        assert [p.cumulative_pnl for p in r.points] == [approx(-20.0), approx(-50.0)]
        assert [p.drawdown for p in r.points] == [approx(20.0), approx(50.0)]
        assert r.max_drawdown == approx(50.0)

    def test_points_ordered_ascending_by_date(self) -> None:
        r = compute_equity_curve([_trip(10, day_offset=2), _trip(10, day_offset=0), _trip(10, day_offset=1)])
        dates = [p.date for p in r.points]
        assert dates == sorted(dates)
