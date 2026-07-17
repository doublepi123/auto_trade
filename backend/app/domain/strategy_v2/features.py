from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from statistics import stdev
from typing import Iterable, Sequence

from app.core.market_calendar import get_session


_ONE_MINUTE_PERIODS_PER_YEAR = 252 * 390


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("bar timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class StrategyBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str = ""
    duration_minutes: int = 1

    def __post_init__(self) -> None:
        timestamp = _as_utc(self.timestamp)
        object.__setattr__(self, "timestamp", timestamp)
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("bar values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("bar OHLC values are inconsistent")
        if self.high < self.low:
            raise ValueError("bar high must not be below low")
        if self.volume < 0:
            raise ValueError("bar volume must not be negative")

    @property
    def end_at(self) -> datetime:
        return self.timestamp + timedelta(minutes=self.duration_minutes)


@dataclass(frozen=True)
class StrategyV2FeatureConfig:
    market: str = "US"
    zscore_window_1m: int = 30
    zscore_window_5m: int = 12
    adx_period: int = 14
    realized_vol_window_1m: int = 30
    realized_vol_periods_per_year: int | None = None
    settlement_grace_seconds: int = 5

    def __post_init__(self) -> None:
        if self.market.upper() not in {"US", "HK"}:
            raise ValueError("market must be US or HK")
        object.__setattr__(self, "market", self.market.upper())
        if self.realized_vol_periods_per_year is None:
            minutes_per_session = 390 if self.market == "US" else 330
            object.__setattr__(
                self,
                "realized_vol_periods_per_year",
                252 * minutes_per_session,
            )
        for name in (
            "zscore_window_1m",
            "zscore_window_5m",
            "adx_period",
            "realized_vol_window_1m",
            "realized_vol_periods_per_year",
        ):
            if int(getattr(self, name) or 0) < 2:
                raise ValueError(f"{name} must be at least 2")
        if not 0 <= self.settlement_grace_seconds <= 60:
            raise ValueError("settlement_grace_seconds must be in [0, 60]")


@dataclass(frozen=True)
class StrategyV2FeatureSnapshot:
    bar: StrategyBar
    session_day: date
    bar_index: int
    bar_timestamp_5m: datetime | None
    session_vwap_1m: float | None
    residual_1m: float | None
    residual_mean_1m: float | None
    residual_sigma_1m: float | None
    zscore_1m: float | None
    session_vwap_5m: float | None
    residual_5m: float | None
    residual_mean_5m: float | None
    residual_sigma_5m: float | None
    zscore_5m: float | None
    adx_5m: float | None
    realized_vol_1m: float | None
    ready: bool
    gate_reasons: tuple[str, ...]


def session_vwap(bars: Sequence[StrategyBar]) -> float | None:
    """Return cumulative typical-price VWAP for already-filtered session bars."""
    notional = 0.0
    volume = 0.0
    for bar in bars:
        if bar.volume <= 0:
            continue
        typical = (bar.high + bar.low + bar.close) / 3.0
        notional += typical * bar.volume
        volume += bar.volume
    return notional / volume if volume > 0 else None


def leave_one_out_zscore(
    previous_values: Sequence[float],
    current_value: float,
    *,
    window: int,
) -> tuple[float | None, float | None, float | None]:
    """Standardize ``current_value`` using only the preceding ``window`` values."""
    if window < 2:
        raise ValueError("window must be at least 2")
    if not math.isfinite(current_value):
        raise ValueError("current_value must be finite")
    if len(previous_values) < window:
        return None, None, None
    sample = [float(value) for value in previous_values[-window:]]
    if any(not math.isfinite(value) for value in sample):
        raise ValueError("previous_values must be finite")
    mean = sum(sample) / window
    sigma = stdev(sample)
    if sigma <= 0:
        return mean, sigma, None
    return mean, sigma, (current_value - mean) / sigma


def annualized_realized_vol(
    closes: Sequence[float],
    *,
    window: int = 30,
    periods_per_year: int = _ONE_MINUTE_PERIODS_PER_YEAR,
    timestamps: Sequence[datetime] | None = None,
) -> float | None:
    """Annualized sample standard deviation of trailing log returns."""
    if window < 2 or periods_per_year <= 0:
        raise ValueError("window and periods_per_year must be positive")
    if len(closes) < window + 1:
        return None
    sample = [float(value) for value in closes[-(window + 1) :]]
    if any(not math.isfinite(value) or value <= 0 for value in sample):
        raise ValueError("closes must contain finite positive prices")
    sample_timestamps: list[datetime] | None = None
    if timestamps is not None:
        if len(timestamps) != len(closes):
            raise ValueError("timestamps must have the same length as closes")
        sample_timestamps = [_as_utc(value) for value in timestamps[-(window + 1) :]]
    returns = []
    for index in range(1, len(sample)):
        if (
            sample_timestamps is not None
            and sample_timestamps[index] - sample_timestamps[index - 1]
            != timedelta(minutes=1)
        ):
            continue
        returns.append(math.log(sample[index] / sample[index - 1]))
    if len(returns) < window:
        return None
    return stdev(returns) * math.sqrt(periods_per_year)


def wilder_adx(
    bars: Sequence[StrategyBar],
    *,
    period: int = 14,
) -> float | None:
    """Classic Wilder ADX; unavailable until ``2 * period`` bars."""
    if period < 2:
        raise ValueError("period must be at least 2")
    if len(bars) < 2 * period:
        return None
    values = list(bars)
    true_ranges: list[float] = [0.0] * len(values)
    plus_dm: list[float] = [0.0] * len(values)
    minus_dm: list[float] = [0.0] * len(values)
    for index in range(1, len(values)):
        current = values[index]
        previous = values[index - 1]
        move_up = current.high - previous.high
        move_down = previous.low - current.low
        plus_dm[index] = move_up if move_up > move_down and move_up > 0 else 0.0
        minus_dm[index] = move_down if move_down > move_up and move_down > 0 else 0.0
        true_ranges[index] = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )

    smooth_tr = sum(true_ranges[1 : period + 1])
    smooth_plus = sum(plus_dm[1 : period + 1])
    smooth_minus = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []
    if smooth_tr <= 0:
        dx_values.append(0.0)
    else:
        di_plus = 100.0 * smooth_plus / smooth_tr
        di_minus = 100.0 * smooth_minus / smooth_tr
        total = di_plus + di_minus
        dx_values.append(100.0 * abs(di_plus - di_minus) / total if total > 0 else 0.0)
    for index in range(period + 1, len(values)):
        smooth_tr = smooth_tr - smooth_tr / period + true_ranges[index]
        smooth_plus = smooth_plus - smooth_plus / period + plus_dm[index]
        smooth_minus = smooth_minus - smooth_minus / period + minus_dm[index]
        if smooth_tr <= 0:
            dx_values.append(0.0)
            continue
        di_plus = 100.0 * smooth_plus / smooth_tr
        di_minus = 100.0 * smooth_minus / smooth_tr
        total = di_plus + di_minus
        dx_values.append(100.0 * abs(di_plus - di_minus) / total if total > 0 else 0.0)
    if len(dx_values) < period:
        return None
    adx = sum(dx_values[:period]) / period
    for value in dx_values[period:]:
        adx = (adx * (period - 1) + value) / period
    return max(0.0, min(100.0, adx))


def _bucket_start(bar: StrategyBar, market: str) -> datetime:
    session = get_session(market)
    local = bar.timestamp.astimezone(session.timezone)
    session_open = datetime.combine(local.date(), session.rth_open, tzinfo=session.timezone)
    offset_minutes = int((local - session_open).total_seconds() // 60)
    bucket_local = session_open + timedelta(minutes=offset_minutes - offset_minutes % 5)
    return bucket_local.astimezone(timezone.utc)


def _aggregate_group(group: Sequence[StrategyBar], bucket_at: datetime) -> StrategyBar:
    ordered = sorted(group, key=lambda item: item.timestamp)
    return StrategyBar(
        timestamp=bucket_at,
        open=ordered[0].open,
        high=max(item.high for item in ordered),
        low=min(item.low for item in ordered),
        close=ordered[-1].close,
        volume=sum(item.volume for item in ordered),
        symbol=ordered[0].symbol,
        duration_minutes=5,
    )


def aggregate_complete_five_minute_bars(
    bars: Iterable[StrategyBar],
    *,
    market: str,
    observed_at: datetime | None = None,
) -> list[StrategyBar]:
    """Aggregate only complete, RTH-aligned five-minute buckets."""
    session = get_session(market)
    observation = _as_utc(observed_at) if observed_at is not None else None
    ordered_bars = sorted(bars, key=lambda item: item.timestamp)
    if not ordered_bars:
        return []
    symbols = {bar.symbol.strip().upper() for bar in ordered_bars}
    if "" in symbols or len(symbols) != 1:
        raise ValueError("five-minute aggregation requires one non-empty symbol")
    groups: dict[tuple[str, datetime], list[StrategyBar]] = {}
    for bar in ordered_bars:
        if bar.duration_minutes != 1 or not session.is_rth(bar.timestamp):
            continue
        if observation is not None and bar.end_at > observation:
            continue
        bucket = _bucket_start(bar, market)
        groups.setdefault((bar.symbol, bucket), []).append(bar)

    result: list[StrategyBar] = []
    for (_, bucket), group in sorted(groups.items(), key=lambda item: item[0][1]):
        expected = {bucket + timedelta(minutes=index) for index in range(5)}
        actual = {bar.timestamp for bar in group}
        if len(group) != 5 or actual != expected or any(bar.volume <= 0 for bar in group):
            continue
        bucket_end = bucket + timedelta(minutes=5)
        if observation is not None and bucket_end > observation:
            continue
        result.append(_aggregate_group(group, bucket))
    return result


class SessionFeatureEngine:
    """Causal per-session feature reducer driven by completed one-minute RTH bars."""

    def __init__(self, config: StrategyV2FeatureConfig | None = None) -> None:
        self.config = config or StrategyV2FeatureConfig()
        self._session_day: date | None = None
        self._bars_1m: list[StrategyBar] = []
        self._bars_5m: list[StrategyBar] = []
        self._residuals_1m: list[float] = []
        self._residuals_5m: list[float] = []
        self._five_minute_group: list[StrategyBar] = []
        self._session_complete = False
        self._last_snapshot: StrategyV2FeatureSnapshot | None = None
        self._symbol: str | None = None

    @property
    def session_day(self) -> date | None:
        return self._session_day

    def reset(self) -> None:
        self._session_day = None
        self._bars_1m.clear()
        self._bars_5m.clear()
        self._residuals_1m.clear()
        self._residuals_5m.clear()
        self._five_minute_group.clear()
        self._session_complete = False
        self._last_snapshot = None
        self._symbol = None

    def on_bar(
        self,
        bar: StrategyBar,
        *,
        observed_at: datetime | None = None,
    ) -> StrategyV2FeatureSnapshot | None:
        if bar.duration_minutes != 1:
            raise ValueError("SessionFeatureEngine accepts one-minute bars only")
        symbol = bar.symbol.strip().upper()
        if not symbol:
            raise ValueError("SessionFeatureEngine requires a non-empty symbol")
        if self._symbol is None:
            self._symbol = symbol
        elif symbol != self._symbol:
            raise ValueError("SessionFeatureEngine accepts exactly one symbol")
        settled_at = bar.end_at + timedelta(seconds=self.config.settlement_grace_seconds)
        observation = _as_utc(observed_at) if observed_at is not None else settled_at
        if settled_at > observation:
            return None
        session = get_session(self.config.market)
        if not session.is_rth(bar.timestamp):
            return None
        local = bar.timestamp.astimezone(session.timezone)
        session_day = local.date()

        if self._bars_1m and bar.timestamp == self._bars_1m[-1].timestamp:
            if bar != self._bars_1m[-1]:
                raise ValueError("conflicting duplicate one-minute bar")
            return self._last_snapshot
        if self._bars_1m and bar.timestamp < self._bars_1m[-1].timestamp:
            raise ValueError("one-minute bars must be processed in timestamp order")

        if session_day != self._session_day:
            self._start_session(session_day, bar)
        elif self._bars_1m:
            expected = self._next_rth_minute(self._bars_1m[-1].timestamp)
            if expected != bar.timestamp:
                self._session_complete = False
        if bar.volume <= 0:
            self._session_complete = False

        previous_residuals_1m = tuple(self._residuals_1m)
        self._bars_1m.append(bar)
        vwap_1m = session_vwap(self._bars_1m)
        residual_1m = math.log(bar.close / vwap_1m) if vwap_1m is not None else None
        mean_1m: float | None = None
        sigma_1m: float | None = None
        zscore_1m: float | None = None
        if residual_1m is not None:
            mean_1m, sigma_1m, zscore_1m = leave_one_out_zscore(
                previous_residuals_1m,
                residual_1m,
                window=self.config.zscore_window_1m,
            )
            self._residuals_1m.append(residual_1m)

        completed_5m = self._accept_five_minute_component(bar)
        residual_5m: float | None = None
        mean_5m: float | None = None
        sigma_5m: float | None = None
        zscore_5m: float | None = None
        if completed_5m is not None:
            previous_residuals_5m = tuple(self._residuals_5m)
            self._bars_5m.append(completed_5m)
            vwap_5m_current = session_vwap(self._bars_5m)
            if vwap_5m_current is not None:
                residual_5m = math.log(completed_5m.close / vwap_5m_current)
                mean_5m, sigma_5m, zscore_5m = leave_one_out_zscore(
                    previous_residuals_5m,
                    residual_5m,
                    window=self.config.zscore_window_5m,
                )
                self._residuals_5m.append(residual_5m)
        elif self._last_snapshot is not None:
            residual_5m = self._last_snapshot.residual_5m
            mean_5m = self._last_snapshot.residual_mean_5m
            sigma_5m = self._last_snapshot.residual_sigma_5m
            zscore_5m = self._last_snapshot.zscore_5m

        vwap_5m = session_vwap(self._bars_5m)
        adx_5m = wilder_adx(self._bars_5m, period=self.config.adx_period)
        realized_vol = annualized_realized_vol(
            [item.close for item in self._bars_1m],
            window=self.config.realized_vol_window_1m,
            periods_per_year=int(self.config.realized_vol_periods_per_year or 0),
            timestamps=[item.timestamp for item in self._bars_1m],
        )
        reasons = self._readiness_reasons(
            vwap_1m=vwap_1m,
            sigma_1m=sigma_1m,
            zscore_1m=zscore_1m,
            vwap_5m=vwap_5m,
            sigma_5m=sigma_5m,
            zscore_5m=zscore_5m,
            adx_5m=adx_5m,
            realized_vol=realized_vol,
            current_volume=bar.volume,
        )
        snapshot = StrategyV2FeatureSnapshot(
            bar=bar,
            session_day=session_day,
            bar_index=len(self._bars_1m) - 1,
            bar_timestamp_5m=self._bars_5m[-1].timestamp if self._bars_5m else None,
            session_vwap_1m=vwap_1m,
            residual_1m=residual_1m,
            residual_mean_1m=mean_1m,
            residual_sigma_1m=sigma_1m,
            zscore_1m=zscore_1m,
            session_vwap_5m=vwap_5m,
            residual_5m=residual_5m,
            residual_mean_5m=mean_5m,
            residual_sigma_5m=sigma_5m,
            zscore_5m=zscore_5m,
            adx_5m=adx_5m,
            realized_vol_1m=realized_vol,
            ready=not reasons,
            gate_reasons=tuple(reasons),
        )
        self._last_snapshot = snapshot
        return snapshot

    def _start_session(self, session_day: date, first_bar: StrategyBar) -> None:
        self._session_day = session_day
        self._bars_1m = []
        self._bars_5m = []
        self._residuals_1m = []
        self._residuals_5m = []
        self._five_minute_group = []
        self._last_snapshot = None
        session = get_session(self.config.market)
        local = first_bar.timestamp.astimezone(session.timezone)
        self._session_complete = local.time() == session.rth_open

    def _next_rth_minute(self, current: datetime) -> datetime | None:
        session = get_session(self.config.market)
        candidate = current + timedelta(minutes=1)
        current_day = session.trade_day(current)
        for _ in range(121):
            if session.trade_day(candidate) != current_day:
                return None
            if session.is_rth(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        return None

    def _accept_five_minute_component(self, bar: StrategyBar) -> StrategyBar | None:
        bucket = _bucket_start(bar, self.config.market)
        if self._five_minute_group:
            current_bucket = _bucket_start(self._five_minute_group[0], self.config.market)
            if current_bucket != bucket:
                if len(self._five_minute_group) != 5:
                    self._session_complete = False
                self._five_minute_group = []
        self._five_minute_group.append(bar)
        expected = [bucket + timedelta(minutes=index) for index in range(5)]
        actual = [item.timestamp for item in self._five_minute_group]
        if actual != expected[: len(actual)]:
            self._session_complete = False
            return None
        if len(self._five_minute_group) < 5:
            return None
        completed = _aggregate_group(self._five_minute_group, bucket)
        self._five_minute_group = []
        return completed

    def _readiness_reasons(
        self,
        *,
        vwap_1m: float | None,
        sigma_1m: float | None,
        zscore_1m: float | None,
        vwap_5m: float | None,
        sigma_5m: float | None,
        zscore_5m: float | None,
        adx_5m: float | None,
        realized_vol: float | None,
        current_volume: float,
    ) -> list[str]:
        reasons: list[str] = []
        if not self._session_complete:
            reasons.append("SESSION_DATA_INCOMPLETE")
        if current_volume <= 0:
            reasons.append("NON_POSITIVE_VOLUME")
        if vwap_1m is None:
            reasons.append("VWAP_1M_UNAVAILABLE")
        if sigma_1m == 0:
            reasons.append("RESIDUAL_SIGMA_1M_ZERO")
        elif zscore_1m is None:
            reasons.append("ZSCORE_1M_WARMUP")
        if vwap_5m is None:
            reasons.append("VWAP_5M_UNAVAILABLE")
        if sigma_5m == 0:
            reasons.append("RESIDUAL_SIGMA_5M_ZERO")
        elif zscore_5m is None:
            reasons.append("ZSCORE_5M_WARMUP")
        if adx_5m is None:
            reasons.append("ADX_5M_WARMUP")
        if realized_vol is None:
            reasons.append("REALIZED_VOL_1M_WARMUP")
        return reasons


class CausalTrendPrewarmFeatureEngine(SessionFeatureEngine):
    """Keep session-local residual features while causally prewarming trend state.

    ``seed_bars`` must contain exactly one complete earlier RTH session.  The
    seed primes only completed five-minute ADX and valid consecutive one-minute
    returns.  It never enters the base reducer, so VWAP and residual z-scores
    still begin empty when the first target-session bar arrives.
    """

    def __init__(
        self,
        config: StrategyV2FeatureConfig,
        seed_bars: Sequence[StrategyBar],
    ) -> None:
        super().__init__(config)
        self._seed_bars = self._validated_seed(seed_bars)
        self._trend_bars_5m: list[StrategyBar] = []
        self._trend_returns_1m: list[float] = []
        self._trend_five_minute_group: list[StrategyBar] = []
        self._trend_last_bar_1m: StrategyBar | None = None
        self._trend_symbol = self._seed_bars[0].symbol.strip().upper()
        self._seed_last_at = self._seed_bars[-1].timestamp
        self._restore_seed_state()

    def reset(self) -> None:
        """Reset target-session state and deterministically restore the seed."""
        super().reset()
        self._restore_seed_state()

    def on_bar(
        self,
        bar: StrategyBar,
        *,
        observed_at: datetime | None = None,
    ) -> StrategyV2FeatureSnapshot | None:
        symbol = bar.symbol.strip().upper()
        if symbol != self._trend_symbol:
            raise ValueError("prewarm seed and target bars must use the same symbol")
        if not self._bars_1m and bar.timestamp <= self._seed_last_at:
            raise ValueError("target bars must follow the prewarm seed session")

        previous_snapshot = self._last_snapshot
        snapshot = super().on_bar(bar, observed_at=observed_at)
        if snapshot is None or snapshot is previous_snapshot:
            return snapshot

        self._record_trend_bar(bar)
        adx_5m = wilder_adx(
            self._trend_bars_5m,
            period=self.config.adx_period,
        )
        realized_vol = self._trend_realized_vol()
        reasons = [
            reason
            for reason in snapshot.gate_reasons
            if reason not in {"ADX_5M_WARMUP", "REALIZED_VOL_1M_WARMUP"}
        ]
        if adx_5m is None:
            reasons.append("ADX_5M_WARMUP")
        if realized_vol is None:
            reasons.append("REALIZED_VOL_1M_WARMUP")
        result = replace(
            snapshot,
            adx_5m=adx_5m,
            realized_vol_1m=realized_vol,
            ready=not reasons,
            gate_reasons=tuple(reasons),
        )
        self._last_snapshot = result
        return result

    def _validated_seed(
        self,
        seed_bars: Sequence[StrategyBar],
    ) -> tuple[StrategyBar, ...]:
        ordered = tuple(sorted(seed_bars, key=lambda item: item.timestamp))
        if not ordered:
            raise ValueError("prewarm seed must contain one complete RTH session")
        if any(item.duration_minutes != 1 for item in ordered):
            raise ValueError("prewarm seed accepts one-minute bars only")
        symbols = {item.symbol.strip().upper() for item in ordered}
        if "" in symbols or len(symbols) != 1:
            raise ValueError("prewarm seed requires exactly one non-empty symbol")
        if any(item.volume <= 0 for item in ordered):
            raise ValueError("prewarm seed requires positive volume")

        session = get_session(self.config.market)
        session_days = {session.trade_day(item.timestamp) for item in ordered}
        if len(session_days) != 1:
            raise ValueError("prewarm seed must contain exactly one session")
        session_day = next(iter(session_days))
        local_midnight = datetime.combine(
            session_day,
            datetime.min.time(),
            tzinfo=session.timezone,
        )
        expected = [
            (local_midnight + timedelta(minutes=offset)).astimezone(timezone.utc)
            for offset in range(24 * 60)
            if session.is_rth(local_midnight + timedelta(minutes=offset))
        ]
        actual = [item.timestamp for item in ordered]
        if not expected or actual != expected:
            raise ValueError("prewarm seed must contain one complete RTH session")
        return ordered

    def _restore_seed_state(self) -> None:
        self._trend_bars_5m = []
        self._trend_returns_1m = []
        self._trend_five_minute_group = []
        self._trend_last_bar_1m = None
        for bar in self._seed_bars:
            self._record_trend_bar(bar)

    def _record_trend_bar(self, bar: StrategyBar) -> None:
        previous = self._trend_last_bar_1m
        if previous is not None:
            if bar.timestamp <= previous.timestamp:
                raise ValueError("trend prewarm bars must be strictly ordered")
            if bar.timestamp - previous.timestamp == timedelta(minutes=1):
                self._trend_returns_1m.append(math.log(bar.close / previous.close))
        self._trend_last_bar_1m = bar

        completed = self._accept_trend_five_minute_component(bar)
        if completed is not None:
            self._trend_bars_5m.append(completed)

    def _accept_trend_five_minute_component(
        self,
        bar: StrategyBar,
    ) -> StrategyBar | None:
        bucket = _bucket_start(bar, self.config.market)
        if self._trend_five_minute_group:
            current_bucket = _bucket_start(
                self._trend_five_minute_group[0],
                self.config.market,
            )
            if current_bucket != bucket:
                self._trend_five_minute_group = []
        self._trend_five_minute_group.append(bar)
        expected = [bucket + timedelta(minutes=index) for index in range(5)]
        actual = [item.timestamp for item in self._trend_five_minute_group]
        if actual != expected[: len(actual)]:
            self._trend_five_minute_group = []
            return None
        if len(self._trend_five_minute_group) < 5:
            return None
        completed = _aggregate_group(self._trend_five_minute_group, bucket)
        self._trend_five_minute_group = []
        return completed

    def _trend_realized_vol(self) -> float | None:
        window = self.config.realized_vol_window_1m
        if len(self._trend_returns_1m) < window:
            return None
        sample = self._trend_returns_1m[-window:]
        return stdev(sample) * math.sqrt(
            int(self.config.realized_vol_periods_per_year or 0)
        )
