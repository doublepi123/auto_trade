from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest import approx

from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.trade_analytics_service import (
    compute_hold_duration_buckets,
    compute_monthly_summary,
    compute_pnl_distribution,
    compute_trade_calendar,
    compute_weekday_attribution,
)


_BASE_DT = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)  # Monday


def _trip(
    symbol: str,
    net: float,
    *,
    day_offset: int = 0,
    hold_seconds: float = 3600.0,
    gross: float | None = None,
) -> ClosedRoundTrip:
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
        gross_pnl=net if gross is None else gross,
        est_fees=0.0,
        net_pnl=net,
        holding_seconds=hold_seconds,
    )


class TestTradeAnalyticsService:
    def test_calendar_buckets_by_exit_date(self) -> None:
        rows = compute_trade_calendar([
            _trip("AAPL.US", 100, day_offset=0),
            _trip("MSFT.US", -40, day_offset=0),
            _trip("AAPL.US", 30, day_offset=1),
        ])

        assert [r.date for r in rows] == ["2026-01-05", "2026-01-06"]
        assert rows[0].trade_count == 2
        assert rows[0].net_pnl == approx(60.0)
        assert rows[0].win_count == 1
        assert rows[0].loss_count == 1
        assert rows[0].symbols == ["AAPL.US", "MSFT.US"]
        assert rows[1].net_pnl == approx(30.0)

    def test_hold_duration_buckets_are_ordered_and_ignore_non_positive_holds(self) -> None:
        rows = compute_hold_duration_buckets([
            _trip("AAPL.US", 10, hold_seconds=120),
            _trip("AAPL.US", -5, hold_seconds=3600),
            _trip("AAPL.US", 20, hold_seconds=30 * 3600),
            _trip("AAPL.US", 99, hold_seconds=0),
        ])

        assert [r.bucket for r in rows] == ["<5m", "5m-1h", "1h-1d", "1d-1w", ">=1w"]
        assert [r.trade_count for r in rows] == [1, 0, 1, 1, 0]
        assert rows[0].avg_net_pnl == approx(10.0)
        assert rows[2].avg_net_pnl == approx(-5.0)
        assert rows[3].win_rate == approx(100.0)

    def test_pnl_distribution_groups_losses_breakeven_and_wins(self) -> None:
        rows = compute_pnl_distribution([
            _trip("AAPL.US", -250),
            _trip("AAPL.US", -50),
            _trip("AAPL.US", 0),
            _trip("AAPL.US", 75),
            _trip("AAPL.US", 420),
        ])

        by_bucket = {r.bucket: r for r in rows}
        assert by_bucket["<=-200"].trade_count == 1
        assert by_bucket["-200--50"].trade_count == 1
        assert by_bucket["breakeven"].trade_count == 1
        assert by_bucket["0-200"].trade_count == 1
        assert by_bucket[">=200"].trade_count == 1
        assert by_bucket[">=200"].net_pnl == approx(420.0)

    def test_monthly_summary_tracks_cumulative_pnl_and_drawdown(self) -> None:
        rows = compute_monthly_summary([
            _trip("AAPL.US", 100, day_offset=0),
            _trip("AAPL.US", -160, day_offset=20),
            _trip("AAPL.US", 70, day_offset=35),
        ])

        assert [r.month for r in rows] == ["2026-01", "2026-02"]
        assert rows[0].net_pnl == approx(-60.0)
        assert rows[0].cumulative_pnl == approx(-60.0)
        assert rows[0].drawdown == approx(60.0)
        assert rows[1].net_pnl == approx(70.0)
        assert rows[1].cumulative_pnl == approx(10.0)
        assert rows[1].drawdown == approx(0.0)

    def test_weekday_attribution_orders_monday_to_friday(self) -> None:
        rows = compute_weekday_attribution([
            _trip("AAPL.US", 100, day_offset=0),  # Monday
            _trip("AAPL.US", -30, day_offset=2),  # Wednesday
            _trip("AAPL.US", 10, day_offset=6),   # Sunday
        ])

        assert [r.weekday for r in rows] == [0, 2, 6]
        assert [r.label for r in rows] == ["Mon", "Wed", "Sun"]
        assert rows[0].win_rate == approx(100.0)
        assert rows[1].loss_count == 1
        assert rows[2].net_pnl == approx(10.0)
