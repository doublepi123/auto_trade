from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy.orm import Session

from app.models import FillSettlement, OrderRecord, TradeEvent
from app.services.trade_event_service import record_trade_event
from app.services.historical_order_completeness_reader import _PRICE_TOLERANCE, _RELATIVE_PRICE_TOLERANCE

ACK_EVENT = "EXTERNAL_ROUND_TRIP_ACKNOWLEDGED"
_LOCAL_SUBMISSION_FIELDS = (
    "submit_started_at", "acknowledged_at", "submit_latency_ms", "ack_latency_ms",
    "decision_at", "decision_bid", "decision_ask", "decision_spread", "decision_spread_bps", "quote_age_ms",
    "exit_cause", "exit_reason",
)
_LOCAL_SUBMISSION_CONTEXT_KEYS = frozenset({
    *_LOCAL_SUBMISSION_FIELDS, "broker_response", "execution_context", "ledger_metadata",
    "execution_signal", "strategy", "config_version", "funded_margin", "funded_margin_evidence",
})


def decimal_text(value: object) -> str:
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("external acknowledgement requires finite positive numbers")
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def instant(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("external acknowledgement timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def canonical_executions(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("v2 requires the complete nonempty execution list on both legs")
    executions = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"trade_id", "quantity", "price", "trade_done_at"}:
            raise ValueError("execution must contain exactly trade_id, quantity, price and trade_done_at")
        trade_id = item["trade_id"]
        if not isinstance(trade_id, str) or not trade_id.strip() or trade_id != trade_id.strip():
            raise ValueError("execution trade_id must be explicit and nonempty")
        executions.append({"trade_id": trade_id, "quantity": decimal_text(item["quantity"]),
                           "price": decimal_text(item["price"]),
                           "trade_done_at": instant(item["trade_done_at"]).isoformat()})
    executions.sort(key=lambda item: (instant(item["trade_done_at"]), item["trade_id"]))
    return executions


def completion_at(leg: Mapping[str, Any]) -> datetime:
    executions = leg.get("executions")
    if executions:
        return max(instant(item["trade_done_at"]) for item in executions)
    return instant(leg["filled_at"])


def canonical_group(identity: str, buy: Mapping[str, Any], sell: Mapping[str, Any], *,
                    schema_version: int = 1) -> dict[str, Any]:
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError("external acknowledgement schema_version must be 1 or 2")
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("current broker identity fingerprint is required")
    legs = []
    trade_ids: set[str] = set()
    for side, raw in (("BUY", buy), ("SELL", sell)):
        symbol = str(raw["symbol"])
        order_id = str(raw["broker_order_id"])
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,31}\.(US|HK)", symbol) or not order_id.strip():
            raise ValueError("explicit symbol and broker order ids are required")
        if raw.get("side", side) != side or raw.get("status", "FILLED") != "FILLED":
            raise ValueError("only fully FILLED BUY then SELL is supported")
        leg: dict[str, Any] = {"broker_order_id": order_id, "symbol": symbol, "side": side, "status": "FILLED",
               "quantity": decimal_text(raw["quantity"]), "price": decimal_text(raw["price"]),
               "submitted_at": instant(raw["submitted_at"]).isoformat(),
               "filled_at": instant(raw["filled_at"]).isoformat()}
        if instant(leg["submitted_at"]) > instant(leg["filled_at"]):
            raise ValueError("fill precedes submission")
        if schema_version == 2:
            executions = canonical_executions(raw.get("executions"))
            for execution in executions:
                if execution["trade_id"] in trade_ids:
                    raise ValueError("execution trade_ids must be unique across the whole round trip")
                trade_ids.add(execution["trade_id"])
                if instant(execution["trade_done_at"]) < instant(leg["submitted_at"]):
                    raise ValueError("execution precedes order submission")
            if instant(executions[0]["trade_done_at"]) != instant(leg["filled_at"]):
                raise ValueError("filled_at must remain the exact first execution timestamp")
            quantity = sum((Decimal(item["quantity"]) for item in executions), start=Decimal(0))
            if quantity != Decimal(leg["quantity"]):
                raise ValueError("execution quantities must cover the full submitted and executed quantity")
            weighted = sum((Decimal(item["quantity"]) * Decimal(item["price"]) for item in executions), start=Decimal(0)) / quantity
            price = Decimal(leg["price"])
            if abs(weighted - price) > max(_PRICE_TOLERANCE, abs(price) * _RELATIVE_PRICE_TOLERANCE):
                raise ValueError("weighted execution price does not match the full order")
            leg["executions"] = executions
        elif raw.get("executions") is not None:
            raise ValueError("execution-list acknowledgement requires schema_version 2")
        legs.append(leg)
    if (legs[0]["broker_order_id"] == legs[1]["broker_order_id"]
            or legs[0]["symbol"] != legs[1]["symbol"] or legs[0]["quantity"] != legs[1]["quantity"]
            or completion_at(legs[0]) >= instant(legs[1]["submitted_at"])):
        raise ValueError("acknowledgement must be an exact independent long round trip")
    return {"schema_version": schema_version, "broker_identity_fingerprint": identity, "buy": legs[0], "sell": legs[1]}


def group_digest(group: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(group, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def row_matches(row: object, leg: Mapping[str, Any]) -> bool:
    try:
        return (str(getattr(row, "broker_order_id")) == leg["broker_order_id"]
                and getattr(row, "symbol") == leg["symbol"] and getattr(row, "side") == leg["side"]
                and getattr(row, "status") == "FILLED"
                and decimal_text(getattr(row, "quantity")) == leg["quantity"]
                and decimal_text(getattr(row, "executed_quantity")) == leg["quantity"]
                and decimal_text(getattr(row, "executed_price")) == leg["price"]
                and ledger_instant(getattr(row, "created_at")) == instant(leg["submitted_at"])
                and ledger_instant(getattr(row, "filled_at")) == instant(leg["filled_at"]))
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return False


def broker_terminal_matches(row: object, leg: Mapping[str, Any], *, schema_version: int) -> bool:
    if schema_version == 1:
        return row_matches(row, leg)
    # BrokerGateway's SDK today adapter may expose ordinary updated_at as
    # filled_at. It is not execution-time evidence. v2 FIRST/LAST are proved
    # separately from the exact official execution list; ledger rows continue
    # to use row_matches with exact FIRST, never a timestamp-range exemption.
    try:
        return (getattr(row, "broker_order_id") == leg["broker_order_id"]
                and getattr(row, "symbol") == leg["symbol"] and getattr(row, "side") == leg["side"]
                and getattr(row, "status") == "FILLED"
                and decimal_text(getattr(row, "quantity")) == leg["quantity"]
                and decimal_text(getattr(row, "executed_quantity")) == leg["quantity"]
                and decimal_text(getattr(row, "executed_price")) == leg["price"]
                and ledger_instant(getattr(row, "created_at")) == instant(leg["submitted_at"]))
    except (ValueError, TypeError, AttributeError, ArithmeticError):
        return False


def ledger_instant(value: object) -> datetime:
    # SQLite's legacy orders DateTime columns return naive UTC. Broker/request
    # observations still go through the strict timezone-aware instant parser.
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return instant(value)


def assert_unowned(db: Session, group: Mapping[str, Any]) -> None:
    ids = [group[side]["broker_order_id"] for side in ("buy", "sell")]
    if db.query(TradeEvent).filter(TradeEvent.event_type == "ORDER_SUBMITTED", TradeEvent.broker_order_id.in_(ids)).first():
        raise ValueError("either leg has ORDER_SUBMITTED evidence")
    if db.query(FillSettlement).filter(FillSettlement.broker_order_id.in_(ids)).first():
        raise ValueError("either leg has local fill settlement evidence")
    for row in db.query(OrderRecord).filter(OrderRecord.broker_order_id.in_(ids)).all():
        if row.pnl_source in {"TRACKED_ENTRY", "BROKER_POSITION"} or row.config_version:
            raise ValueError("either leg has authoritative local booking or strategy evidence")
        if any(getattr(row, field, None) not in (None, "") for field in _LOCAL_SUBMISSION_FIELDS):
            raise ValueError("either leg has local-only submission metadata")
        for field in ("raw_response", "config_snapshot"):
            raw = getattr(row, field, None)
            if not raw:
                continue
            try:
                context = json.loads(raw)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError("either leg has unverifiable submission context") from exc
            if not isinstance(context, dict):
                raise ValueError("either leg has unverifiable submission context")
            if _LOCAL_SUBMISSION_CONTEXT_KEYS.intersection(context):
                raise ValueError("either leg has local-only submission context")


def assert_isolated(db: Session, group: Mapping[str, Any], broker_legs: Sequence[object] = ()) -> None:
    """Raw unfiltered FIFO; never uses authoritative/synthetic accounting lots."""
    rows: dict[str, object] = {}
    for row in db.query(OrderRecord).filter(OrderRecord.symbol == group["buy"]["symbol"]).order_by(OrderRecord.id).all():
        rows[str(row.broker_order_id or f"local:{row.id}")] = row
    for leg in broker_legs:
        key = str(getattr(leg, "broker_order_id"))
        if key in rows and not row_matches(rows[key], group["buy"] if key == group["buy"]["broker_order_id"] else group["sell"]):
            raise ValueError("local terminal facts differ from fresh broker evidence")
        rows[key] = leg
    buy_id, sell_id = (group[side]["broker_order_id"] for side in ("buy", "sell"))
    if buy_id not in rows or sell_id not in rows:
        raise ValueError("both raw FIFO legs must be proved")
    for side in ("buy", "sell"):
        if not row_matches(rows[group[side]["broker_order_id"]], group[side]):
            raise ValueError("terminal facts drifted")
    fills = []
    for key, row in rows.items():
        raw_quantity = getattr(row, "executed_quantity", None)
        if raw_quantity is None and getattr(row, "status", "") == "FILLED":
            raw_quantity = getattr(row, "quantity", None)
        quantity = Decimal(str(raw_quantity or 0))
        status = str(getattr(row, "status", ""))
        if not quantity.is_finite() or quantity < 0 or (status in {"FILLED", "PARTIAL_FILLED"} and quantity == 0):
            raise ValueError("invalid raw FIFO execution quantity")
        if quantity > 0:
            if str(getattr(row, "side", "")) not in {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}:
                raise ValueError("raw FIFO execution side is unknown")
            filled_at = getattr(row, "filled_at", None) or getattr(row, "created_at", None)
            fills.append((ledger_instant(filled_at), key, str(getattr(row, "side")), quantity))
    fills.sort()
    # Ledger filled_at is FIRST execution, not completion. A third order
    # crossing any part of the v2 execution interval invalidates isolation.
    if group["schema_version"] == 2:
        first = instant(group["buy"]["filled_at"])
        last = completion_at(group["sell"])
        if any(first <= at <= last and key not in {buy_id, sell_id} for at, key, _, _ in fills):
            raise ValueError("another fill crosses the complete external execution interval")
    inventory = Decimal(0)
    short_inventory = Decimal(0)
    opened = False
    for _, key, side, quantity in fills:
        if key == buy_id:
            if inventory != 0 or short_inventory != 0:
                raise ValueError("external BUY overlaps existing long inventory")
            opened = True
        elif key == sell_id:
            if not opened or inventory != quantity:
                raise ValueError("external SELL crosses another FIFO lot or leaves a remainder")
            return
        elif opened:
            raise ValueError("another fill crosses the external round trip")
        if side == "BUY":
            inventory += quantity
        elif side == "SELL":
            if quantity > inventory:
                raise ValueError("raw FIFO SELL has unmatched or oversized inventory")
            inventory -= quantity
        elif side == "SELL_SHORT":
            short_inventory += quantity
        elif side == "BUY_TO_COVER":
            if quantity > short_inventory:
                raise ValueError("raw FIFO BUY_TO_COVER has unmatched or oversized inventory")
            short_inventory -= quantity
        else:
            raise ValueError("raw FIFO execution side is unknown")
    raise ValueError("raw FIFO round trip is incomplete")


def validate_event(event: TradeEvent, identity: str | None = None) -> dict[str, Any]:
    payload = json.loads(event.payload_json)
    group = canonical_group(payload["broker_identity_fingerprint"], payload["buy"], payload["sell"],
                            schema_version=payload.get("schema_version"))
    digest = group_digest(group)
    if (type(payload.get("schema_version")) is not int or payload.get("schema_version") not in {1, 2}
            or payload.get("buy") != group["buy"] or payload.get("sell") != group["sell"]
            or event.event_type != ACK_EVENT or event.status != "ACKNOWLEDGED"
            or payload.get("digest") != digest or event.source_event_key != digest
            or event.broker_order_id != group["sell"]["broker_order_id"] or event.symbol != group["sell"]["symbol"]
            or not isinstance(payload.get("actor_hash"), str) or not payload["actor_hash"].strip()
            or not isinstance(payload.get("confirmation_reason"), str) or not payload["confirmation_reason"].strip()
            or len(payload.get("observation_times", [])) != 2):
        raise ValueError("corrupt external acknowledgement")
    first, second = map(instant, payload["observation_times"])
    if (second - first).total_seconds() < 5 or second < completion_at(group["sell"]):
        raise ValueError("external acknowledgement lacks two separated observations")
    if identity is not None and identity != group["broker_identity_fingerprint"]:
        raise ValueError("external acknowledgement broker identity mismatch")
    if group["schema_version"] == 2:
        semantic_start = instant(group["buy"]["submitted_at"])
        semantic_end = completion_at(group["sell"])
        symbol_parameters = {"symbol": group["buy"]["symbol"]}
        history_parameters = {**symbol_parameters, "start_at": str(int(semantic_start.timestamp()) - 1),
                              "end_at": str(int(semantic_end.timestamp()) + 1)}

        def route_identity(path: str, parameters: Mapping[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in parameters.items()):
                raise ValueError("v2 route parameters must be exact query strings")
            return path, tuple(sorted(parameters.items()))

        expected_routes = {
            route_identity("/v1/trade/order/history", history_parameters),
            route_identity("/v1/trade/execution/history", history_parameters),
            route_identity("/v1/trade/order/today", symbol_parameters),
            route_identity("/v1/trade/execution/today", symbol_parameters),
            *(route_identity("/v1/trade/execution/today", {**symbol_parameters, "order_id": group[side]["broker_order_id"]})
              for side in ("buy", "sell")),
        }
        proofs = payload.get("broker_evidence_observations")
        if not isinstance(proofs, list) or len(proofs) != 2:
            raise ValueError("v2 acknowledgement lacks both official target quantity proofs")
        for proof, observation_at in zip(proofs, (first, second), strict=True):
            if (not isinstance(proof, dict) or proof.get("completeness_scope") != "target_quantity_closed"
                    or proof.get("schema_version") != 2 or proof.get("provider") != "longport_official_http_target_quantity_v2"
                    or proof.get("symbol") != group["buy"]["symbol"]
                    or proof.get("orders_has_more") is not False or proof.get("executions_has_more") is not False
                    or proof.get("today_orders_has_more") is not False
                    or (proof.get("today_executions_has_more") is not None and proof.get("today_executions_has_more") is not False)
                    or proof.get("broker_identity_fingerprint") != group["broker_identity_fingerprint"]
                    or proof.get("target_execution_completeness") != "FINAL_FILLED_QUANTITY_CLOSURE"
                    or proof.get("scope") != [group["buy"]["broker_order_id"], group["sell"]["broker_order_id"]]
                    or proof.get("target_query_matches_symbol_query") is not True
                    or "all_endpoints_complete" in proof
                    or proof.get("target_order_ids") != [group["buy"]["broker_order_id"], group["sell"]["broker_order_id"]]):
                raise ValueError("v2 acknowledgement official completeness scope or flags are invalid")
            if instant(proof.get("start_at")) != semantic_start or instant(proof.get("end_at")) != semantic_end:
                raise ValueError("v2 proof semantic window differs from the exact completed round trip")
            routes = proof.get("route_response_digests")
            if (not isinstance(routes, list) or len(routes) != 6
                    or any(not isinstance(route, list) or len(route) != 2 or not isinstance(route[0], str)
                           or not isinstance(route[1], str) or not re.fullmatch(r"[0-9a-f]{64}", route[1]) for route in routes)):
                raise ValueError("v2 acknowledgement lacks raw official route response digests")
            route_digests: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {}
            for url, response_digest in routes:
                parsed = urlsplit(url)
                pairs = parse_qsl(parsed.query, keep_blank_values=True)
                if parsed.scheme or parsed.netloc or parsed.fragment or len(pairs) != len(dict(pairs)):
                    raise ValueError("v2 route URL or duplicate query parameter is invalid")
                route_key = route_identity(parsed.path, dict(pairs))
                if route_key in route_digests:
                    raise ValueError("v2 proof repeats an official route identity")
                route_digests[route_key] = response_digest
            if set(route_digests) != expected_routes:
                raise ValueError("v2 proof must contain exactly the six canonical official route identities")
            raw_routes = proof.get("raw_route_evidence")
            if not isinstance(raw_routes, list) or len(raw_routes) != 6:
                raise ValueError("v2 acknowledgement lacks bounded raw route observations")
            raw_keys: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
            for route in raw_routes:
                if (not isinstance(route, dict) or not isinstance(route.get("path"), str)
                        or not isinstance(route.get("parameters"), dict)
                        or not completion_at(group["sell"]) <= instant(route.get("fetched_at")) <= observation_at):
                    raise ValueError("v2 raw route path, parameters, timestamp or digest is invalid")
                route_key = route_identity(route["path"], route["parameters"])
                if (route_key in raw_keys or route_key not in route_digests
                        or route_digests[route_key] != route.get("digest")):
                    raise ValueError("v2 raw route observations are not a one-to-one canonical route proof")
                raw_keys.add(route_key)
                if route["path"] == "/v1/trade/execution/today":
                    if route.get("has_more") is not None and route.get("has_more") is not False:
                        raise ValueError("v2 today executions flag is not an actual absent/false flag")
                elif route["path"] in {"/v1/trade/order/history", "/v1/trade/execution/history", "/v1/trade/order/today"}:
                    if route.get("has_more") is not False:
                        raise ValueError("v2 order/history pages must retain actual has_more=false")
                else:
                    raise ValueError("v2 raw route is not an approved read-only endpoint")
            if raw_keys != expected_routes:
                raise ValueError("v2 raw route observations omit a required canonical request")
    return group


def replay_exclusions(db: Session, *, identity: str | None = None) -> set[str]:
    """Corruption raises before any fill conversion: never re-credit owner PnL."""
    events = db.query(TradeEvent).filter(TradeEvent.event_type == ACK_EVENT).all()
    excluded: set[str] = set()
    for event in events:
        group = validate_event(event, identity)
        assert_unowned(db, group)
        # BUY may exist only in the durable broker evidence bundle. Validate
        # every retained local row, never invent/import a missing order row.
        for side in ("buy", "sell"):
            leg = group[side]
            for row in db.query(OrderRecord).filter(OrderRecord.broker_order_id == leg["broker_order_id"]).all():
                if not row_matches(row, leg):
                    raise ValueError("external acknowledgement terminal fact drift")
            excluded.add(leg["broker_order_id"])
        from app.core.broker import BrokerOrder

        evidence_legs = [
            BrokerOrder(leg["broker_order_id"], leg["symbol"], leg["side"], Decimal(leg["quantity"]),
                        Decimal(leg["price"]), Decimal(leg["quantity"]), Decimal(leg["price"]),
                        "FILLED", instant(leg["submitted_at"]), instant(leg["filled_at"]))
            for leg in (group["buy"], group["sell"])
        ]
        assert_isolated(db, group, evidence_legs)
    return excluded


def append_ack(db: Session, group: dict[str, Any], *, actor_hash: str, reason: str,
               observations: tuple[datetime, datetime],
               evidence_observations: tuple[dict[str, Any] | None, dict[str, Any] | None] | None = None) -> TradeEvent:
    digest = group_digest(group)
    payload = {**group, "digest": digest, "actor_hash": actor_hash, "confirmation_reason": reason,
               "observation_times": [at.isoformat() for at in observations]}
    if evidence_observations is not None:
        payload["broker_evidence_observations"] = list(evidence_observations)
    event = record_trade_event(db, event_type=ACK_EVENT, symbol=group["buy"]["symbol"],
                               broker_order_id=group["sell"]["broker_order_id"], status="ACKNOWLEDGED",
                               message="owner acknowledged exact external completed round trip",
                               payload=payload)
    event.source_event_key = digest
    return event
