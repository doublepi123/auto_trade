from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pytest import approx

from app.services.daily_pnl_service import ClosedRoundTrip
from app.services.symbol_attribution_service import compute_symbol_attribution


_BASE_DT = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def _trip(symbol: str, net: float, *, day_offset: int = 0) -> ClosedRoundTrip:
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


class TestComputeSymbolAttribution:
    def test_empty(self) -> None:
        r = compute_symbol_attribution([])
        assert r.rows == []
        assert r.total_realized_pnl == 0.0

    def test_single_symbol_grouped(self) -> None:
        r = compute_symbol_attribution([_trip("AAPL.US", 100), _trip("AAPL.US", -40)])
        assert len(r.rows) == 1
        row = r.rows[0]
        assert row.symbol == "AAPL.US"
        assert row.realized_pnl == approx(60.0)
        assert row.trade_count == 2
        assert row.win_count == 1
        assert row.win_rate == approx(50.0)
        assert row.largest_win == approx(100.0)
        assert row.largest_loss == approx(-40.0)
        assert r.total_realized_pnl == approx(60.0)

    def test_multiple_symbols_sorted_by_abs_pnl(self) -> None:
        # TSLA +200, AAPL +50, MSFT -120 -> |200|, |120|, |50|
        trips = [_trip("TSLA.US", 200), _trip("AAPL.US", 50), _trip("MSFT.US", -120)]
        r = compute_symbol_attribution(trips)
        symbols = [row.symbol for row in r.rows]
        assert symbols == ["TSLA.US", "MSFT.US", "AAPL.US"]
        assert r.total_realized_pnl == approx(130.0)

    def test_contribution_share_signed(self) -> None:
        # total = 100; AAPL +60 -> share 0.6; MSFT +40 -> share 0.4
        trips = [_trip("AAPL.US", 60), _trip("MSFT.US", 40)]
        r = compute_symbol_attribution(trips)
        shares = {row.symbol: row.contribution_share for row in r.rows}
        assert shares["AAPL.US"] == approx(0.6)
        assert shares["MSFT.US"] == approx(0.4)

    def test_share_when_total_zero_is_zero(self) -> None:
        # AAPL +50, MSFT -50 -> total 0; shares fall back to 0 (avoid div by zero)
        trips = [_trip("AAPL.US", 50), _trip("MSFT.US", -50)]
        r = compute_symbol_attribution(trips)
        for row in r.rows:
            assert row.contribution_share == 0.0

    def test_single_sign_symbol_reports_none_for_absent_side(self) -> None:
        # loss-only symbol -> largest_win None, largest_loss = most negative
        r = compute_symbol_attribution([_trip("MSFT.US", -50), _trip("MSFT.US", -30)])
        row = next(row for row in r.rows if row.symbol == "MSFT.US")
        assert row.largest_win is None
        assert row.largest_loss == approx(-50.0)
        # win-only symbol -> largest_loss None, largest_win = largest
        r2 = compute_symbol_attribution([_trip("AAPL.US", 100), _trip("AAPL.US", 40)])
        row2 = next(row for row in r2.rows if row.symbol == "AAPL.US")
        assert row2.largest_loss is None
        assert row2.largest_win == approx(100.0)

    def test_near_zero_total_shares_are_zero(self) -> None:
        # wins and losses cancel to a tiny float residue (not exactly 0);
        # abs(total) <= 1e-9 must also zero the shares to avoid blow-up.
        trips = [_trip("AAPL.US", 50.0), _trip("AAPL.US", -49.9999999999)]
        r = compute_symbol_attribution(trips)
        for row in r.rows:
            assert row.contribution_share == 0.0
