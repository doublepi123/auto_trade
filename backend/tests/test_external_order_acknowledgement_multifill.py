from __future__ import annotations

import copy
import inspect
import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy.orm import Session

from app.models import OrderRecord, TradeEvent
from app.services.daily_pnl_service import DailyPnlService
from app.services.external_order_acknowledgement_service import canonical_group, group_digest, validate_event
from tests.test_external_order_acknowledgement import BUY_FILL, SELL_AT, SELL_FILL, IDENTITY, _leg, _record
from tests.test_external_order_acknowledgement_safety import (
    case, _pair_payloads, _pair_rows,
)
from app.services.historical_order_completeness_reader import LongportHistoricalCompletenessReader
from urllib.parse import parse_qs, urlsplit


def _wire_real_reader(case, monkeypatch, orders, executions):
    from app import runner as runner_module

    monkeypatch.setattr("app.services.historical_order_completeness_reader.datetime", runner_module.datetime)
    class _RouteTransport:
        def __init__(self):
            self.calls = []

        def request(self, method, path):
            self.calls.append((method, path))
            route = urlsplit(path).path
            query = parse_qs(urlsplit(path).query)
            if route == "/v1/trade/order/history":
                lower, upper = int(query["start_at"][0]), int(query["end_at"][0])
                return {**orders, "orders": [row for row in orders["orders"]
                                             if row["order_id"] != case.sell.broker_order_id
                                             and lower < int(row["submitted_at"]) < upper]}
            if route == "/v1/trade/order/today":
                return {**orders, "orders": [row for row in orders["orders"] if row["order_id"] == case.sell.broker_order_id]}
            if route == "/v1/trade/execution/history":
                return {**executions, "trades": [row for row in executions["trades"] if row["order_id"] != case.sell.broker_order_id]}
            if route == "/v1/trade/execution/today":
                rows = [row for row in executions["trades"] if row["order_id"] == case.sell.broker_order_id]
                if "order_id" in query:
                    rows = [row for row in executions["trades"] if row["order_id"] == query["order_id"][0]]
                # Official today execution payload has no has_more field.
                return {"trades": rows}
            raise AssertionError(f"unexpected official route {route}")

    transport = _RouteTransport()
    monkeypatch.setattr("app.services.historical_order_completeness_reader.build_longport_historical_reader_from_env",
                        lambda: LongportHistoricalCompletenessReader(transport, broker_identity_fingerprint=IDENTITY))
    return transport


def _bundle():
    orders, executions = _pair_payloads()
    executions["trades"][1]["quantity"] = "18"
    for index, quantity, seconds in ((2, "180", 50), (3, "2", 55)):
        execution = copy.deepcopy(executions["trades"][1])
        execution.update(trade_id=f"sell-trade-{index}", quantity=quantity,
                         trade_done_at=str(int((SELL_FILL + timedelta(seconds=seconds)).timestamp())))
        executions["trades"].append(execution)
    return orders, executions


def _request(case, executions):
    group = canonical_group(IDENTITY, case.request["buy"], case.request["sell"])
    group["schema_version"] = 2
    for side in ("buy", "sell"):
        leg = group[side]
        leg["executions"] = [
            {"trade_id": execution["trade_id"], "quantity": execution["quantity"],
             "price": execution["price"].rstrip("0").rstrip(".") if "." in execution["price"] else execution["price"],
             "trade_done_at": datetime.fromtimestamp(
                 int(execution["trade_done_at"]), tz=SELL_FILL.tzinfo).isoformat()}
            for execution in executions["trades"] if execution["order_id"] == leg["broker_order_id"]
        ]
    return {**case.request, "schema_version": 2, "buy": group["buy"], "sell": group["sell"],
            "digest": group_digest(group)}


def _attempt(case, request):
    request = copy.deepcopy(request)
    # Baseline is exercised through its supported entry point and REAL reader,
    # not an absent-parameter TypeError: its single-time refusal is behavioral RED.
    if "schema_version" not in inspect.signature(case.runner.acknowledge_external_round_trip).parameters:
        request.pop("schema_version", None)
        request["digest"] = group_digest(canonical_group(IDENTITY, request["buy"], request["sell"]))
    try:
        return case.runner.acknowledge_external_round_trip(**request)
    except (ValueError, RuntimeError) as exc:
        return {"status": "REJECTED", "error": str(exc)}


def test_v2_real_reader_three_sell_executions_ack_keeps_first_timestamp_and_pause(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _bundle()
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    before = case.runner.risk.pause_verification_snapshot()
    first = _attempt(case, request)
    assert first["status"] == "PROOF_PENDING", first
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"
    assert case.runner.risk.pause_verification_snapshot() == before
    assert str(int((SELL_FILL + timedelta(seconds=56)).timestamp())) in transport.calls[0][1]
    assert str(int((case.buy.created_at - timedelta(seconds=1)).timestamp())) in transport.calls[0][1]
    with Session(case.engine) as db:
        row = db.query(OrderRecord).filter(OrderRecord.broker_order_id == case.sell.broker_order_id).one()
        assert row.filled_at is not None
        assert row.filled_at.replace(tzinfo=SELL_FILL.tzinfo) == SELL_FILL
        assert db.query(OrderRecord).count() == 2
        assert db.query(TradeEvent).count() == 1
        payload = json.loads(db.query(TradeEvent).one().payload_json)
        for proof in payload["broker_evidence_observations"]:
            assert proof["completeness_scope"] == "target_quantity_closed"
            assert proof["today_orders_has_more"] is False
            assert proof["today_executions_has_more"] is None
            assert len(proof["route_response_digests"]) == 6
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert result.is_complete and result.realized_pnl == 0


def test_v1_real_reader_multitime_still_rejected(case, monkeypatch):
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    outcome = _attempt(case, case.request)
    assert outcome["status"] == "REJECTED"


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "nan", "wrong_order", "quantity", "price", "nonterminal", "truncated"])
def test_v2_full_execution_evidence_must_match(case, monkeypatch, mutation):
    orders, executions = _bundle()
    request = _request(case, executions)
    if mutation == "missing":
        request["sell"]["executions"].pop()
    elif mutation == "extra":
        extra = copy.deepcopy(request["sell"]["executions"][-1])
        extra["trade_id"] = "extra"
        request["sell"]["executions"].append(extra)
    elif mutation == "duplicate":
        request["sell"]["executions"][1]["trade_id"] = request["buy"]["executions"][0]["trade_id"]
    elif mutation == "nan":
        request["sell"]["executions"][0]["price"] = "NaN"
    elif mutation == "wrong_order":
        executions["trades"][-1]["order_id"] = case.buy.broker_order_id
    elif mutation == "quantity":
        executions["trades"][-1]["quantity"] = "1"
    elif mutation == "price":
        executions["trades"][-1]["price"] = "99"
    elif mutation == "nonterminal":
        orders["orders"][1]["status"] = "PartialFilledStatus"
    else:
        executions["has_more"] = True
    if mutation in {"missing", "extra", "duplicate", "nan"}:
        group = {"schema_version": 2, "broker_identity_fingerprint": IDENTITY,
                 "buy": request["buy"], "sell": request["sell"]}
        request["digest"] = group_digest(group)
    _wire_real_reader(case, monkeypatch, orders, executions)
    outcome = _attempt(case, request)
    assert outcome["status"] == "REJECTED", outcome
    with Session(case.engine) as db:
        assert db.query(TradeEvent).count() == 0


@pytest.mark.parametrize("seconds", [0, 30, 55])
def test_v2_third_local_fill_between_sell_first_and_last_rejected(case, monkeypatch, seconds):
    _pair_rows(case)
    with Session(case.engine) as db:
        at = SELL_FILL + timedelta(seconds=seconds)
        _record(db, _leg("crossing-buy", "BUY", "37", at, at))
        db.commit()
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    assert _attempt(case, _request(case, executions))["status"] == "REJECTED"


def test_v2_buy_not_completed_before_sell_submission_rejected(case, monkeypatch):
    orders, executions = _bundle()
    executions["trades"][0]["quantity"] = "199"
    extra = copy.deepcopy(executions["trades"][0])
    extra.update(trade_id="late-buy", quantity="1", trade_done_at=str(int(SELL_AT.timestamp())))
    executions["trades"].append(extra)
    _wire_real_reader(case, monkeypatch, orders, executions)
    assert _attempt(case, _request(case, executions))["status"] == "REJECTED"


def test_v2_canonical_digest_sorted_and_event_execution_drift_fail_closed(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    group = canonical_group(IDENTITY, request["buy"], request["sell"], schema_version=2)
    request["sell"]["executions"].reverse()
    assert group_digest(canonical_group(IDENTITY, request["buy"], request["sell"], schema_version=2)) == group_digest(group)
    assert _attempt(case, request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"
    with Session(case.engine) as db:
        event = db.query(TradeEvent).one()
        payload = json.loads(event.payload_json)
        payload["sell"]["executions"][-1]["quantity"] = "1"
        event.payload_json = json.dumps(payload)
        db.commit()
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert not result.is_complete and result.realized_pnl == 0
        with pytest.raises(ValueError):
            DailyPnlService(db).pair_round_trips_with_issues()


def test_v2_authenticated_post_contract(case, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import trade
    from app.config import settings

    class _FakeAudit:
        def record(self, *args, **kwargs):
            pass

    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    request.pop("actor_hash")
    for side in ("buy", "sell"):
        request[side].pop("side")
        request[side].pop("status")
    monkeypatch.setattr(trade, "get_runner", lambda: case.runner)
    monkeypatch.setattr(settings, "api_key", "owner-secret")
    app = FastAPI()
    app.include_router(trade.router)
    app.dependency_overrides[trade.get_audit_logger] = _FakeAudit
    request = json.loads(json.dumps(request, default=str))
    with TestClient(app) as client:
        headers = {"x-api-key": "owner-secret"}
        first = client.post("/api/control/acknowledge-external-round-trip", json=request, headers=headers)
        assert first.status_code == 409 and first.json()["detail"]["status"] == "PROOF_PENDING"
        case.clock[0] += 5
        second = client.post("/api/control/acknowledge-external-round-trip", json=request, headers=headers)
        assert second.status_code == 200 and second.json()["status"] == "ACKNOWLEDGED"


def test_v2_sdk_updated_time_is_not_ledger_first_execution_time(case, monkeypatch):
    _pair_rows(case)
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    sdk_sell = copy.copy(case.sell)
    # BrokerGateway's today adapter falls back to ordinary updated_at when
    # the SDK has no filled_at. Exact FIRST must come from official executions.
    sdk_sell.filled_at = SELL_FILL + timedelta(seconds=55)
    case.broker.orders = [sdk_sell]
    request = _request(case, executions)
    first = _attempt(case, request)
    assert first["status"] == "PROOF_PENDING", first
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"
    monkeypatch.setattr(case.runner, "_sync_risk_from_order_ledger", lambda: False)
    case.runner.sync_today_orders_from_broker(force=True)
    assert case.runner._last_order_sync_succeeded
    with Session(case.engine) as db:
        row = db.query(OrderRecord).filter(OrderRecord.broker_order_id == case.sell.broker_order_id).one()
        assert row.filled_at is not None and row.filled_at.replace(tzinfo=SELL_FILL.tzinfo) == SELL_FILL


@pytest.mark.parametrize("mutation", ["today_orders_missing_flag", "today_orders_truncated", "today_duplicate",
                                      "directed_missing", "directed_duplicate", "execution_conflict", "order_conflict",
                                      "third_partial", "cancel_with_execution", "wrong_side"])
def test_target_union_rejects_incomplete_conflicting_or_crossing_evidence(case, monkeypatch, mutation):
    orders, executions = _bundle()
    request = _request(case, executions)
    if mutation == "wrong_side":
        executions["trades"][1]["side"] = "Buy"
    if mutation in {"third_partial", "cancel_with_execution"}:
        at = SELL_FILL + timedelta(seconds=30)
        third = copy.deepcopy(orders["orders"][1])
        third.update(order_id="third-partial", status="PartialFilledStatus" if mutation == "third_partial" else "CanceledStatus",
                     quantity="200", executed_quantity="1", submitted_at=str(int(SELL_AT.timestamp())))
        orders["orders"].append(third)
        third_execution = copy.deepcopy(executions["trades"][-1])
        third_execution.update(order_id="third-partial", trade_id="third-trade", quantity="1",
                               trade_done_at=str(int(at.timestamp())))
        executions["trades"].append(third_execution)
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    original = transport.request

    def changed(method, path):
        payload: dict[str, Any] = copy.deepcopy(original(method, path))
        route, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if route == "/v1/trade/order/today":
            if mutation == "today_orders_missing_flag":
                payload.pop("has_more")
            elif mutation == "today_orders_truncated":
                payload["has_more"] = True
            elif mutation == "order_conflict":
                extra = copy.deepcopy(orders["orders"][0])
                extra["price"] = "99"
                payload["orders"].append(extra)
        if route == "/v1/trade/execution/today":
            if mutation == "today_duplicate" and "order_id" not in query:
                payload["trades"].append(copy.deepcopy(payload["trades"][0]))
            elif mutation == "directed_missing" and query.get("order_id") == [case.sell.broker_order_id]:
                payload["trades"].pop()
            elif mutation == "directed_duplicate" and query.get("order_id") == [case.sell.broker_order_id]:
                payload["trades"].append(copy.deepcopy(payload["trades"][0]))
            elif mutation == "execution_conflict" and "order_id" not in query:
                extra = copy.deepcopy(executions["trades"][0])
                extra["price"] = "99"
                payload["trades"].append(extra)
        return payload

    monkeypatch.setattr(transport, "request", changed)
    outcome = _attempt(case, request)
    assert outcome["status"] == "REJECTED", outcome
    assert case.runner._external_ack_proof is None
    with Session(case.engine) as db:
        assert db.query(TradeEvent).count() == 0


def test_target_union_identical_cross_endpoint_duplicates_deduplicated(case, monkeypatch):
    orders, executions = _bundle()
    request = _request(case, executions)
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    original = transport.request

    def duplicated_across_routes(method, path):
        payload = copy.deepcopy(original(method, path))
        route, query = urlsplit(path).path, parse_qs(urlsplit(path).query)
        if route == "/v1/trade/order/today":
            payload["orders"].append(copy.deepcopy(orders["orders"][0]))
        if route == "/v1/trade/execution/today" and "order_id" not in query:
            payload["trades"].append(copy.deepcopy(executions["trades"][0]))
        return payload

    monkeypatch.setattr(transport, "request", duplicated_across_routes)
    assert _attempt(case, request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"


def test_target_union_zero_execution_cancellations_allowed(case, monkeypatch):
    orders, executions = _bundle()
    cancelled = copy.deepcopy(orders["orders"][1])
    cancelled.update(order_id="cancel-zero", status="CanceledStatus", executed_quantity="0", executed_price="0")
    orders["orders"].append(cancelled)
    _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    assert _attempt(case, request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"


def test_v2_proof_explicitly_records_bounded_quantity_closure_and_raw_routes(case, monkeypatch):
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    assert _attempt(case, request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"
    with Session(case.engine) as db:
        payload = json.loads(db.query(TradeEvent).one().payload_json)
        for proof in payload["broker_evidence_observations"]:
            assert proof.get("target_execution_completeness") == "FINAL_FILLED_QUANTITY_CLOSURE"
            assert proof.get("scope") == [case.buy.broker_order_id, case.sell.broker_order_id]
            assert proof.get("target_query_matches_symbol_query") is True
            assert "all_endpoints_complete" not in proof
            routes = proof.get("raw_route_evidence", [])
            assert len(routes) == 6
            for route in routes:
                assert route.get("fetched_at") and route.get("parameters") and len(route.get("digest", "")) == 64
                if route["path"].endswith("/execution/today"):
                    assert route["has_more"] is None
                else:
                    assert route["has_more"] is False


def test_recorded_official_18_180_2_trade_ids_and_exclusive_boundary(case, monkeypatch):
    orders, executions = _bundle()
    actual_ids = {"200": "0001390d.6ac81b53.01.01", "18": "0000f9bf.6ac83f23.01.01",
                  "180": "0000f9bf.6ac83f4d.01.01", "2": "0000f9bf.6ac83f5f.01.01"}
    for execution in executions["trades"]:
        execution["trade_id"] = actual_ids[execution["quantity"]]
    _pair_rows(case)
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    first = _attempt(case, request)
    assert first["status"] == "PROOF_PENDING", first
    case.clock[0] += 5
    second = _attempt(case, request)
    assert second["status"] == "ACKNOWLEDGED", second
    with Session(case.engine) as db:
        payload = json.loads(db.query(TradeEvent).one().payload_json)
        assert [item["quantity"] for item in payload["sell"]["executions"]] == ["18", "180", "2"]
        assert [item["trade_id"] for item in payload["sell"]["executions"]] == [actual_ids["18"], actual_ids["180"], actual_ids["2"]]
        assert payload["broker_evidence_observations"][0]["target_execution_completeness"] == "FINAL_FILLED_QUANTITY_CLOSURE"
        assert DailyPnlService(db).calculate(trade_day=SELL_FILL.date()).realized_pnl == 0
    history_params = parse_qs(urlsplit(transport.calls[0][1]).query)
    assert int(history_params["start_at"][0]) == int(case.buy.created_at.timestamp()) - 1
    assert int(history_params["end_at"][0]) == int((SELL_FILL + timedelta(seconds=55)).timestamp()) + 1


def test_directed_today_buy_returns_historical_buy_matching_full_symbol_union(case, monkeypatch):
    orders, executions = _bundle()
    transport = _wire_real_reader(case, monkeypatch, orders, executions)
    original = transport.request

    def real_directed_buy(method, path):
        query = parse_qs(urlsplit(path).query)
        if urlsplit(path).path == "/v1/trade/execution/today" and query.get("order_id") == [case.buy.broker_order_id]:
            transport.calls.append((method, path))
            return {"trades": [copy.deepcopy(executions["trades"][0])]}
        return original(method, path)

    monkeypatch.setattr(transport, "request", real_directed_buy)
    request = _request(case, executions)
    first = _attempt(case, request)
    assert first["status"] == "PROOF_PENDING", first
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"


@pytest.mark.parametrize("mutation", ["six_same_history", "duplicate_sell_target", "wrong_symbol",
                                      "wrong_order_id", "wrong_history_boundary", "wrong_proof_start", "wrong_proof_end"])
def test_v2_durable_proof_requires_exact_six_route_bijection(case, monkeypatch, mutation):
    _pair_rows(case)
    orders, executions = _bundle()
    _wire_real_reader(case, monkeypatch, orders, executions)
    request = _request(case, executions)
    assert _attempt(case, request)["status"] == "PROOF_PENDING"
    case.clock[0] += 5
    assert _attempt(case, request)["status"] == "ACKNOWLEDGED"
    with Session(case.engine) as db:
        event = db.query(TradeEvent).one()
        payload = json.loads(event.payload_json)
        original_digest = payload["digest"]
        for proof in payload["broker_evidence_observations"]:
            routes, records = proof["route_response_digests"], proof["raw_route_evidence"]
            if mutation == "six_same_history":
                proof["route_response_digests"] = [copy.deepcopy(routes[0]) for _ in range(6)]
                proof["raw_route_evidence"] = [copy.deepcopy(records[0]) for _ in range(6)]
            elif mutation == "duplicate_sell_target":
                routes[4], records[4] = copy.deepcopy(routes[5]), copy.deepcopy(records[5])
            elif mutation in {"wrong_symbol", "wrong_order_id", "wrong_history_boundary"}:
                index = 4 if mutation == "wrong_order_id" else 0
                parameters = records[index]["parameters"]
                if mutation == "wrong_symbol":
                    parameters["symbol"] = "OTHER.US"
                elif mutation == "wrong_order_id":
                    parameters["order_id"] = "OTHER-ID"
                else:
                    parameters["start_at"] = str(int(parameters["start_at"]) + 1)
                routes[index][0] = records[index]["path"] + "?" + urlencode(parameters)
            elif mutation == "wrong_proof_start":
                proof["start_at"] = (case.buy.created_at - timedelta(seconds=1)).isoformat()
            else:
                proof["end_at"] = (SELL_FILL + timedelta(seconds=56)).isoformat()
        assert payload["digest"] == original_digest
        event.payload_json = json.dumps(payload)
        db.commit()
        with pytest.raises(ValueError):
            validate_event(event, IDENTITY)
        result = DailyPnlService(db).calculate(trade_day=SELL_FILL.date())
        assert not result.is_complete and result.realized_pnl == 0
