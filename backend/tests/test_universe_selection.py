from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain.universe_selection.catalog import IndexCandidate
from app.domain.universe_selection.selector import (
    CandidateInput,
    UniverseSelectionConfig,
    completed_daily_bars,
    latest_closed_session_date,
    liquidity_spread_proxy_bps,
    select_candidates,
)


@dataclass(frozen=True)
class _Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


def _bars(
    *,
    start_price: float = 100.0,
    volume: float = 10_000_000,
    daily_move: float = 0.018,
) -> list[_Bar]:
    result: list[_Bar] = []
    price = start_price
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for index in range(30):
        direction = 1 if index % 2 == 0 else -1
        next_price = price * (1 + direction * daily_move)
        high = max(price, next_price) * 1.008
        low = min(price, next_price) * 0.992
        result.append(
            _Bar(
                timestamp=start + timedelta(days=index),
                open=price,
                high=high,
                low=low,
                close=next_price,
                volume=volume,
            )
        )
        price = next_price
    return result


def _input(
    symbol: str,
    *,
    sector: str,
    volume: float = 10_000_000,
    bid: float = 99.98,
    ask: float = 100.02,
) -> CandidateInput:
    return CandidateInput(
        candidate=IndexCandidate(
            symbol=symbol,
            alias=symbol,
            sector=sector,
            memberships=("NASDAQ_100",),
        ),
        completed_daily_bars=_bars(volume=volume),
        bid=bid,
        ask=ask,
    )


def _rotation_bars(
    *,
    drift: float,
    recent_move: float | None = None,
    volume: float = 20_000_000,
) -> list[_Bar]:
    result: list[_Bar] = []
    price = 100.0
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index in range(270):
        noise = 0.012 if index % 2 == 0 else -0.012
        move = drift + noise
        if recent_move is not None and index >= 249:
            move = recent_move
        close = price * (1 + move)
        result.append(
            _Bar(
                timestamp=start + timedelta(days=index),
                open=price,
                high=max(price, close) * 1.008,
                low=min(price, close) * 0.992,
                close=close,
                volume=volume,
            )
        )
        price = close
    return result


def _rotation_input(
    symbol: str,
    *,
    sector: str,
    drift: float,
    recent_move: float | None = None,
) -> CandidateInput:
    return CandidateInput(
        candidate=IndexCandidate(
            symbol=symbol,
            alias=symbol,
            sector=sector,
            memberships=("NASDAQ_100",),
        ),
        completed_daily_bars=_rotation_bars(
            drift=drift,
            recent_move=recent_move,
        ),
        bid=99.98,
        ask=100.02,
    )


def _config(**overrides: object) -> UniverseSelectionConfig:
    values: dict[str, object] = {
        "max_selected": 3,
        "max_per_sector": 2,
        "min_avg_dollar_volume": 100_000_000,
        "max_relative_spread_bps": 20,
    }
    values.update(overrides)
    return UniverseSelectionConfig(**values)  # type: ignore[arg-type]


def test_select_candidates_prefers_liquid_tight_spread_names() -> None:
    rows = select_candidates(
        [
            _input("LIQUID.US", sector="Software", volume=25_000_000),
            _input("NORMAL.US", sector="Retail", volume=8_000_000),
            _input(
                "WIDE.US",
                sector="Industrials",
                volume=30_000_000,
                bid=99.5,
                ask=100.5,
            ),
        ],
        _config(max_selected=2),
    )

    by_symbol = {row.candidate.symbol: row for row in rows}
    assert by_symbol["LIQUID.US"].selected is True
    assert by_symbol["LIQUID.US"].rank == 1
    assert by_symbol["NORMAL.US"].selected is True
    assert by_symbol["WIDE.US"].selected is False
    assert "SPREAD_ABOVE_MAXIMUM" in by_symbol["WIDE.US"].exclusion_reasons


def test_select_candidates_enforces_sector_diversification() -> None:
    rows = select_candidates(
        [
            _input("CHIP1.US", sector="Semiconductors", volume=30_000_000),
            _input("CHIP2.US", sector="Semiconductors", volume=20_000_000),
            _input("BANK.US", sector="Financials", volume=10_000_000),
        ],
        _config(max_selected=2, max_per_sector=1),
    )

    selected = [row for row in rows if row.selected]
    assert len(selected) == 2
    assert {row.candidate.sector for row in selected} == {
        "Semiconductors",
        "Financials",
    }
    rejected_chip = next(
        row
        for row in rows
        if row.candidate.sector == "Semiconductors" and not row.selected
    )
    assert rejected_chip.exclusion_reasons == ("SECTOR_CAP",)


def test_select_candidates_caps_related_technology_industries_together() -> None:
    rows = select_candidates(
        [
            _input("CHIP.US", sector="Semiconductors", volume=30_000_000),
            _input("SOFT.US", sector="Software", volume=20_000_000),
            _input("BANK.US", sector="Financials", volume=10_000_000),
        ],
        _config(max_selected=2, max_per_sector=1),
    )

    selected = {
        row.candidate.symbol
        for row in rows
        if row.selected
    }
    assert selected == {"CHIP.US", "BANK.US"}
    software = next(
        row for row in rows if row.candidate.symbol == "SOFT.US"
    )
    assert software.exclusion_reasons == ("SECTOR_CAP",)


def test_select_candidates_records_data_quality_failures() -> None:
    candidate = IndexCandidate(
        symbol="BROKEN.US",
        alias="Broken",
        sector="Software",
        memberships=("NASDAQ_100",),
    )
    rows = select_candidates(
        [
            CandidateInput(
                candidate=candidate,
                completed_daily_bars=_bars()[:10],
                bid=None,
                ask=None,
            )
        ],
        _config(),
    )

    assert rows[0].evaluable is False
    assert rows[0].selected is False
    assert rows[0].metrics.price is None
    assert rows[0].exclusion_reasons == ("DATA_INSUFFICIENT_DAILY_BARS",)


def test_select_candidates_exposes_cost_aware_metrics() -> None:
    row = select_candidates([_input("AAPL.US", sector="Technology")], _config())[0]

    assert row.selected is True
    assert row.metrics.avg_dollar_volume is not None
    assert row.metrics.avg_dollar_volume > 100_000_000
    assert row.metrics.relative_spread_bps is not None
    assert row.metrics.opportunity_to_cost_ratio is not None
    assert row.metrics.opportunity_to_cost_ratio > 1


def test_rotation_shadow_selects_strongest_diversified_momentum() -> None:
    rows = select_candidates(
        [
            _rotation_input(
                "CHIP.US",
                sector="Semiconductors",
                drift=0.0030,
            ),
            _rotation_input(
                "SOFT.US",
                sector="Software",
                drift=0.0025,
            ),
            _rotation_input(
                "BANK.US",
                sector="Financials",
                drift=0.0020,
            ),
        ],
        _config(
            max_per_sector=1,
            rotation_max_selected=2,
            rotation_max_per_risk_group=1,
        ),
    )

    by_symbol = {row.candidate.symbol: row for row in rows}
    assert by_symbol["CHIP.US"].rotation.selected is True
    assert by_symbol["CHIP.US"].rotation.rank == 1
    assert by_symbol["BANK.US"].rotation.selected is True
    assert by_symbol["BANK.US"].rotation.rank == 2
    assert by_symbol["SOFT.US"].rotation.selected is False
    assert (
        "ROTATION_RISK_GROUP_CAP"
        in by_symbol["SOFT.US"].rotation.exclusion_reasons
    )


def test_rotation_history_does_not_block_intraday_selection() -> None:
    row = select_candidates(
        [_input("AAPL.US", sector="Technology")],
        _config(),
    )[0]

    assert row.selected is True
    assert row.evaluable is True
    assert row.rotation.selected is False
    assert row.rotation.momentum_pct is None
    assert row.rotation.exclusion_reasons == (
        "ROTATION_HISTORY_INSUFFICIENT",
    )


def test_rotation_momentum_skips_the_most_recent_month() -> None:
    steady = select_candidates(
        [
            _rotation_input(
                "STEADY.US",
                sector="Financials",
                drift=0.0020,
                recent_move=0.001,
            )
        ],
        _config(),
    )[0]
    reversed_row = select_candidates(
        [
            _rotation_input(
                "REVERSED.US",
                sector="Financials",
                drift=0.0020,
                recent_move=-0.01,
            )
        ],
        _config(),
    )[0]

    assert steady.rotation.momentum_pct is not None
    assert reversed_row.rotation.momentum_pct is not None
    assert (
        steady.rotation.momentum_pct
        == reversed_row.rotation.momentum_pct
    )
    assert steady.rotation.above_sma is True
    assert reversed_row.rotation.above_sma is False


def test_t1_liquidity_spread_proxy_is_bounded_and_tightens_with_volume() -> None:
    lower_volume = liquidity_spread_proxy_bps(
        _bars(volume=5_000_000),
    )
    higher_volume = liquidity_spread_proxy_bps(
        _bars(volume=50_000_000),
    )

    assert lower_volume is not None
    assert higher_volume is not None
    assert 0.5 <= higher_volume < lower_volume <= 10


def test_selection_config_rejects_invalid_ranges() -> None:
    try:
        UniverseSelectionConfig(
            min_realized_vol_20d=0.5,
            max_realized_vol_20d=0.5,
        )
    except ValueError as exc:
        assert "realized-volatility" in str(exc)
    else:
        raise AssertionError("invalid realized volatility range was accepted")


def test_completed_daily_bars_excludes_current_partial_us_candle() -> None:
    bars = [
        _Bar(
            timestamp=datetime(2026, 7, day, 4, tzinfo=timezone.utc),
            open=100,
            high=102,
            low=99,
            close=101,
            volume=1_000_000,
        )
        for day in (21, 22, 23)
    ]

    complete = completed_daily_bars(
        bars,
        market="US",
        now=datetime(2026, 7, 23, 19, tzinfo=timezone.utc),
    )

    assert [bar.timestamp.day for bar in complete] == [21, 22]


def test_completed_daily_bars_includes_current_us_candle_after_delay() -> None:
    bars = [
        _Bar(
            timestamp=datetime(2026, 7, day, 4, tzinfo=timezone.utc),
            open=100,
            high=102,
            low=99,
            close=101,
            volume=1_000_000,
        )
        for day in (21, 22, 23)
    ]

    before_delay = completed_daily_bars(
        bars,
        market="US",
        now=datetime(2026, 7, 23, 20, 14, tzinfo=timezone.utc),
    )
    after_delay = completed_daily_bars(
        bars,
        market="US",
        now=datetime(2026, 7, 23, 20, 15, tzinfo=timezone.utc),
    )

    assert [bar.timestamp.day for bar in before_delay] == [21, 22]
    assert [bar.timestamp.day for bar in after_delay] == [21, 22, 23]


def test_latest_closed_session_date_uses_friday_on_weekend() -> None:
    assert latest_closed_session_date(
        market="US",
        now=datetime(2026, 7, 25, 12, tzinfo=timezone.utc),
    ).isoformat() == "2026-07-24"


def test_latest_closed_session_date_honors_us_half_day_close() -> None:
    assert latest_closed_session_date(
        market="US",
        now=datetime(2026, 12, 24, 18, 15, tzinfo=timezone.utc),
    ).isoformat() == "2026-12-24"
