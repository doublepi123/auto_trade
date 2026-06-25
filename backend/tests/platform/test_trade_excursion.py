"""Tests for P218 trade MFE/MAE & holding-period analyzer."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.platform.events import BarEvent, EventSource, FillEvent
from app.platform.trade_excursion import (
    analyze_trades,
    trades_from_fills,
    TradeExcursionInput,
)


def _bars(highs, lows, closes=None, start=None):
    start = start or datetime(2024, 1, 1, 9, 30)
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    out = []
    for i in range(len(highs)):
        out.append(BarEvent(
            timestamp=start + timedelta(minutes=i),
            source=EventSource.MARKET, symbol="X",
            open=closes[i], high=highs[i], low=lows[i],
            close=closes[i], volume=1000,
        ))
    return out


def test_long_trade_mfe_mae_basic():
    # bars t0..t4: highs [101,105,99,103,108], lows [100,102,98,101,106]
    highs = [101, 105, 99, 103, 108]
    lows = [100, 102, 98, 101, 106]
    bars = _bars(highs, lows)
    # trade: BUY entry at t1 @100, exit at t4 @108
    trade = TradeExcursionInput(
        entry_time=bars[1].timestamp, exit_time=bars[4].timestamp,
        side="BUY", entry_price=100.0, exit_price=108.0,
    )
    per, summary = analyze_trades([trade], bars)
    t = per[0]
    # window t1..t4: highs [105,99,103,108] → mfe_price=108, mfe=8
    assert t.mfe_price == 108.0
    assert abs(t.mfe - 8.0) < 1e-9
    # lows [102,98,101,106] → mae_price=98, mae=-2
    assert t.mae_price == 98.0
    assert abs(t.mae - (-2.0)) < 1e-9
    assert abs(t.mfe_pct - 0.08) < 1e-9
    assert abs(t.mae_pct - (-0.02)) < 1e-9
    assert t.holding_bars == 4
    assert abs(t.realized_pnl_pct - 0.08) < 1e-9


def test_short_trade_flips_mfe_mae():
    highs = [101, 105, 99, 103, 108]
    lows = [100, 102, 98, 101, 106]
    bars = _bars(highs, lows)
    trade = TradeExcursionInput(
        entry_time=bars[1].timestamp, exit_time=bars[4].timestamp,
        side="SELL", entry_price=100.0, exit_price=92.0,
    )
    per, summary = analyze_trades([trade], bars)
    t = per[0]
    # short: mfe from min(low)=98 → mfe=(98-100)*-1=2.0
    assert t.mfe_price == 98.0
    assert abs(t.mfe - 2.0) < 1e-9
    # mae from max(high)=108 → mae=(108-100)*-1=-8.0 (worst high reached against the short)
    assert t.mae_price == 108.0
    assert abs(t.mae - (-8.0)) < 1e-9
    assert abs(t.realized_pnl_pct - 0.08) < 1e-9  # (92-100)*-1/100


def test_open_trade_exit_none_uses_last_bar():
    highs = [101, 105, 99, 103, 108]
    lows = [100, 102, 98, 101, 106]
    bars = _bars(highs, lows)
    trade = TradeExcursionInput(
        entry_time=bars[1].timestamp, exit_time=None,
        side="BUY", entry_price=100.0, exit_price=None,
    )
    per, summary = analyze_trades([trade], bars)
    t = per[0]
    assert t.realized_pnl_pct is None
    assert summary.num_open == 1


def test_empty_trades_returns_zero_summary():
    bars = _bars([101, 102], [100, 101])
    per, summary = analyze_trades([], bars)
    assert summary.num_trades == 0
    assert per == []


def test_empty_bars_zero_excursion():
    trade = TradeExcursionInput(
        entry_time=datetime(2024, 1, 1, 9, 30), exit_time=datetime(2024, 1, 1, 9, 35),
        side="BUY", entry_price=100.0, exit_price=101.0,
    )
    per, summary = analyze_trades([trade], [])
    t = per[0]
    assert t.holding_bars == 0
    assert t.mfe == 0.0 and t.mae == 0.0


def test_holding_bars_percentiles():
    bars = _bars([101] * 30, [100] * 30)
    trades = []
    for i in range(5):
        trades.append(TradeExcursionInput(
            entry_time=bars[i].timestamp,
            exit_time=bars[i + i + 1].timestamp if i + i + 1 < len(bars) else bars[-1].timestamp,
            side="BUY", entry_price=100.0, exit_price=100.0,
        ))
    per, summary = analyze_trades(trades, bars)
    assert summary.num_trades == 5
    assert summary.median_holding_bars >= 1


def test_entry_exit_timing_rank():
    bars = _bars([101] * 10, [100] * 10)
    trades = [
        TradeExcursionInput(entry_time=bars[0].timestamp, exit_time=bars[5].timestamp, side="BUY", entry_price=100.0, exit_price=100.0),
        TradeExcursionInput(entry_time=bars[1].timestamp, exit_time=bars[6].timestamp, side="BUY", entry_price=100.0, exit_price=100.0),
        TradeExcursionInput(entry_time=bars[2].timestamp, exit_time=bars[7].timestamp, side="BUY", entry_price=100.0, exit_price=100.0),
    ]
    per, summary = analyze_trades(trades, bars)
    # earliest entry rank 0.0, latest 1.0, middle 0.5
    ranks = [t.entry_timing_rank for t in per]
    assert min(ranks) == 0.0
    assert max(ranks) == 1.0
    assert any(abs(r - 0.5) < 1e-9 for r in ranks)


def test_trades_from_fills_fifo_pairs():
    t0 = datetime(2024, 1, 1, 9, 30)
    t1 = datetime(2024, 1, 1, 9, 31)
    t2 = datetime(2024, 1, 1, 9, 32)
    t3 = datetime(2024, 1, 1, 9, 33)
    fills = [
        FillEvent(timestamp=t0, source=EventSource.BROKER, symbol="A", broker_order_id="1", side="BUY", quantity=10, price=Decimal("100")),
        FillEvent(timestamp=t1, source=EventSource.BROKER, symbol="A", broker_order_id="2", side="BUY", quantity=10, price=Decimal("102")),
        FillEvent(timestamp=t2, source=EventSource.BROKER, symbol="A", broker_order_id="3", side="SELL", quantity=10, price=Decimal("105")),
        FillEvent(timestamp=t3, source=EventSource.BROKER, symbol="A", broker_order_id="4", side="SELL", quantity=10, price=Decimal("99")),
    ]
    trades = trades_from_fills(fills)
    assert len(trades) == 2
    assert trades[0].entry_price == 100.0 and trades[0].exit_price == 105.0
    assert trades[1].entry_price == 102.0 and trades[1].exit_price == 99.0


def test_trades_from_fills_unmatched_open():
    t0 = datetime(2024, 1, 1, 9, 30)
    fills = [
        FillEvent(timestamp=t0, source=EventSource.BROKER, symbol="A", broker_order_id="1", side="BUY", quantity=10, price=Decimal("100")),
    ]
    trades = trades_from_fills(fills)
    assert len(trades) == 1
    assert trades[0].exit_time is None


def test_mfe_mae_ratio_guard():
    bars = _bars([101, 102], [100, 101])
    trade = TradeExcursionInput(
        entry_time=bars[0].timestamp, exit_time=bars[1].timestamp,
        side="BUY", entry_price=100.0, exit_price=100.0,
    )
    per, summary = analyze_trades([trade], bars)
    # mae_pct could be 0 → ratio None (guard against ZeroDivision)
    if summary.median_mae_pct == 0.0:
        assert summary.mfe_mae_ratio is None


def test_determinism():
    highs = [101, 105, 99, 103, 108]
    lows = [100, 102, 98, 101, 106]
    bars = _bars(highs, lows)
    trade = TradeExcursionInput(
        entry_time=bars[1].timestamp, exit_time=bars[4].timestamp,
        side="BUY", entry_price=100.0, exit_price=108.0,
    )
    a = analyze_trades([trade], bars)
    b = analyze_trades([trade], bars)
    assert a == b


# used in trades_from_fills test
from decimal import Decimal  # noqa: E402