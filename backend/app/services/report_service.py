from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.market_calendar import trade_day_for
from app.models import LLMInteraction
from app.services.daily_pnl_service import DailyPnlService, RealizedTrade


_REPORT_SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,12}\.(US|HK)$")


@dataclass(frozen=True)
class _LLMRec:
    applied: bool


@dataclass(frozen=True)
class DailyPnLPoint:
    date: str
    pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int
    win_count: int


@dataclass(frozen=True)
class ReportAttributionPoint:
    key: str
    label: str
    trade_count: int
    pnl: float
    win_rate: float
    share: float


@dataclass(frozen=True)
class _OrderDetail:
    broker_order_id: str
    side: str
    quantity: float
    executed_price: float
    status: str
    filled_at: datetime | None
    pnl: float


@dataclass(frozen=True)
class ReportDayDetail:
    date: str
    orders: list[_OrderDetail]


@dataclass(frozen=True)
class ReportMetrics:
    total_pnl: float
    total_trades: int
    win_count: int
    loss_count: int
    win_rate: float
    profit_loss_ratio: float
    avg_pnl_per_trade: float
    max_profit: float
    max_loss: float
    max_drawdown: float
    llm_suggestions_count: int
    llm_applied_count: int
    llm_apply_rate: float
    llm_profitable_count: int
    llm_accuracy_rate: float


@dataclass(frozen=True)
class PeriodReport:
    period_type: str
    symbol: str
    start_date: str
    end_date: str
    metrics: ReportMetrics
    daily_points: list[DailyPnLPoint]
    attribution: list[ReportAttributionPoint]
    details: list[ReportDayDetail]


class ReportService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_daily_report(self, symbol: str, target_date: str) -> PeriodReport:
        symbol = self._normalize_symbol(symbol)
        d = datetime.strptime(target_date, "%Y-%m-%d").date()
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return self._build_report(symbol, start, end, "daily", target_date, target_date)

    def get_weekly_report(self, symbol: str, week_start: str) -> PeriodReport:
        symbol = self._normalize_symbol(symbol)
        d = datetime.strptime(week_start, "%Y-%m-%d").date()
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        end = start + timedelta(days=7)
        return self._build_report(
            symbol, start, end, "weekly", week_start, (d + timedelta(days=6)).isoformat()
        )

    def get_monthly_report(self, symbol: str, month: str) -> PeriodReport:
        symbol = self._normalize_symbol(symbol)
        d = datetime.strptime(month, "%Y-%m").date()
        start = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
        if d.month == 12:
            end = datetime(d.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            end = datetime(d.year, d.month + 1, 1, tzinfo=timezone.utc)
        end_date_str = (end.date() - timedelta(days=1)).isoformat()
        return self._build_report(symbol, start, end, "monthly", d.isoformat(), end_date_str)

    def get_range_report(
        self, symbol: str, from_date: str, to_date: str
    ) -> PeriodReport:
        symbol = self._normalize_symbol(symbol)
        from_d = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_d = datetime.strptime(to_date, "%Y-%m-%d").date()
        if to_d < from_d:
            raise ValueError("to_date must be greater than or equal to from_date")
        start = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
        end = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)
        return self._build_report(symbol, start, end, "range", from_date, to_date)

    def export_report(
        self, symbol: str, from_date: str, to_date: str, fmt: str
    ) -> io.BytesIO:
        symbol = self._normalize_symbol(symbol)
        report = self.get_range_report(symbol, from_date, to_date)
        if fmt == "json":
            data = self._report_to_dict(report)
            buf = io.BytesIO(
                json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            )
            buf.seek(0)
            return buf
        if fmt != "csv":
            raise ValueError("format must be json or csv")
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "date", "symbol", "trade_count", "win_count", "pnl", "cumulative_pnl", "drawdown",
        ])
        for point in report.daily_points:
            writer.writerow([
                point.date,
                symbol,
                point.trade_count,
                point.win_count,
                point.pnl,
                point.cumulative_pnl,
                point.drawdown,
            ])
        bio = io.BytesIO(buf.getvalue().encode("utf-8"))
        bio.seek(0)
        return bio

    def _build_report(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        period_type: str,
        start_date_str: str,
        end_date_str: str,
    ) -> PeriodReport:
        llms = self._load_llm_interactions(symbol, start, end)
        pnl_service = DailyPnlService(self._db)
        market = "HK" if symbol.endswith(".HK") else "US"
        daily_points: list[DailyPnLPoint] = []
        details: list[ReportDayDetail] = []
        total_pnl = 0.0
        total_trades = 0
        total_wins = 0
        max_profit = 0.0
        max_loss = 0.0
        total_win_pnl = 0.0
        total_loss_pnl = 0.0
        win_trade_count = 0
        loss_trade_count = 0
        cumulative_pnl = 0.0
        peak_pnl = 0.0
        max_drawdown = 0.0
        current_day = start.date()
        end_day = (end - timedelta(days=1)).date()
        attribution_buckets: dict[str, list[RealizedTrade]] = {}
        while current_day <= end_day:
            result = pnl_service.calculate(
                trade_day=current_day,
                symbol=symbol,
                to_trade_day=lambda dt, market=market: trade_day_for(market, dt),
            )
            day_pnl = result.realized_pnl
            day_trades = len(result.trades)
            day_wins = sum(1 for t in result.trades if t.pnl > 0)
            cumulative_pnl += day_pnl
            peak_pnl = max(peak_pnl, cumulative_pnl)
            drawdown = peak_pnl - cumulative_pnl
            max_drawdown = max(max_drawdown, drawdown)
            if day_trades > 0 or abs(day_pnl) > 1e-9:
                daily_points.append(
                    DailyPnLPoint(
                        date=current_day.isoformat(),
                        pnl=round(day_pnl, 2),
                        cumulative_pnl=round(cumulative_pnl, 2),
                        drawdown=round(drawdown, 2),
                        trade_count=day_trades,
                        win_count=day_wins,
                    )
                )
            if result.trades:
                details.append(
                    ReportDayDetail(
                        date=current_day.isoformat(),
                        orders=[
                            _OrderDetail(
                                broker_order_id=t.broker_order_id,
                                side=t.side,
                                quantity=t.quantity,
                                executed_price=t.price,
                                status="FILLED",
                                filled_at=t.filled_at,
                                pnl=t.pnl,
                            )
                            for t in result.trades
                        ],
                    )
                )
            total_pnl += day_pnl
            total_trades += day_trades
            total_wins += day_wins
            for trade in result.trades:
                max_profit = max(max_profit, trade.pnl)
                max_loss = min(max_loss, trade.pnl)
                if trade.pnl > 0:
                    total_win_pnl += trade.pnl
                    win_trade_count += 1
                elif trade.pnl < 0:
                    total_loss_pnl += trade.pnl
                    loss_trade_count += 1
                attribution_buckets.setdefault(trade.side, []).append(trade)
            current_day += timedelta(days=1)

        metrics = self._compute_metrics_from_trades(
            total_pnl,
            total_trades,
            total_wins,
            max_profit,
            max_loss,
            max_drawdown,
            total_win_pnl,
            win_trade_count,
            total_loss_pnl,
            loss_trade_count,
            llms,
        )
        attribution = self._compute_attribution(attribution_buckets, total_pnl)

        return PeriodReport(
            period_type=period_type,
            symbol=symbol,
            start_date=start_date_str,
            end_date=end_date_str,
            metrics=metrics,
            daily_points=daily_points,
            attribution=attribution,
            details=details,
        )

    def _load_llm_interactions(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[_LLMRec]:
        interactions = (
            self._db.query(LLMInteraction)
            .filter(
                LLMInteraction.symbol == symbol,
                LLMInteraction.created_at >= start,
                LLMInteraction.created_at < end,
            )
            .all()
        )
        return [
            _LLMRec(applied=i.applied)
            for i in interactions
        ]

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        if not _REPORT_SYMBOL_RE.fullmatch(normalized):
            raise ValueError("symbol market must be US or HK")
        return normalized

    def _compute_metrics_from_trades(
        self,
        total_pnl: float,
        total_trades: int,
        win_count: int,
        max_profit: float,
        max_loss: float,
        max_drawdown: float,
        total_win_pnl: float,
        win_trade_count: int,
        total_loss_pnl: float,
        loss_trade_count: int,
        llms: list[_LLMRec],
    ) -> ReportMetrics:
        # Loss count is strictly the number of trades with negative realized PnL;
        # breakeven trades are neither wins nor losses.
        loss_count = loss_trade_count
        win_rate = win_count / total_trades if total_trades > 0 else 0.0
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0.0
        avg_profit = total_win_pnl / win_trade_count if win_trade_count > 0 else 0.0
        avg_loss = abs(total_loss_pnl / loss_trade_count) if loss_trade_count > 0 else 0.0
        profit_loss_ratio = (avg_profit / avg_loss) if avg_loss > 0 else 0.0

        llm_total = len(llms)
        llm_applied = sum(1 for l in llms if l.applied)
        llm_apply_rate = llm_applied / llm_total if llm_total > 0 else 0.0

        return ReportMetrics(
            total_pnl=round(total_pnl, 2),
            total_trades=total_trades,
            win_count=win_count,
            loss_count=loss_count,
            win_rate=round(win_rate, 4),
            profit_loss_ratio=round(profit_loss_ratio, 2),
            avg_pnl_per_trade=round(avg_pnl, 2),
            max_profit=round(max_profit, 2),
            max_loss=round(max_loss, 2),
            max_drawdown=round(max_drawdown, 2),
            llm_suggestions_count=llm_total,
            llm_applied_count=llm_applied,
            llm_apply_rate=round(llm_apply_rate, 4),
            # Phase 1 intentionally keeps LLM profitability/accuracy hard-coded at zero;
            # non-persistent/ad-hoc fields are not used here. Phase 2 will compute via order attribution.
            llm_profitable_count=0,
            llm_accuracy_rate=0.0,
        )

    @staticmethod
    def _compute_attribution(
        buckets: dict[str, list[RealizedTrade]], total_pnl: float
    ) -> list[ReportAttributionPoint]:
        points: list[ReportAttributionPoint] = []
        for side, trades in sorted(buckets.items()):
            count = len(trades)
            pnl = sum(t.pnl for t in trades)
            wins = sum(1 for t in trades if t.pnl > 0)
            win_rate = wins / count if count > 0 else 0.0
            share = pnl / total_pnl if total_pnl != 0 else 0.0
            points.append(
                ReportAttributionPoint(
                    key=side,
                    label=side,
                    trade_count=count,
                    pnl=round(pnl, 2),
                    win_rate=round(win_rate, 4),
                    share=round(share, 4),
                )
            )
        return points

    @staticmethod
    def _report_to_dict(report: PeriodReport) -> dict[str, Any]:
        return {
            "period_type": report.period_type,
            "symbol": report.symbol,
            "start_date": report.start_date,
            "end_date": report.end_date,
            "metrics": {
                "total_pnl": report.metrics.total_pnl,
                "total_trades": report.metrics.total_trades,
                "win_count": report.metrics.win_count,
                "loss_count": report.metrics.loss_count,
                "win_rate": report.metrics.win_rate,
                "profit_loss_ratio": report.metrics.profit_loss_ratio,
                "avg_pnl_per_trade": report.metrics.avg_pnl_per_trade,
                "max_profit": report.metrics.max_profit,
                "max_loss": report.metrics.max_loss,
                "max_drawdown": report.metrics.max_drawdown,
                "llm_suggestions_count": report.metrics.llm_suggestions_count,
                "llm_applied_count": report.metrics.llm_applied_count,
                "llm_apply_rate": report.metrics.llm_apply_rate,
                "llm_profitable_count": report.metrics.llm_profitable_count,
                "llm_accuracy_rate": report.metrics.llm_accuracy_rate,
            },
            "daily_points": [
                {
                    "date": p.date,
                    "pnl": p.pnl,
                    "cumulative_pnl": p.cumulative_pnl,
                    "drawdown": p.drawdown,
                    "trade_count": p.trade_count,
                    "win_count": p.win_count,
                }
                for p in report.daily_points
            ],
            "attribution": [asdict(a) for a in report.attribution],
            "details": [asdict(d) for d in report.details],
        }
