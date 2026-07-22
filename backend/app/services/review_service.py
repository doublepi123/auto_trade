from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.market_calendar import market_for_symbol, trade_day_for
from app.models import LLMInteraction, OrderRecord, RuntimeStateSnapshot, TradeEvent
from app.services.daily_pnl_service import DailyPnlService
from app.services.statistics_quality_service import (
    build_statistics_quality,
    select_statistics_sample,
)


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _format_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


class ReviewService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_review(self, symbol: str, from_date: str, to_date: str) -> dict[str, Any]:
        from app.models import StrategyConfig
        from_d = _parse_date(from_date)
        to_d = _parse_date(to_date)
        start_at = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
        end_at = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        config = self._db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        max_daily_loss = float(config.max_daily_loss) if config and config.max_daily_loss else 0.0

        llm_interactions = (
            self._db.query(LLMInteraction)
            .filter(
                LLMInteraction.symbol == symbol,
                LLMInteraction.created_at >= start_at,
                LLMInteraction.created_at < end_at,
            )
            .all()
        )

        orders = (
            self._db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.created_at >= start_at,
                OrderRecord.created_at < end_at,
            )
            .all()
        )

        events = (
            self._db.query(TradeEvent)
            .filter(
                TradeEvent.symbol == symbol,
                TradeEvent.created_at >= start_at,
                TradeEvent.created_at < end_at,
            )
            .all()
        )

        snapshots = (
            self._db.query(RuntimeStateSnapshot)
            .filter(
                RuntimeStateSnapshot.symbol == symbol,
                RuntimeStateSnapshot.created_at >= start_at,
                RuntimeStateSnapshot.created_at < end_at,
            )
            .order_by(RuntimeStateSnapshot.created_at.asc(), RuntimeStateSnapshot.id.asc())
            .all()
        )

        # Group by date
        days: dict[str, dict[str, Any]] = {}
        day_market_keys: dict[str, set[tuple[str, date]]] = {}

        for interaction in llm_interactions:
            d = _format_date(interaction.created_at)
            if d not in days:
                days[d] = self._empty_day(d, symbol)
            days[d]["llm_interactions"].append(self._llm_to_dict(interaction))

        for order in orders:
            d = _format_date(order.created_at)
            if d not in days:
                days[d] = self._empty_day(d, symbol)
            days[d]["orders"].append(self._order_to_dict(order))
            instant = order.filled_at or order.created_at
            day_market_keys.setdefault(d, set()).add((
                order.symbol,
                trade_day_for(market_for_symbol(order.symbol), instant),
            ))

        for event in events:
            d = _format_date(event.created_at)
            if d not in days:
                days[d] = self._empty_day(d, symbol)
            days[d]["events"].append(self._event_to_dict(event))

        for snapshot in snapshots:
            d = _format_date(snapshot.created_at)
            if d not in days:
                days[d] = self._empty_day(d, symbol)
            days[d]["snapshots"].append(self._snapshot_to_dict(snapshot))

        replay = DailyPnlService(self._db).pair_round_trips_with_issues(
            symbol=symbol,
            include_excursions=False,
        )
        sample = select_statistics_sample(
            replay,
            from_dt=start_at,
            to_dt=end_at - timedelta(microseconds=1),
        )
        for issue in sample.issues:
            issue_utc_day = _format_date(issue.filled_at)
            if from_date <= issue_utc_day <= to_date:
                if issue_utc_day not in days:
                    days[issue_utc_day] = self._empty_day(issue_utc_day, symbol)
                day_market_keys.setdefault(issue_utc_day, set()).add(
                    (issue.symbol, issue.trade_day)
                )

        # Compute error tags and daily PnL
        total_pnl = 0.0
        total_trades = 0
        all_error_tags: set[str] = set()

        for d in sorted(days.keys()):
            day = days[d]
            keys = day_market_keys.get(d, set())
            day_issues = [
                issue
                for issue in sample.issues
                if (issue.symbol, issue.trade_day) in keys
            ]
            day_quality = build_statistics_quality(day_issues)
            day["included_in_statistics"] = not day_issues
            day["statistics_quality"] = asdict(day_quality)
            day["daily_pnl"] = (day["snapshots"][-1].get("daily_pnl", 0) if day["snapshots"] else 0.0)
            day["trade_count"] = len([o for o in day["orders"] if o["status"] in ("FILLED", "PARTIAL_FILLED")])
            day["error_tags"] = self._compute_error_tags(day, symbol, max_daily_loss)
            all_error_tags.update(day["error_tags"])
            if day["included_in_statistics"]:
                total_pnl += day["daily_pnl"]
                total_trades += day["trade_count"]

        return {
            "symbol": symbol,
            "from_date": from_date,
            "to_date": to_date,
            "days": [days[d] for d in sorted(days.keys(), reverse=True)],
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "all_error_tags": sorted(all_error_tags),
            "statistics_quality": asdict(sample.quality),
        }

    def get_runtime_history(self, symbol: str, from_date: str, to_date: str) -> dict[str, Any]:
        from_d = _parse_date(from_date)
        to_d = _parse_date(to_date)
        start_at = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
        end_at = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

        snapshots = (
            self._db.query(RuntimeStateSnapshot)
            .filter(
                RuntimeStateSnapshot.symbol == symbol,
                RuntimeStateSnapshot.created_at >= start_at,
                RuntimeStateSnapshot.created_at < end_at,
            )
            .order_by(RuntimeStateSnapshot.created_at.asc(), RuntimeStateSnapshot.id.asc())
            .all()
        )

        markers = (
            self._db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.status.in_(("FILLED", "PARTIAL_FILLED")),
                OrderRecord.created_at >= start_at,
                OrderRecord.created_at < end_at,
            )
            .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
            .all()
        )

        return {
            "points": [
                {
                    "symbol": snapshot.symbol,
                    "timestamp": snapshot.created_at.isoformat(),
                    "engine_state": snapshot.engine_state,
                    "paused": snapshot.paused,
                    "kill_switch": snapshot.kill_switch,
                    "daily_pnl": snapshot.daily_pnl,
                    "consecutive_losses": snapshot.consecutive_losses,
                    "last_price": snapshot.last_price,
                    "last_trigger_price": snapshot.last_trigger_price,
                }
                for snapshot in snapshots
            ],
            "markers": [
                {
                    "timestamp": order.created_at.isoformat(),
                    "broker_order_id": order.broker_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.executed_quantity or order.quantity,
                    "price": order.executed_price or order.price,
                    "status": order.status,
                }
                for order in markers
            ],
        }

    def export_review(
        self,
        symbol: str,
        from_date: str,
        to_date: str,
        fmt: str,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> io.BytesIO:
        review = self.get_review(symbol, from_date, to_date)
        runtime_history = self.get_runtime_history(symbol, from_date, to_date)
        diagnostics_payload = diagnostics or {}
        if fmt == "json":
            payload = {
                "review": review,
                "runtime_history": runtime_history,
                "diagnostics": diagnostics_payload,
            }
            buf = io.BytesIO(json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
            buf.seek(0)
            return buf
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["section", "row_type", "date", "symbol", "field_a", "field_b", "field_c", "field_d"])
        for day in review["days"]:
            writer.writerow([
                "review_day",
                "summary",
                day["date"],
                symbol,
                day["trade_count"],
                day["daily_pnl"],
                ";".join(day["error_tags"]),
                (
                    f"events={len(day['events'])};"
                    f"included_in_statistics={str(day['included_in_statistics']).lower()};"
                    f"quality={day['statistics_quality']['status']}"
                ),
            ])
        for point in runtime_history["points"]:
            writer.writerow([
                "history_point",
                "runtime_point",
                point["timestamp"],
                point["symbol"],
                point["engine_state"],
                point["last_price"],
                point["daily_pnl"],
                point["last_trigger_price"],
            ])
        for marker in runtime_history["markers"]:
            writer.writerow([
                "history_marker",
                "trade_marker",
                marker["timestamp"],
                marker["symbol"],
                marker["side"],
                marker["quantity"],
                marker["price"],
                marker["status"],
            ])
        for runtime in diagnostics_payload.get("symbol_runtimes", []):
            writer.writerow([
                "diagnostic_runtime",
                "runtime",
                runtime.get("symbol", ""),
                runtime.get("symbol", ""),
                runtime.get("engine_state", ""),
                runtime.get("last_price", ""),
                runtime.get("last_trigger_price", ""),
                runtime.get("has_pending_order", ""),
            ])
        pending_symbols = diagnostics_payload.get("pending_order_symbols", [])
        writer.writerow([
            "diagnostic_meta",
            "pending_order_symbols",
            "",
            ",".join(pending_symbols),
            diagnostics_payload.get("runner_running", ""),
            diagnostics_payload.get("thread_alive", ""),
            diagnostics_payload.get("quotes_subscribed", ""),
            diagnostics_payload.get("trigger_in_flight", ""),
        ])
        quality = review["statistics_quality"]
        writer.writerow([])
        writer.writerow([
            "statistics_quality_status",
            "known_exclusion_count",
            "unresolved_issue_count",
            "omitted_day_count",
        ])
        writer.writerow([
            quality["status"],
            quality["known_exclusion_count"],
            quality["unresolved_issue_count"],
            quality["omitted_day_count"],
        ])
        writer.writerow([
            "quality_trade_day",
            "quality_symbol",
            "issue_code",
            "exit_order_id",
            "broker_order_id",
            "side",
            "filled_quantity",
            "matched_quantity",
            "unmatched_quantity",
            "exclusion_id",
            "reason",
        ])
        for item in quality["items"]:
            writer.writerow([
                item["trade_day"],
                item["symbol"],
                item["issue_code"],
                item["exit_order_id"],
                item["broker_order_id"],
                item["side"],
                item["filled_quantity"],
                item["matched_quantity"],
                item["unmatched_quantity"],
                item["exclusion_id"],
                item["reason"],
            ])
        bio = io.BytesIO(buf.getvalue().encode("utf-8"))
        bio.seek(0)
        return bio

    @staticmethod
    def _empty_day(d: str, symbol: str) -> dict[str, Any]:
        return {
            "date": d,
            "symbol": symbol,
            "llm_interactions": [],
            "orders": [],
            "events": [],
            "snapshots": [],
            "daily_pnl": 0.0,
            "trade_count": 0,
            "error_tags": [],
        }

    @staticmethod
    def _llm_to_dict(interaction: LLMInteraction) -> dict[str, Any]:
        return {
            "id": interaction.id,
            "interaction_type": interaction.interaction_type,
            "symbol": interaction.symbol,
            "market": interaction.market,
            "success": interaction.success,
            "order_action": interaction.order_action,
            "order_status": interaction.order_status,
            "order_id": interaction.order_id,
            "applied": interaction.applied,
            "created_at": interaction.created_at.isoformat(),
        }

    @staticmethod
    def _order_to_dict(order: OrderRecord) -> dict[str, Any]:
        return {
            "id": order.id,
            "broker_order_id": order.broker_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "executed_quantity": order.executed_quantity,
            "executed_price": order.executed_price,
            "status": order.status,
            "created_at": order.created_at.isoformat(),
            "filled_at": order.filled_at.isoformat() if order.filled_at else None,
        }

    @staticmethod
    def _event_to_dict(event: TradeEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "broker_order_id": event.broker_order_id,
            "side": event.side,
            "status": event.status,
            "message": event.message,
            "payload_json": event.payload_json,
            "created_at": event.created_at.isoformat(),
        }

    @staticmethod
    def _snapshot_to_dict(snapshot: RuntimeStateSnapshot) -> dict[str, Any]:
        return {
            "id": snapshot.id,
            "engine_state": snapshot.engine_state,
            "daily_pnl": snapshot.daily_pnl,
            "consecutive_losses": snapshot.consecutive_losses,
            "last_price": snapshot.last_price,
            "last_trigger_price": snapshot.last_trigger_price,
            "created_at": snapshot.created_at.isoformat(),
        }

    @staticmethod
    def _compute_error_tags(day: dict[str, Any], symbol: str, max_daily_loss: float = 0.0) -> list[str]:
        tags: set[str] = set()
        orders = day["orders"]
        events = day["events"]
        llm_interactions = day["llm_interactions"]

        # 收益不足: ORDER_SKIPPED with skip_category='FEE'
        for event in events:
            if event["event_type"] == "ORDER_SKIPPED":
                try:
                    payload = json.loads(event.get("payload_json", "{}"))
                    if payload.get("skip_category") == "FEE":
                        tags.add("收益不足")
                except Exception:
                    pass

        # 频繁重挂: ≥3 ORDER_CANCELLED
        cancelled_count = sum(1 for e in events if e["event_type"] == "ORDER_CANCELLED")
        if cancelled_count >= 3:
            tags.add("频繁重挂")

        # 过早买入/过早卖出: 对比 LLM 建议和后续价格变化
        # 简化: 如果有 LLM 建议但订单亏损，标记
        for llm in llm_interactions:
            if llm["applied"] and llm["order_id"]:
                # 查找对应订单
                related_orders = [o for o in orders if o["broker_order_id"] == llm["order_id"]]
                for order in related_orders:
                    if order["status"] in ("FILLED", "PARTIAL_FILLED") and order["executed_price"]:
                        # 检查同一日的快照是否有更低/更高价格
                        snapshots = day["snapshots"]
                        if snapshots:
                            prices = [s["last_price"] for s in snapshots if s["last_price"] > 0]
                            if prices:
                                min_price = min(prices)
                                max_price = max(prices)
                                if order["side"] in ("BUY", "BUY_TO_COVER") and order["executed_price"] > min_price * 1.02:
                                    tags.add("过早买入")
                                elif order["side"] in ("SELL", "SELL_SHORT") and order["executed_price"] < max_price * 0.98:
                                    tags.add("过早卖出")

        # 错过止损: 日亏损超过阈值但无风控暂停
        snapshots = day["snapshots"]
        if snapshots:
            worst_daily_pnl = min(s["daily_pnl"] for s in snapshots)
            loss_threshold = -abs(max_daily_loss) if max_daily_loss else -1000
            if worst_daily_pnl < loss_threshold:
                has_risk_pause = any(e["event_type"] == "RISK_PAUSED" for e in events)
                if not has_risk_pause:
                    tags.add("错过止损")

        return sorted(tags)
