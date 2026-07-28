from __future__ import annotations

import gzip
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.cli.import_opening_activity import OpeningActivityRecord
from app.cli.opening_extension_research import RawMinuteBar, _save_cache
from app.cli.opening_relative_volume_orb_research import (
    RelativeVolumeOrbResearchConfig,
    RelativeVolumeOrbResearchResult,
    evaluate_relative_volume_orb,
    load_research_inputs,
    load_seed_research_inputs,
    materialize_candidate_exit_paths,
    research_payload,
)
from app.core.broker import BrokerCandle
from app.core.market_calendar import get_session
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
    _Candle,
)


def _timestamp(session_date: date, offset: int) -> datetime:
    market_session = get_session("US")
    local_open = datetime.combine(
        session_date,
        market_session.rth_open,
        tzinfo=market_session.timezone,
    )
    return (local_open + timedelta(minutes=offset)).astimezone(timezone.utc)


def _bars_for_day(
    session_date: date,
    *,
    symbol: str,
    stop_on_offset_seven: bool = False,
) -> tuple[RawMinuteBar, ...]:
    signal_close = 102.0 if symbol == "AAA.US" else 104.0
    entry_price = signal_close
    rows: list[RawMinuteBar] = []
    for offset in range(9):
        if offset < 5:
            rows.append(RawMinuteBar(
                timestamp=_timestamp(session_date, offset),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
            ))
        elif offset == 5:
            rows.append(RawMinuteBar(
                timestamp=_timestamp(session_date, offset),
                open=100.5,
                high=signal_close,
                low=100.5,
                close=signal_close,
            ))
        else:
            low = 98.0 if stop_on_offset_seven and offset == 7 else entry_price
            rows.append(RawMinuteBar(
                timestamp=_timestamp(session_date, offset),
                open=entry_price if offset < 8 else entry_price + 1.0,
                high=entry_price + 1.0,
                low=low,
                close=entry_price + 0.5,
            ))
    return tuple(rows)


def _shadow_candle(bar: RawMinuteBar) -> _Candle:
    assert bar.high is not None
    assert bar.low is not None
    return _Candle(
        timestamp=bar.timestamp,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
    )


class _FakeHistoricalProvider:
    def __init__(
        self,
        rows_by_symbol: dict[str, tuple[BrokerCandle, ...]],
        *,
        response_limit: int | None = None,
    ) -> None:
        self.rows_by_symbol = rows_by_symbol
        self.response_limit = response_limit
        self.calls: list[tuple[str, str, int, datetime]] = []

    def get_history_candlesticks_by_offset(
        self,
        symbol: str,
        period: str,
        count: int,
        after: datetime,
    ) -> list[BrokerCandle]:
        self.calls.append((symbol, period, count, after))
        rows = [
            item
            for item in self.rows_by_symbol.get(symbol, ())
            if item.timestamp >= after
        ][:count]
        if self.response_limit is not None:
            rows = rows[: self.response_limit]
        return rows


def _research_data(
    *,
    stop_on_signal_day: bool = False,
) -> tuple[
    dict[str, tuple[RawMinuteBar, ...]],
    tuple[OpeningActivityRecord, ...],
    date,
]:
    start = date(2026, 6, 1)
    dates = tuple(start + timedelta(days=index) for index in range(16))
    signal_date = dates[14]
    bars_by_symbol = {
        symbol: tuple(
            bar
            for session_date in dates
            for bar in _bars_for_day(
                session_date,
                symbol=symbol,
                stop_on_offset_seven=(
                    stop_on_signal_day
                    and symbol == "AAA.US"
                    and session_date == signal_date
                ),
            )
        )
        for symbol in ("AAA.US", "BBB.US")
    }
    records: list[OpeningActivityRecord] = []
    for index, session_date in enumerate(dates):
        for symbol in ("AAA.US", "BBB.US"):
            volume = 100.0
            if index == 14:
                volume = 1_000.0 if symbol == "AAA.US" else 200.0
            elif index == 15:
                # This future value must not affect the prior session ratio.
                volume = 100_000.0 if symbol == "AAA.US" else 100.0
            records.append(OpeningActivityRecord(
                session_date=session_date,
                symbol=symbol,
                volume=volume,
                turnover=None,
                observed_at=_timestamp(session_date, 5),
            ))
    return bars_by_symbol, tuple(records), signal_date


def _config() -> RelativeVolumeOrbResearchConfig:
    return RelativeVolumeOrbResearchConfig(
        holding_minutes=2,
        top_n=1,
        minimum_universe_size=2,
        minimum_data_coverage=1.0,
    )


def test_required_cache_offset_matches_production_timing() -> None:
    config = RelativeVolumeOrbResearchConfig()

    assert config.required_maximum_offset == 66


def test_loader_rejects_cache_that_cannot_settle_sixty_minute_exit(
    tmp_path: Path,
) -> None:
    session_date = date(2026, 7, 27)
    ohlc_path = tmp_path / "opening-64.json.gz"
    bars = tuple(
        RawMinuteBar(
            timestamp=_timestamp(session_date, offset),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
        )
        for offset in range(65)
    )
    _save_cache(
        ohlc_path,
        {"AAA.US": bars},
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=64,
    )

    with pytest.raises(ValueError, match="covers only 64 minutes"):
        load_research_inputs(
            ohlc_cache_path=ohlc_path,
            activity_cache_path=tmp_path / "unused.json.gz",
            start_date=session_date,
            end_date=session_date,
            required_maximum_offset=66,
        )


def test_seed_loader_preserves_minutes_beyond_selection_horizon(
    tmp_path: Path,
) -> None:
    session_date = date(2026, 7, 27)
    ohlc_path = tmp_path / "opening-8.json.gz"
    activity_path = tmp_path / "activity.json.gz"
    bars = tuple(
        RawMinuteBar(
            timestamp=_timestamp(session_date, offset),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
        )
        for offset in range(9)
    )
    _save_cache(
        ohlc_path,
        {"AAA.US": bars},
        start_date=session_date,
        end_date=session_date,
        retained_minutes_after_open=8,
    )
    with gzip.open(activity_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "cache_version": "opening-activity-first6-v1",
                "start_date": session_date.isoformat(),
                "end_date": session_date.isoformat(),
                "window_minutes": 6,
                "symbols": {
                    "AAA.US": {
                        _timestamp(session_date, offset).isoformat(): [
                            100.0,
                            1_000.0,
                        ]
                        for offset in range(5)
                    },
                },
            },
            handle,
        )

    loaded, activity = load_seed_research_inputs(
        ohlc_cache_path=ohlc_path,
        activity_cache_path=activity_path,
        start_date=session_date,
        end_date=session_date,
        minimum_required_offset=6,
    )

    assert len(loaded["AAA.US"]) == 9
    assert len(activity) == 1


def test_research_uses_causal_activity_top_n_and_exact_exit_open() -> None:
    bars_by_symbol, records, signal_date = _research_data()

    result = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=_config(),
    )

    trade = next(
        item for item in result.trades if item.session_date == signal_date
    )
    session = next(
        item for item in result.sessions if item.session_date == signal_date
    )
    assert trade.symbol == "AAA.US"
    assert trade.activity_ratio == pytest.approx(10.0)
    assert trade.entry_price == pytest.approx(102.0)
    assert trade.exit_price == pytest.approx(103.0)
    assert trade.exit_reason == "FIXED_HOLD_EXIT"
    assert trade.net_return_bps == pytest.approx(
        (103.0 / 102.0 - 1) * 10_000 - 30.0
    )
    assert session.top_symbols == ("AAA.US",)


def test_alternative_candidate_modes_are_explicit_and_deterministic() -> None:
    bars_by_symbol, records, signal_date = _research_data()
    breakout = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=replace(_config(), top_n=2),
    )
    activity = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=replace(
            _config(),
            top_n=2,
            candidate_selection_mode="ACTIVITY_RATIO",
        ),
    )
    opening_return = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=replace(
            _config(),
            top_n=2,
            candidate_selection_mode="OPENING_RETURN",
        ),
    )

    def selected_symbol(result: RelativeVolumeOrbResearchResult) -> str:
        return next(
            item.symbol
            for item in result.trades
            if item.session_date == signal_date
        )

    assert selected_symbol(breakout) == "BBB.US"
    assert selected_symbol(activity) == "AAA.US"
    assert selected_symbol(opening_return) == "BBB.US"


def test_research_matches_opening_range_stop_fill_semantics() -> None:
    bars_by_symbol, records, signal_date = _research_data(
        stop_on_signal_day=True,
    )

    result = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=_config(),
    )

    trade = next(
        item for item in result.trades if item.session_date == signal_date
    )
    assert trade.stop_price == pytest.approx(99.0)
    assert trade.exit_price == pytest.approx(99.0)
    assert trade.exit_reason == "STOP_LOSS_EXIT"
    assert trade.net_return_bps == pytest.approx(
        (99.0 / 102.0 - 1) * 10_000 - 30.0
    )

    stop_loss_pct = OpeningMomentumShadowService._opening_range_stop_loss_pct(
        opening_range_low=99.0,
        entry_price=102.0,
        maximum_stop_loss_pct=4.0,
    )
    assert stop_loss_pct is not None
    production_outcome = OpeningMomentumShadowService._exit_outcome(
        tuple(
            _shadow_candle(bar)
            for bar in bars_by_symbol["AAA.US"]
            if signal_date
            == get_session("US").local(bar.timestamp).date()
        ),
        entry_at=_timestamp(signal_date, 6),
        exit_due_at=_timestamp(signal_date, 8),
        entry_price=102.0,
        stop_loss_pct=stop_loss_pct,
    )
    assert production_outcome.price == pytest.approx(trade.exit_price)
    assert production_outcome.reason == trade.exit_reason


def test_materializer_fetches_only_selected_candidate_exit_gap() -> None:
    bars_by_symbol, records, signal_date = _research_data()
    complete_aaa = bars_by_symbol["AAA.US"]
    partial = {
        **bars_by_symbol,
        "AAA.US": tuple(
            bar
            for bar in complete_aaa
            if not (
                get_session("US").local(bar.timestamp).date() == signal_date
                and bar.timestamp
                in {
                    _timestamp(signal_date, 7),
                    _timestamp(signal_date, 8),
                }
            )
        ),
    }
    provider = _FakeHistoricalProvider({
        "AAA.US": tuple(
            BrokerCandle(
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high or bar.open,
                low=bar.low or bar.open,
                close=bar.close,
                volume=1.0,
            )
            for bar in complete_aaa
        ),
    })

    extended, report = materialize_candidate_exit_paths(
        partial,
        records,
        provider,
        config=_config(),
    )

    assert provider.calls == [
        ("AAA.US", "MIN_1", 2, _timestamp(signal_date, 7)),
    ]
    assert report.required_maximum_offset == 8
    assert report.incomplete_candidate_sessions_before == 1
    assert report.fetch_requests == 1
    assert report.fetched_bars == 2
    assert report.incomplete_candidate_sessions_after == 0
    result = evaluate_relative_volume_orb(
        extended,
        records,
        config=_config(),
    )
    trade = next(
        item for item in result.trades if item.session_date == signal_date
    )
    assert trade.symbol == "AAA.US"
    assert trade.exit_price == pytest.approx(103.0)


def test_materializer_rejects_an_incomplete_provider_response() -> None:
    bars_by_symbol, records, signal_date = _research_data()
    complete_aaa = bars_by_symbol["AAA.US"]
    partial = {
        **bars_by_symbol,
        "AAA.US": tuple(
            bar
            for bar in complete_aaa
            if bar.timestamp
            not in {
                _timestamp(signal_date, 7),
                _timestamp(signal_date, 8),
            }
        ),
    }
    provider = _FakeHistoricalProvider(
        {
            "AAA.US": tuple(
                BrokerCandle(
                    timestamp=bar.timestamp,
                    open=bar.open,
                    high=bar.high or bar.open,
                    low=bar.low or bar.open,
                    close=bar.close,
                    volume=1.0,
                )
                for bar in complete_aaa
            ),
        },
        response_limit=1,
    )

    with pytest.raises(
        ValueError,
        match="did not complete candidate exit paths",
    ):
        materialize_candidate_exit_paths(
            partial,
            records,
            provider,
            config=_config(),
        )


def test_report_keeps_holdout_separate_and_disables_auto_promotion() -> None:
    bars_by_symbol, records, _ = _research_data()
    result = evaluate_relative_volume_orb(
        bars_by_symbol,
        records,
        config=_config(),
    )

    payload = research_payload(result)

    assert payload["automatic_promotion_allowed"] is False
    assert payload["research_design"] == {
        "causal_activity_baseline": True,
        "fixed_catalog_universe": True,
        "point_in_time_membership": False,
        "selection_uses_holdout": False,
        "candidate_selection_mode": "BREAKOUT_DEPTH",
        "production_exit_semantics": True,
        "required_maximum_offset": 8,
        "discovery_ratio": 0.60,
    }
    sessions = payload["sessions"]
    assert isinstance(sessions, dict)
    assert sessions["discovery_dates"]
    assert sessions["holdout_dates"]
    performance = payload["performance"]
    assert isinstance(performance, dict)
    assert "without_best_3_bps" in performance["all"]
