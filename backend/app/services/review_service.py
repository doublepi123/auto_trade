from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import LLMInteraction, OrderRecord, RuntimeStateSnapshot, TradeEvent


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _format_date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


class ReviewService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_review(self, symbol: str, from_date: str, to_date: str) -> dict[str, Any]:
        from_d = _parse_date(from_date)
        to_d = _parse_date(to_date)

        llm_interactions = (
            self._db.query(LLMInteraction)
            .filter(
                LLMInteraction.symbol == symbol,
                LLMInteraction.created_at >= datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                LLMInteraction.created_at < datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1),
            )
            .all()
        )

        orders = (
            self._db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == symbol,
                OrderRecord.created_at >= datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                OrderRecord.created_at < datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1),
            )
            .all()
        )

        events = (
            self._db.query(TradeEvent)
            .filter(
                TradeEvent.symbol == symbol,
                TradeEvent.created_at >= datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                TradeEvent.created_at < datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1),
            )
            .all()
        )

        snapshots = (
            self._db.query(RuntimeStateSnapshot)
            .filter(
                RuntimeStateSnapshot.symbol == symbol,
                RuntimeStateSnapshot.created_at >= datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc),
                RuntimeStateSnapshot.created_at < datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1),
            )
            .all()
        )

        # Group by date
        days: dict[str, dict[str, Any]] = {}

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

        # Compute error tags and daily PnL
        total_pnl = 0.0
        total_trades = 0
        all_error_tags: set[str] = set()

        for d in sorted(days.keys()):
            day = days[d]
            day["daily_pnl"] = (day["snapshots"][-1].get("daily_pnl", 0) if day["snapshots"] else 0.0)
            day["trade_count"] = len([o for o in day["orders"] if o["status"] in ("FILLED", "PARTIAL_FILLED")])
            day["error_tags"] = self._compute_error_tags(day, symbol)
            all_error_tags.update(day["error_tags"])
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
                len(day["events"]),
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
    def _compute_error_tags(day: dict[str, Any], symbol: str) -> list[str]:
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
            max_daily_pnl = min(s["daily_pnl"] for s in snapshots)
            if max_daily_pnl < -1000:  # 简化阈值
                has_risk_pause = any(e["event_type"] == "RISK_PAUSED" for e in events)
                if not has_risk_pause:
                    tags.add("错过止损")

        return sorted(tags)
