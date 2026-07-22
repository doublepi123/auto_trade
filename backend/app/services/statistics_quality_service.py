from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.core.market_calendar import trade_day_for
from app.services.daily_pnl_service import (
    ClosedRoundTrip,
    PnlReplayIssue,
    PnlReplayIssueCode,
    RoundTripReplayResult,
)


@dataclass(frozen=True)
class StatisticsQualityItemData:
    trade_day: str
    symbol: str
    issue_code: str
    exit_order_id: int
    broker_order_id: str
    side: str
    filled_quantity: float
    matched_quantity: float
    unmatched_quantity: float
    exclusion_id: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class StatisticsQualityData:
    status: Literal[
        "COMPLETE",
        "KNOWN_EXCLUSIONS",
        "UNRESOLVED",
        "STALE_EXCLUSION",
    ] = "COMPLETE"
    known_exclusion_count: int = 0
    unresolved_issue_count: int = 0
    omitted_day_count: int = 0
    items: list[StatisticsQualityItemData] = field(default_factory=list)


@dataclass(frozen=True)
class StatisticsSample:
    trades: list[ClosedRoundTrip]
    issues: list[PnlReplayIssue]
    quality: StatisticsQualityData


_ISSUE_REASONS = {
    PnlReplayIssueCode.FULL_UNMATCHED_EXIT: (
        "exit has no locally verifiable opening cost basis"
    ),
    PnlReplayIssueCode.PARTIAL_OVERCLOSE: (
        "exit quantity exceeds the locally verifiable opening quantity"
    ),
    PnlReplayIssueCode.COST_BASIS_CONFLICT: (
        "persisted authoritative cost basis conflicts with ledger replay"
    ),
}


def build_statistics_quality(
    issues: list[PnlReplayIssue],
) -> StatisticsQualityData:
    ordered = sorted(
        issues,
        key=lambda issue: (
            issue.trade_day,
            issue.symbol,
            issue.filled_at,
            issue.exit_order_id,
        ),
    )
    items = [
        StatisticsQualityItemData(
            trade_day=issue.trade_day.isoformat(),
            symbol=issue.symbol,
            issue_code=issue.issue_code.value,
            exit_order_id=issue.exit_order_id,
            broker_order_id=issue.exit_broker_order_id,
            side=issue.side,
            filled_quantity=issue.filled_quantity,
            matched_quantity=issue.matched_quantity,
            unmatched_quantity=issue.unmatched_quantity,
            reason=_ISSUE_REASONS[issue.issue_code],
        )
        for issue in ordered
    ]
    omitted_days = {(issue.symbol, issue.trade_day) for issue in ordered}
    return StatisticsQualityData(
        status="UNRESOLVED" if items else "COMPLETE",
        unresolved_issue_count=len(items),
        omitted_day_count=len(omitted_days),
        items=items,
    )


def exclude_unresolved_trade_days(
    trades: list[ClosedRoundTrip],
    issues: list[PnlReplayIssue],
) -> list[ClosedRoundTrip]:
    unresolved_days = {(issue.symbol, issue.trade_day) for issue in issues}
    if not unresolved_days:
        return trades
    return [
        trade
        for trade in trades
        if (
            trade.symbol,
            trade_day_for(
                "HK" if trade.symbol.endswith(".HK") else "US",
                trade.exit_at,
            ),
        )
        not in unresolved_days
    ]


def select_statistics_sample(
    replay: RoundTripReplayResult,
    *,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> StatisticsSample:
    """Select a window while keeping market-day quality gates intact.

    Replay is intentionally requested without a time filter by callers. An
    issue just outside a UTC lookback boundary can share the same US/HK market
    day with an otherwise included trade; that entire market-day sample must
    still fail closed.
    """
    window_trades = [
        trade
        for trade in replay.trades
        if (from_dt is None or trade.exit_at >= from_dt)
        and (to_dt is None or trade.exit_at <= to_dt)
    ]
    trade_day_keys = {
        (
            trade.symbol,
            trade_day_for(
                "HK" if trade.symbol.upper().endswith(".HK") else "US",
                trade.exit_at,
            ),
        )
        for trade in window_trades
    }
    relevant_issues = [
        issue
        for issue in replay.issues
        if (
            (from_dt is None or issue.filled_at >= from_dt)
            and (to_dt is None or issue.filled_at <= to_dt)
        )
        or (issue.symbol, issue.trade_day) in trade_day_keys
    ]
    included_trades = exclude_unresolved_trade_days(
        window_trades,
        relevant_issues,
    )
    return StatisticsSample(
        trades=included_trades,
        issues=relevant_issues,
        quality=build_statistics_quality(relevant_issues),
    )
