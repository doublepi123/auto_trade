from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from decimal import Decimal
from app.platform.api import router as platform_router
from app.platform.portfolio_api import router as portfolio_router
from app.platform.registry import get_default_registry
from app.platform.runner import PlatformRunner
from app.services.strategy_service import StrategyService
from app.database import SessionLocal
from app.api.backtest import router as backtest_router
from app.api.audit_pack import router as audit_pack_router
from app.api.audit_log import router as audit_log_router
from app.api.calendar import router as calendar_router
from app.api.trade_notes import router as trade_notes_router
from app.api.trades import router as trades_router
from app.api.equity import router as equity_router
from app.api.pnl import router as pnl_router
from app.api.positions import router as positions_router
from app.api.alert_rules import router as alert_rules_router
from app.api.alert_rules import alert_firings_router
from app.api.strategy_presets import router as strategy_presets_router
from app.api.risk import router as risk_router
from app.api.broker import router as broker_router
from app.api.llm_interactions import router as llm_interactions_router
from app.api.llm_usage import router as llm_usage_router
from app.api.notifications import router as notifications_router
from app.api.credentials import router as credentials_router
from app.api.experiments import router as experiments_router
from app.api.metrics import router as metrics_router
from app.api.opening_momentum_shadow import (
    router as opening_momentum_shadow_router,
)
from app.api.indicators import router as indicators_router
from app.api.performance import router as performance_router
from app.api.llm_advisor import router as llm_advisor_router
from app.api.reports import router as reports_router
from app.api.review import router as review_router
from app.api.strategy import router as strategy_router
from app.api.strategy_shadow import router as strategy_shadow_router
from app.api.strategy_experiments import router as strategy_experiments_router
from app.api.trade import router as trade_router
from app.api.universe import router as universe_router
from app.api.watchlist import router as watchlist_router
from app.api.watchlist_quant_v6 import router as watchlist_quant_v6_router
from app.api.ws import router as ws_router
from app.api.ws import manager as ws_manager
from app.api.signal_consensus import router as signal_consensus_router
from app.api.universe_explainer import router as universe_explainer_router
from app.api.risk_timeline import router as risk_timeline_router
from app.api.platform_catalog import router as platform_catalog_router
from app.api.attribution import router as attribution_router
from app.api.regime import router as regime_router
from app.api.drawdown_analysis import router as drawdown_analysis_router
from app.api.strategy_health import router as strategy_health_router
from app.api.execution_quality import router as execution_quality_router
from app.api.decision_replay import router as decision_replay_router
from app.api.lookahead_analysis import router as lookahead_analysis_router
from app.api.monte_carlo import router as monte_carlo_router
from app.api.correlation import router as correlation_router
from app.api.kelly import router as kelly_router
from app.api.streaks import router as streaks_router
from app.api.time_performance import router as time_performance_router
from app.api.rolling_metrics import router as rolling_metrics_router
from app.api.recovery import router as recovery_router
from app.api.benchmark import router as benchmark_router
from app.api.tag_analytics import router as tag_analytics_router
from app.api.risk_score import router as risk_score_router
from app.api.holding_time import router as holding_time_router
from app.api.distribution_shape import router as distribution_shape_router
from app.api.trade_frequency import router as trade_frequency_router
from app.api.profit_factor import router as profit_factor_router
from app.api.concentration import router as concentration_router
from app.api.autocorrelation import router as autocorrelation_router
from app.api.size_impact import router as size_impact_router
from app.api.return_calendar import router as return_calendar_router
from app.api.edge_quality import router as edge_quality_router
from app.api.decay_detection import router as decay_detection_router
from app.api.rolling_var import router as rolling_var_router
from app.api.asymmetry import router as asymmetry_router
from app.api.capital_efficiency import router as capital_efficiency_router
from app.api.intraday_seasonality import router as intraday_seasonality_router
from app.api.drawdown_duration import router as drawdown_duration_router
from app.api.prediction_score import router as prediction_score_router
from app.api.regime_sensitivity import router as regime_sensitivity_router
from app.api.robustness import router as robustness_router
from app.api.milestones import router as milestones_router
from app.api.momentum_ranking import router as momentum_ranking_router
from app.api.fee_drag import router as fee_drag_router
from app.api.exit_efficiency import router as exit_efficiency_router
from app.api.skip_analytics import router as skip_analytics_router
from app.api.r_multiples import router as r_multiples_router
from app.api.profit_concentration import router as profit_concentration_router
from app.api.scratch_analysis import router as scratch_analysis_router
from app.api.reentry_analysis import router as reentry_analysis_router
from app.api.first_trade import router as first_trade_router
from app.api.loss_containment import router as loss_containment_router
from app.api.daily_consistency import router as daily_consistency_router
from app.api.database_health import router as database_health_router
from app.config import settings
from app.database import init_db
from app.runner import get_runner
from app.services.interval_application_service import IntervalApplicationService
from app.services.llm_symbol_state_service import LLMSymbolStateService
from app.services.trade_event_service import record_trade_event
from app import __version__ as APP_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("auto_trade.main")
_last_llm_trigger_price: float = 0.0
_last_llm_trigger_price_by_symbol: dict[str, float] = {}
_llm_last_analysis_at_by_symbol: dict[str, datetime] = {}
_llm_analysis_timestamps: list[float] = []
_llm_analysis_lock = asyncio.Lock()
_report_schedule_lock = asyncio.Lock()
_alert_rules_lock = asyncio.Lock()
_strategy_v2_shadow_lock = asyncio.Lock()
_opening_momentum_shadow_lock = asyncio.Lock()
_universe_selection_lock = asyncio.Lock()
_watchlist_quant_lock = asyncio.Lock()
_watchlist_quant_v6_evaluation_lock = asyncio.Lock()
_llm_globals_lock = threading.Lock()
_watchlist_quant_sync_lock = threading.Lock()
_watchlist_quant_v6_evaluation_sync_lock = threading.Lock()
_watchlist_quant_v6_evaluation_stop_event = threading.Event()
_WATCHLIST_QUANT_POLL_SECONDS = 60
_WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS = 120
_OPENING_MOMENTUM_POLL_SECONDS = 15
_OPENING_MOMENTUM_PRIORITY_POLL_SECONDS = 5
_LLM_SECONDARY_ACTION_PRIORITY = {
    "CANDIDATE": 0,
    "WATCH": 1,
}
_LLM_QUANT_GATE_SKIP_REASON = (
    "fresh quant evidence is not actionable for secondary LLM analysis"
)


def _opening_execution_priority_window(
    now: datetime | None = None,
) -> bool:
    """Reserve market-data and DB capacity for the causal opening entry."""
    from app.services.opening_momentum_execution_service import (
        opening_execution_reservation_window,
    )

    return opening_execution_reservation_window(now)


def _opening_momentum_poll_seconds(
    now: datetime | None = None,
) -> int:
    return (
        _OPENING_MOMENTUM_PRIORITY_POLL_SECONDS
        if _opening_execution_priority_window(now)
        else _OPENING_MOMENTUM_POLL_SECONDS
    )


def _price_drift_pct(current_price: float, last_price: float) -> float:
    """Return percentage price drift between current and last reference price."""
    if last_price <= 0 or current_price <= 0:
        return 0.0
    return abs(current_price - last_price) / last_price * 100


def _should_run_llm_analysis(
    current_price: float,
    last_trigger_price: float,
    threshold_pct: float,
    last_analysis_at: datetime | None,
    interval_minutes: int,
    now: datetime,
) -> tuple[bool, bool]:
    """Return (time_gate_passed, volatility_triggered)."""
    time_gate_passed = False
    if last_analysis_at is not None:
        if last_analysis_at.tzinfo is None:
            last_analysis_at = last_analysis_at.replace(tzinfo=timezone.utc)
        if now - last_analysis_at >= timedelta(minutes=interval_minutes):
            time_gate_passed = True
    else:
        time_gate_passed = True

    volatility_triggered = False
    drift = _price_drift_pct(current_price, last_trigger_price)
    if drift >= threshold_pct:
        volatility_triggered = True

    return time_gate_passed, volatility_triggered


def _prune_llm_analysis_timestamps(now_monotonic: float) -> int:
    cutoff = now_monotonic - 3600.0
    global _llm_analysis_timestamps
    _llm_analysis_timestamps = [ts for ts in _llm_analysis_timestamps if ts >= cutoff]
    return len(_llm_analysis_timestamps)


def _prune_llm_per_symbol_caches() -> int:
    """Drop per-symbol cache entries for symbols the runner no longer tracks.

    Called once at the top of every ``_llm_analysis_tick``. Returns the
    number of entries removed across the two module-level dicts.
    """
    from app.runner import get_runner

    runner = get_runner()
    # Symbols the runner currently cares about (either primary or in the
    # symbol_runtimes dict). Lazy-created rts for unknown symbols are not
    # queried here so they accumulate in the dict; the next sync of the
    # watchlist will evict them.
    known: set[str] = set()
    engine = getattr(runner, "engine", None)
    primary = ""
    if engine is not None:
        params = getattr(engine, "params", None)
        if params is not None:
            primary = getattr(params, "symbol", "") or ""
    if primary:
        known.add(primary)
    known.update(getattr(runner, "_symbol_runtimes", {}).keys())

    removed = 0
    with _llm_globals_lock:
        for stale in [k for k in _last_llm_trigger_price_by_symbol if k not in known]:
            del _last_llm_trigger_price_by_symbol[stale]
            removed += 1
        for stale in [k for k in _llm_last_analysis_at_by_symbol if k not in known]:
            del _llm_last_analysis_at_by_symbol[stale]
            removed += 1
    if removed:
        logger.debug("pruned %d stale LLM per-symbol cache entries", removed)
    return removed


def _llm_runtime_targets(runner: Any, primary_symbol: str, primary_market: str) -> list[tuple[str, str, Any, bool]]:
    runtimes = dict(getattr(runner, "_symbol_runtimes", {}))
    targets: list[tuple[str, str, Any, bool]] = []
    seen: set[str] = set()
    if primary_symbol:
        primary_runtime = runtimes.get(primary_symbol)
        primary_engine = primary_runtime.engine if primary_runtime is not None else runner.engine
        targets.append((primary_symbol, primary_market, primary_engine, True))
        seen.add(primary_symbol)
    for symbol, runtime in runtimes.items():
        if not symbol or symbol in seen:
            continue
        targets.append((symbol, runtime.market, runtime.engine, False))
    return targets


def _llm_target_last_analysis_at(
    state: Any,
) -> datetime:
    value = getattr(state, "last_analysis_at", None)
    if not isinstance(value, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _llm_quant_score_is_fresh(score: Any, now: datetime) -> bool:
    expires_at = getattr(score, "expires_at", None)
    if not isinstance(expires_at, datetime):
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at > now


def _prioritize_llm_runtime_targets(
    targets: list[tuple[str, str, Any, bool]],
    *,
    quant_scores: dict[str, Any],
    schedule_states: dict[str, Any],
    now: datetime,
) -> tuple[
    list[tuple[str, str, Any, bool]],
    list[tuple[str, str, Any, bool]],
]:
    """Prioritize actionable secondary research without delaying primary."""
    primary_targets: list[tuple[str, str, Any, bool]] = []
    secondary_targets: list[
        tuple[
            tuple[int, datetime, float, str, int],
            tuple[str, str, Any, bool],
        ]
    ] = []
    excluded_targets: list[tuple[str, str, Any, bool]] = []
    for target_index, target in enumerate(targets):
        symbol, _market, _engine, is_primary = target
        if is_primary:
            primary_targets.append(target)
            continue

        quant_score = quant_scores.get(symbol)
        action = ""
        score_value = -1.0
        if (
            quant_score is not None
            and _llm_quant_score_is_fresh(quant_score, now)
        ):
            action = str(
                getattr(quant_score, "recommended_action", "")
            ).upper()
            try:
                score_value = float(getattr(quant_score, "score", -1.0))
            except (TypeError, ValueError):
                score_value = -1.0
            if action == "AVOID":
                excluded_targets.append(target)
                continue

        action_priority = _LLM_SECONDARY_ACTION_PRIORITY.get(action, 2)
        secondary_targets.append(
            (
                (
                    action_priority,
                    _llm_target_last_analysis_at(
                        schedule_states.get(symbol)
                    ),
                    -score_value,
                    symbol if action else "",
                    target_index,
                ),
                target,
            )
        )

    secondary_targets.sort(key=lambda item: item[0])
    return (
        [
            *primary_targets,
            *(target for _priority, target in secondary_targets),
        ],
        excluded_targets,
    )


def _recent_price_context_for_target(runtime_engine: Any, runtime: Any | None, symbol: str) -> list[dict[str, Any]]:
    entries = list(getattr(runtime, "recent_quotes", []) or [])
    if not entries and getattr(runtime_engine, "last_price", 0.0) > 0:
        return [
            {
                "symbol": symbol,
                "last_price": float(runtime_engine.last_price),
                "bid": float(runtime_engine.last_price),
                "ask": float(runtime_engine.last_price),
                "timestamp": "",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    result: list[dict[str, Any]] = []
    for item in entries:
        observed_at = item.get("observed_at")
        result.append(
            {
                "symbol": item.get("symbol", symbol),
                "last_price": item.get("last_price", 0.0),
                "bid": item.get("bid", 0.0),
                "ask": item.get("ask", 0.0),
                "timestamp": item.get("timestamp") or "",
                "observed_at": observed_at.isoformat() if hasattr(observed_at, "isoformat") else "",
            }
        )
    return result


def _collect_llm_contexts(
    symbol: str,
    market: str,
    current_price: float,
    short_selling: bool,
) -> tuple[dict[str, float | str], dict[str, Any]]:
    from app.api.llm_advisor import _account_context, _position_context

    position_context = _position_context(symbol, current_price)
    account_context = _account_context(symbol, market, current_price, short_selling)
    return position_context, account_context


async def _ws_cleanup_task() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await ws_manager.cleanup_stale()
        except Exception:
            logger.exception("WebSocket cleanup failed")


async def _llm_analysis_tick() -> None:
    if _opening_execution_priority_window():
        logger.debug("LLM analysis deferred during opening execution priority window")
        return
    from app.database import SessionLocal
    from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
    from app.services.strategy_service import StrategyService
    from app.runner import get_runner
    global _last_llm_trigger_price

    # Periodically prune the per-symbol LLM caches so they cannot grow
    # unboundedly as the watchlist churns. We keep the most recent
    # entry for every symbol the runner currently knows about.
    _prune_llm_per_symbol_caches()

    db = SessionLocal()
    try:
        svc = StrategyService(db)
        config = svc.get_config()
        if not config.auto_interval_enabled or not config.symbol:
            return

        runner = get_runner()
        now = datetime.now(timezone.utc)
        interval_minutes = config.llm_interval_minutes or settings.llm_interval_cron_minutes
        state_svc = LLMSymbolStateService(db)
        used_this_hour = state_svc.count_analyses_last_hour(now)
        remaining_hour_budget = max(0, settings.llm_max_analyses_per_hour - used_this_hour)
        if remaining_hour_budget <= 0:
            logger.info("LLM analysis skipped: hourly budget exhausted")
            return

        raw_targets = _llm_runtime_targets(
            runner,
            config.symbol,
            config.market,
        )
        if not raw_targets:
            return
        schedule_states: dict[str, Any] = {}
        for symbol, market, _engine, _is_primary in raw_targets:
            schedule_states[symbol] = state_svc.get_state(symbol, market)
        db.commit()

        from app.services.watchlist_quant_service import (
            list_latest_current_quant_scores,
        )

        quant_scores = {
            row.symbol: row
            for row in list_latest_current_quant_scores(db)
        }
        targets, quant_excluded_targets = (
            _prioritize_llm_runtime_targets(
                raw_targets,
                quant_scores=quant_scores,
                schedule_states=schedule_states,
                now=now,
            )
        )
        quant_skip_changed = False
        for symbol, market, _engine, _is_primary in (
            quant_excluded_targets
        ):
            state = schedule_states[symbol]
            if (
                getattr(state, "last_status", "") == "SKIPPED"
                and getattr(state, "last_skip_reason", "")
                == _LLM_QUANT_GATE_SKIP_REASON
            ):
                continue
            state_svc.record_skip(
                symbol,
                market,
                _LLM_QUANT_GATE_SKIP_REASON,
                next_analysis_at=None,
            )
            quant_skip_changed = True
        if quant_skip_changed:
            db.commit()
        if quant_excluded_targets:
            logger.info(
                "LLM secondary quant gate excluded %d/%d targets",
                len(quant_excluded_targets),
                max(0, len(raw_targets) - 1),
            )

        cycle_budget = min(settings.llm_max_symbols_per_cycle, remaining_hour_budget)
        attempted_count = 0

        from app.api.llm_advisor import _interval_reference_quantity
        from app.services.llm_interaction_service import (
            LLMInteractionService,
            build_order_policy_outcome,
        )

        advisor = LLMAdvisorService(broker=runner.broker)
        for symbol, market, engine, is_primary in targets:
            try:
                symbol_state = state_svc.get_state(symbol, market)
                # The loop can replace its SQLAlchemy session after a prior
                # symbol fails, so resolve state from the current session.
                db.commit()
                symbol_last_analysis_at = symbol_state.last_analysis_at
                symbol_next_analysis_at = symbol_state.next_analysis_at
                symbol_last_status = getattr(symbol_state, "last_status", "")
                if symbol_next_analysis_at is not None:
                    if symbol_next_analysis_at.tzinfo is None:
                        symbol_next_analysis_at = symbol_next_analysis_at.replace(
                            tzinfo=timezone.utc
                        )
                    else:
                        symbol_next_analysis_at = symbol_next_analysis_at.astimezone(
                            timezone.utc
                        )
                if (
                    symbol_last_status == "FAILED"
                    and symbol_next_analysis_at is not None
                    and symbol_next_analysis_at > now
                ):
                    logger.info(
                        "LLM analysis backoff active for %s until %s",
                        symbol,
                        symbol_next_analysis_at.isoformat(),
                    )
                    continue

                if attempted_count >= cycle_budget:
                    state_svc.record_skip(
                        symbol,
                        market,
                        "cycle budget exhausted",
                        next_analysis_at=None,
                    )
                    db.commit()
                    continue

                runtime = getattr(runner, "_symbol_runtimes", {}).get(symbol)
                params = getattr(engine, "params", config)
                current_price = runner.fresh_market_price(symbol)
                if (
                    current_price is None
                    or not math.isfinite(current_price)
                    or current_price <= 0
                ):
                    state_svc.record_skip(
                        symbol,
                        market,
                        "current market price unavailable",
                        next_analysis_at=None,
                    )
                    db.commit()
                    continue

                with _llm_globals_lock:
                    if is_primary:
                        last_analysis_at = config.llm_last_analysis_at
                        last_trigger_price = _last_llm_trigger_price
                    else:
                        last_analysis_at = symbol_last_analysis_at
                        last_trigger_price = _last_llm_trigger_price_by_symbol.get(symbol, 0.0)

                time_gate_passed, volatility_triggered = _should_run_llm_analysis(
                    current_price=current_price,
                    last_trigger_price=last_trigger_price,
                    threshold_pct=settings.llm_interval_volatility_threshold_pct,
                    last_analysis_at=last_analysis_at,
                    interval_minutes=interval_minutes,
                    now=now,
                )
                if not time_gate_passed and not volatility_triggered:
                    state_svc.record_skip(
                        symbol,
                        market,
                        "interval gate not passed",
                        next_analysis_at=(
                            last_analysis_at + timedelta(minutes=interval_minutes)
                            if last_analysis_at is not None
                            else None
                        ),
                    )
                    db.commit()
                    continue

                if config.trading_session_mode == "RTH_ONLY":
                    from app.core.market_calendar import is_trading_hours

                    if not is_trading_hours(market):
                        state_svc.record_skip(
                            symbol,
                            market,
                            "non-RTH session",
                            next_analysis_at=(
                                last_analysis_at + timedelta(minutes=interval_minutes)
                                if last_analysis_at is not None
                                else None
                            ),
                        )
                        db.commit()
                        continue

                position_context, account_context = await asyncio.to_thread(
                    _collect_llm_contexts,
                    symbol,
                    market,
                    current_price,
                    getattr(params, "short_selling", config.short_selling),
                )

                target_buy_low = getattr(params, "buy_low", config.buy_low)
                target_sell_high = getattr(params, "sell_high", config.sell_high)
                attempted_count += 1
                result = await asyncio.to_thread(
                    advisor.analyze,
                    symbol=symbol,
                    market=market,
                    current_price=current_price,
                    current_buy_low=target_buy_low,
                    current_sell_high=target_sell_high,
                    short_selling=getattr(params, "short_selling", config.short_selling),
                    current_position=str(position_context["side"]),
                    recent_trades=[],
                    position_quantity=float(position_context["quantity"]),
                    position_avg_price=float(position_context["avg_price"]),
                    unrealized_pnl_pct=float(position_context["unrealized_pnl_pct"]),
                    min_profit_amount=float(config.min_profit_amount or 0.0),
                    recent_prices=_recent_price_context_for_target(engine, runtime, symbol),
                    recent_analysis=build_recent_analysis_context(config) if is_primary else None,
                    account_context=account_context,
                    force=True,
                    persist=is_primary,
                )
                analysis_completed_at = datetime.now(timezone.utc)
                if result.get("success"):
                    now_mono = time.monotonic()
                    with _llm_globals_lock:
                        _prune_llm_analysis_timestamps(now_mono)
                        _llm_analysis_timestamps.append(now_mono)
                        _llm_last_analysis_at_by_symbol[symbol] = (
                            analysis_completed_at
                        )

                if result.get("success"):
                    # Only update trigger reference price on successful analysis
                    with _llm_globals_lock:
                        if is_primary:
                            _last_llm_trigger_price = current_price
                        else:
                            _last_llm_trigger_price_by_symbol[symbol] = current_price

                    app_result = {"applied": False, "reason": "secondary symbol analysis does not update primary interval config"}
                    if is_primary:
                        from app.api.strategy import _reload_strategy_after_save

                        app_result = IntervalApplicationService().apply_suggestion(
                            db=db,
                            engine_state=engine.state.value.lower(),
                            current_price=current_price,
                            suggestion={
                                "suggested_buy_low": result.get("suggested_buy_low"),
                                "suggested_sell_high": result.get("suggested_sell_high"),
                                "confidence_score": result.get("confidence_score"),
                            },
                            reference_quantity=_interval_reference_quantity(
                                position_context,
                                account_context,
                                current_price=current_price,
                                trade_service=getattr(runner, "_trade_svc", None),
                            ),
                            position_avg_price=position_context["avg_price"],
                            runtime_reload=_reload_strategy_after_save,
                        )
                    order_result = {"status": "NO_ACTION", "order_id": None}
                    if (
                        is_primary
                        and result.get("order_action")
                        and result.get("order_action") != "NONE"
                    ):
                        order_result = await asyncio.to_thread(runner.execute_llm_order_decision, {**result, "symbol": symbol})
                    elif not is_primary and result.get("order_action") not in {None, "NONE"}:
                        order_result = {
                            "status": "WATCHLIST_READ_ONLY",
                            "order_id": None,
                            "reason": "secondary symbols are analysis-only",
                        }
                    policy_outcome = build_order_policy_outcome(result, order_result)
                    interaction_id = result.get("interaction_id")
                    if interaction_id is not None:
                        LLMInteractionService(db).update_outcome(
                            interaction_id,
                            applied=bool(app_result["applied"]),
                            order_status=order_result.get("status"),
                            order_id=order_result.get("order_id"),
                            policy_outcome=policy_outcome,
                        )
                    record_trade_event(
                        db,
                        event_type="LLM_ANALYSIS",
                        symbol=symbol,
                        status="SUCCESS",
                        message=cast(str, result.get("analysis") or app_result["reason"]),
                        payload={
                            "source": "cron",
                            "interaction_id": interaction_id,
                            "confidence_score": result.get("confidence_score"),
                            "suggested_buy_low": result.get("suggested_buy_low"),
                            "suggested_sell_high": result.get("suggested_sell_high"),
                            "applied": app_result["applied"],
                            "apply_reason": app_result["reason"],
                            "order_action": result.get("order_action"),
                            "order_status": order_result.get("status"),
                            "order_id": order_result.get("order_id"),
                            "policy_outcome": policy_outcome,
                            "symbol_budget_index": attempted_count,
                            "persisted_interval": is_primary,
                        },
                    )
                    state_svc.record_analysis(
                        symbol,
                        market,
                        analyzed_at=analysis_completed_at,
                        next_analysis_at=analysis_completed_at
                        + timedelta(minutes=interval_minutes),
                    )
                    db.commit()
                else:
                    record_trade_event(
                        db,
                        event_type="LLM_ANALYSIS",
                        symbol=symbol,
                        status="FAILED",
                        message=result.get("error", "Unknown error"),
                        payload={
                            "source": "cron",
                            "error": result.get("error", "Unknown error"),
                            "failure_kind": result.get("failure_kind"),
                            "transient": result.get("transient", False),
                            "retry_after_seconds": result.get(
                                "retry_after_seconds", 0
                            ),
                            "symbol_budget_index": attempted_count,
                            "persisted_interval": is_primary,
                        },
                    )
                    failure_at = datetime.now(timezone.utc)
                    retry_after_seconds = int(
                        result.get("retry_after_seconds") or interval_minutes * 60
                    )
                    state_svc.record_failure(
                        symbol,
                        market,
                        result.get("error", "Unknown error"),
                        next_analysis_at=failure_at
                        + timedelta(seconds=max(1, retry_after_seconds)),
                    )
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception("LLM analysis failed for symbol %s; skipping", symbol)
                # Session may have stale state after rollback; close it and
                # create a fresh session for remaining symbols.
                db.close()
                db = SessionLocal()
                svc = StrategyService(db)
                config = svc.get_config()
                state_svc = LLMSymbolStateService(db)
                continue
    finally:
        db.close()


async def _llm_analysis_cron() -> None:
    while True:
        await asyncio.sleep(60)
        async with _llm_analysis_lock:
            try:
                await _llm_analysis_tick()
            except Exception:
                logger.exception("LLM analysis cron failed")


async def _report_schedule_cron() -> None:
    """Periodically push a scheduled performance report (if enabled in config).

    Mirrors the LLM cron pattern: off-thread work, never crashes the loop.
    Read-only analysis + notification dispatch; no order path.
    """
    from app.database import SessionLocal
    from app.services.report_schedule_service import ReportScheduleService

    while True:
        await asyncio.sleep(300)
        async with _report_schedule_lock:
            try:
                runner = get_runner()
                db = SessionLocal()
                try:
                    ReportScheduleService(db).maybe_send(runner)
                finally:
                    db.close()
            except Exception:
                logger.exception("report schedule cron failed")


async def _alert_rules_cron() -> None:
    """Periodically evaluate user-defined alert rules (if any enabled).

    Read-only evaluation + notification dispatch; never touches the order path.
    """
    from app.database import SessionLocal
    from app.services.alert_rule_service import AlertRuleService

    while True:
        await asyncio.sleep(60)
        async with _alert_rules_lock:
            try:
                runner = get_runner()
                db = SessionLocal()
                try:
                    AlertRuleService(db).evaluate(runner)
                finally:
                    db.close()
            except Exception:
                logger.exception("alert rules cron failed")


def _llm_storage_maintenance_tick_sync() -> None:
    """Bound observational storage without running on the event loop."""
    from app.services.llm_interaction_service import LLMInteractionService
    from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

    db = SessionLocal()
    try:
        service = LLMInteractionService(db)
        pruned = service.prune_expired(
            retention_days=settings.llm_interaction_retention_days,
            no_action_retention_days=settings.llm_no_action_retention_days,
            batch_size=settings.llm_storage_maintenance_batch_size,
            max_batches=8,
        )
        compacted = service.compact_oversized_contexts(
            max_bytes=settings.llm_context_snapshot_max_bytes,
            batch_size=min(25, settings.llm_storage_maintenance_batch_size),
            max_rows=settings.llm_storage_maintenance_batch_size,
        )
        shadow_pruned = StrategyV2ShadowService(db).prune_expired_wait_decisions(
            retention_days=settings.strategy_v2_wait_retention_days,
            batch_size=settings.strategy_v2_wait_maintenance_batch_size,
            max_batches=8,
        )
        if pruned.deleted or compacted.compacted:
            logger.info(
                "LLM storage maintenance: deleted=%d delete_batches=%d "
                "compacted=%d inspected=%d compact_batches=%d",
                pruned.deleted,
                pruned.batches,
                compacted.compacted,
                compacted.inspected,
                compacted.batches,
            )
        if shadow_pruned.deleted:
            logger.info(
                "Strategy v2 WAIT storage maintenance: deleted=%d batches=%d",
                shadow_pruned.deleted,
                shadow_pruned.batches,
            )
    finally:
        db.close()


async def _llm_storage_maintenance_cron() -> None:
    """Run bounded observation maintenance; VACUUM remains offline-only."""
    await asyncio.sleep(60)
    while True:
        try:
            await _run_llm_storage_maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("observational storage maintenance failed")
        await asyncio.sleep(settings.llm_storage_maintenance_interval_minutes * 60)


async def _run_llm_storage_maintenance_tick() -> None:
    """Run one bounded maintenance tick and join its thread during shutdown."""
    worker = asyncio.create_task(
        asyncio.to_thread(_llm_storage_maintenance_tick_sync)
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling the asyncio waiter does not stop ``to_thread``. Waiting
        # here keeps SQLite commits from racing application/container teardown.
        try:
            await worker
        except Exception:
            logger.exception(
                "observational storage maintenance failed during shutdown"
            )
        raise


def _strategy_v2_shadow_tick_sync() -> None:
    """Advance every active Strategy v2 simulator without touching orders."""
    if _opening_execution_priority_window():
        logger.debug(
            "Strategy v2 shadow deferred during opening execution priority window"
        )
        return
    from app.core.market_calendar import market_for_symbol
    from app.models import (
        StrategyV2BracketChallengerTrade,
        StrategyV2ForwardRegistration,
        StrategyV2ShadowConfig,
        StrategyV2ShadowTrade,
    )
    from app.services.strategy_v2_shadow_service import (
        FORWARD_CANDIDATE_VERSIONS,
        StrategyV2ShadowService,
    )
    from app.services.strategy_v2_portfolio_service import (
        StrategyV2PortfolioService,
    )
    from app.services.universe_promotion_service import (
        UniversePromotionService,
    )

    db = SessionLocal()
    try:
        strategy = StrategyService(db).get_config()
        targets: dict[str, str] = {}
        if strategy.symbol:
            targets[strategy.symbol] = strategy.market
        enabled_symbols = db.query(StrategyV2ShadowConfig.symbol).filter(
            StrategyV2ShadowConfig.enabled.is_(True)
        ).all()
        open_symbols = db.query(StrategyV2ShadowTrade.symbol).filter(
            StrategyV2ShadowTrade.status == "OPEN"
        ).distinct().all()
        open_bracket_symbols = db.query(
            StrategyV2BracketChallengerTrade.symbol
        ).filter(
            StrategyV2BracketChallengerTrade.status == "OPEN"
        ).distinct().all()
        registered_symbols = db.query(StrategyV2ForwardRegistration.symbol).all()
        for (symbol,) in (
            *enabled_symbols,
            *open_symbols,
            *open_bracket_symbols,
            *registered_symbols,
        ):
            targets.setdefault(symbol, market_for_symbol(symbol))
        if not targets:
            return

        universe_observed_symbols: frozenset[str] = frozenset()
        try:
            universe_observed_symbols = (
                UniversePromotionService(db).get_observed_symbols()
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Strategy v2 universe observation lookup failed"
            )

        portfolio: StrategyV2PortfolioService | None = None
        if (
            settings.strategy_v2_portfolio_shadow_enabled
            and strategy.symbol
        ):
            portfolio = StrategyV2PortfolioService(db)
            try:
                if portfolio.ensure_registrations(
                    primary_symbol=strategy.symbol,
                    now=datetime.now(timezone.utc),
                ):
                    logger.info(
                        "registered Strategy v2 portfolio routing "
                        "variants for primary=%s",
                        strategy.symbol,
                    )
            except Exception:
                db.rollback()
                portfolio = None
                logger.exception(
                    "Strategy v2 portfolio routing registration failed"
                )

        shadow = StrategyV2ShadowService(db, get_runner().broker)
        for symbol, market in sorted(targets.items()):
            try:
                shadow.tick(symbol=symbol, market=market)
            except Exception:
                db.rollback()
                logger.exception("Strategy v2 shadow tick failed for symbol=%s", symbol)
            try:
                if shadow.ensure_universe_forward_registration(
                    symbol,
                    observed_by_universe=(
                        symbol in universe_observed_symbols
                    ),
                ):
                    logger.info(
                        "registered universe forward validation for symbol=%s",
                        symbol,
                    )
            except Exception:
                db.rollback()
                logger.exception(
                    "Strategy v2 universe forward registration failed "
                    "for symbol=%s",
                    symbol,
                )
            for candidate_version in FORWARD_CANDIDATE_VERSIONS:
                try:
                    shadow.collect_forward_validation(
                        symbol=symbol,
                        market=market,
                        candidate_algorithm_version=candidate_version,
                    )
                except Exception:
                    db.rollback()
                    logger.exception(
                        "Strategy v2 forward validation failed for "
                        "symbol=%s candidate=%s",
                        symbol,
                        candidate_version,
                    )
        if portfolio is not None:
            try:
                portfolio.advance(now=datetime.now(timezone.utc))
            except Exception:
                db.rollback()
                logger.exception(
                    "Strategy v2 portfolio routing advance failed"
                )
    finally:
        db.close()


async def _strategy_v2_shadow_cron() -> None:
    """Poll completed minute bars for the isolated Strategy v2 shadow."""
    while True:
        await asyncio.sleep(15)
        async with _strategy_v2_shadow_lock:
            try:
                await asyncio.to_thread(_strategy_v2_shadow_tick_sync)
            except Exception:
                logger.exception("Strategy v2 shadow cron failed")


def _opening_momentum_shadow_tick_sync() -> None:
    """Advance opening-momentum execution and its shadow observers."""
    from app.services.opening_momentum_execution_service import (
        OpeningMomentumExecutionService,
    )
    from app.services.opening_momentum_shadow_service import (
        OpeningMomentumShadowService,
    )

    db = SessionLocal()
    try:
        runner = get_runner()
        try:
            OpeningMomentumExecutionService(
                db,
                runner.broker,
                runner,
            ).tick()
        except Exception:
            db.rollback()
            logger.exception("opening momentum execution tick failed")
        try:
            OpeningMomentumShadowService(
                db,
                runner.broker,
            ).tick()
        except Exception:
            db.rollback()
            logger.exception("opening momentum shadow tick failed")
    finally:
        db.close()


async def _opening_momentum_shadow_cron() -> None:
    """Poll the frozen daily opening-momentum shadow variants."""
    while True:
        await asyncio.sleep(_opening_momentum_poll_seconds())
        async with _opening_momentum_shadow_lock:
            try:
                await asyncio.to_thread(
                    _opening_momentum_shadow_tick_sync
                )
            except Exception:
                logger.exception(
                    "opening momentum shadow cron failed"
                )


def _watchlist_quant_tick_sync() -> None:
    """Refresh due deterministic watchlist scores during open sessions."""
    if not settings.watchlist_quant_auto_score_enabled:
        return
    if _opening_execution_priority_window():
        logger.debug(
            "watchlist quant scoring deferred during opening execution priority window"
        )
        return
    from app.services.watchlist_quant_service import (
        WatchlistQuantService,
        build_quant_observation_plan,
    )

    db = SessionLocal()
    try:
        observation_plan = build_quant_observation_plan(db)
        if not observation_plan.items:
            return
        with _watchlist_quant_sync_lock:
            rows = WatchlistQuantService(
                db,
                get_runner().broker,
            ).score_due_items(
                observation_plan.items,
                refresh_interval_minutes=(
                    settings.watchlist_quant_interval_minutes
                ),
                ttl_minutes=settings.watchlist_quant_score_ttl_minutes,
                max_items=settings.watchlist_quant_batch_size,
                priority_symbols=observation_plan.priority_symbols,
            )
        if rows:
            logger.info(
                "automatic watchlist quant scoring refreshed %d symbols: %s",
                len(rows),
                ", ".join(row.symbol for row in rows),
            )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _run_watchlist_quant_tick() -> None:
    """Join an active scoring worker before application shutdown."""
    worker = asyncio.create_task(
        asyncio.to_thread(_watchlist_quant_tick_sync)
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            logger.exception(
                "watchlist quant scoring failed during shutdown"
            )
        raise


async def _watchlist_quant_cron() -> None:
    """Poll cheaply and fetch market data only for expired RTH scores."""
    if not settings.watchlist_quant_auto_score_enabled:
        return
    logger.info(
        "automatic watchlist quant scoring enabled: "
        "interval=%dm ttl=%dm batch=%d poll=%ds",
        settings.watchlist_quant_interval_minutes,
        settings.watchlist_quant_score_ttl_minutes,
        settings.watchlist_quant_batch_size,
        _WATCHLIST_QUANT_POLL_SECONDS,
    )
    await asyncio.sleep(45)
    while True:
        async with _watchlist_quant_lock:
            try:
                await _run_watchlist_quant_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("automatic watchlist quant scoring failed")
        await asyncio.sleep(_WATCHLIST_QUANT_POLL_SECONDS)


def _watchlist_quant_v6_evaluation_tick_sync() -> None:
    """Publish one quote-only historical cohort without execution authority."""
    if not settings.watchlist_quant_v6_evaluation_enabled:
        return
    from app.services.watchlist_quant_v6_evaluation_service import (
        build_latest_quant_v6_registration_plan,
    )
    from app.services.watchlist_quant_v6_historical_provider import (
        QuantV6HistoricalBarProvider,
    )
    from app.services.watchlist_quant_v6_publication_service import (
        WatchlistQuantV6PublicationService,
    )

    with _watchlist_quant_v6_evaluation_sync_lock:
        # Re-check after waiting for another direct/manual tick. Keeping all
        # imports and resource construction below the enable gate makes the
        # default-disabled path side-effect free.
        if not settings.watchlist_quant_v6_evaluation_enabled:
            return
        plan = build_latest_quant_v6_registration_plan(
            observed_at=datetime.now(timezone.utc),
        )
        provider = QuantV6HistoricalBarProvider(
            cancel_event=_watchlist_quant_v6_evaluation_stop_event,
        )
        try:
            receipt = WatchlistQuantV6PublicationService(
                SessionLocal,
            ).register_provider_evaluate_publish(
                plan=plan,
                provider=provider,
            )
            logger.info(
                "quant-v6 historical publication id=%d registration=%d "
                "members=%d bindings=%d created=%s manifest=%s",
                receipt.publication_id,
                receipt.registration_id,
                len(plan.members),
                receipt.binding_count,
                receipt.created,
                receipt.manifest_sha256,
            )
        finally:
            provider.close()


async def _run_watchlist_quant_v6_evaluation_tick() -> None:
    """Run one historical tick and join its worker during cancellation."""
    if not settings.watchlist_quant_v6_evaluation_enabled:
        return
    _watchlist_quant_v6_evaluation_stop_event.clear()
    worker = asyncio.create_task(
        asyncio.to_thread(_watchlist_quant_v6_evaluation_tick_sync)
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling a to_thread waiter cannot stop quote/SQLite work. Join it
        # so the provider and all publication sessions close before teardown.
        _watchlist_quant_v6_evaluation_stop_event.set()
        try:
            await worker
        except Exception:
            logger.exception(
                "quant-v6 historical evaluation failed during shutdown"
            )
        raise


async def _watchlist_quant_v6_evaluation_cron() -> None:
    """Run the independent, default-disabled historical evidence publisher."""
    if not settings.watchlist_quant_v6_evaluation_enabled:
        return
    logger.info(
        "quant-v6 historical evaluation enabled: interval=%dm retry=%dm",
        settings.watchlist_quant_v6_evaluation_interval_minutes,
        settings.watchlist_quant_v6_evaluation_retry_interval_minutes,
    )
    await asyncio.sleep(_WATCHLIST_QUANT_V6_INITIAL_DELAY_SECONDS)
    while True:
        failed = False
        async with _watchlist_quant_v6_evaluation_lock:
            try:
                await _run_watchlist_quant_v6_evaluation_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                failed = True
                logger.exception(
                    "quant-v6 historical evaluation failed; retrying in %dm",
                    settings.watchlist_quant_v6_evaluation_retry_interval_minutes,
                )
        delay_minutes = (
            settings.watchlist_quant_v6_evaluation_retry_interval_minutes
            if failed
            else settings.watchlist_quant_v6_evaluation_interval_minutes
        )
        await asyncio.sleep(delay_minutes * 60)


def _universe_selection_tick_sync() -> None:
    """Refresh the candidate pool and its short-horizon suitability scores."""
    if not settings.universe_selection_enabled:
        return
    if _opening_execution_priority_window():
        logger.debug(
            "universe selection deferred during opening execution priority window"
        )
        return
    from app.api.universe import build_universe_selection_service
    from app.services.watchlist_quant_service import (
        QuantScoringOutsideRTHError,
        WatchlistQuantService,
        build_quant_observation_plan,
    )

    db = SessionLocal()
    try:
        response = build_universe_selection_service(db).refresh()
        logger.info(
            "universe selection run=%d as_of=%s status=%s coverage=%.3f "
            "selected=%d exploration=%d applied=%s",
            response.run.id,
            response.run.as_of_date,
            response.run.status,
            response.run.coverage_ratio,
            response.run.selected_count,
            len(response.exploration_symbols),
            response.applied,
        )
        if response.applied:
            # Reconciliation commits before the in-memory runtime reload. Keep
            # this idempotent so a transient reload failure is retried even
            # when the next refresh has no watchlist delta.
            get_runner().reload_strategy()
        if response.run.status == "COMPLETE":
            observation_plan = build_quant_observation_plan(db)
            if observation_plan.items:
                try:
                    with _watchlist_quant_sync_lock:
                        WatchlistQuantService(
                            db,
                            get_runner().broker,
                        ).score_due_items(
                            observation_plan.items,
                            refresh_interval_minutes=(
                                settings.watchlist_quant_interval_minutes
                            ),
                            ttl_minutes=(
                                settings.watchlist_quant_score_ttl_minutes
                            ),
                            max_items=(
                                settings.watchlist_quant_batch_size
                            ),
                            priority_symbols=(
                                observation_plan.priority_symbols
                            ),
                        )
                except QuantScoringOutsideRTHError as exc:
                    logger.info(
                        "post-selection watchlist quant scoring skipped: %s",
                        exc,
                    )
                except Exception:
                    db.rollback()
                    logger.exception(
                        "post-selection watchlist quant scoring failed"
                    )
    finally:
        db.close()


async def _run_universe_selection_tick() -> None:
    """Join the worker thread before shutdown so DB writes cannot race stop."""
    worker = asyncio.create_task(
        asyncio.to_thread(_universe_selection_tick_sync)
    )
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except Exception:
            logger.exception(
                "universe selection failed during shutdown"
            )
        raise


async def _universe_selection_cron() -> None:
    """Refresh at a bounded interval; daily run identity makes this idempotent."""
    if not settings.universe_selection_enabled:
        return
    await asyncio.sleep(30)
    while True:
        async with _universe_selection_lock:
            try:
                await _run_universe_selection_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("universe selection cron failed")
        await asyncio.sleep(
            settings.universe_selection_interval_minutes * 60
        )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    from app.api.deps import init_audit_logger

    init_db()
    init_audit_logger()
    # Log the active CORS allowlist so operators can confirm allowed origins
    # in the runtime log (Issue 5: helps diagnose CORS rejections in prod).
    allowed_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    logger.info("CORS allowlist: %s", allowed_origins)
    runner = get_runner()
    started = await asyncio.to_thread(runner.start, loop=asyncio.get_running_loop())
    if not started:
        message = "runner failed to start during app lifespan"
        logger.critical(message)
        raise RuntimeError(message)

    if settings.platform_mode:
        db = SessionLocal()
        try:
            strategy_config = StrategyService(db).get_config()
            registry = get_default_registry()
            strategy_cls = registry.get("interval")
            strategy = cast(Any, strategy_cls)(params={
                "buy_low": Decimal(str(strategy_config.buy_low or 0)),
                "sell_high": Decimal(str(strategy_config.sell_high or 0)),
                "quantity": int(getattr(strategy_config, "quantity", 0) or 0),
            })
            platform_runner = PlatformRunner(
                symbol=strategy_config.symbol or "",
                strategy=strategy,
                mode="live",
            )
            _app.state.platform_runner = platform_runner
            logger.info("platform runner enabled for symbol=%s", strategy_config.symbol)
        except Exception:
            logger.exception("failed to initialize platform runner")
            _app.state.platform_runner = None
        finally:
            db.close()
    else:
        _app.state.platform_runner = None

    background_tasks = (
        asyncio.create_task(_ws_cleanup_task()),
        asyncio.create_task(_llm_analysis_cron()),
        asyncio.create_task(_report_schedule_cron()),
        asyncio.create_task(_alert_rules_cron()),
        asyncio.create_task(_llm_storage_maintenance_cron()),
        asyncio.create_task(_strategy_v2_shadow_cron()),
        asyncio.create_task(_opening_momentum_shadow_cron()),
        asyncio.create_task(_universe_selection_cron()),
        asyncio.create_task(_watchlist_quant_cron()),
        asyncio.create_task(_watchlist_quant_v6_evaluation_cron()),
    )
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("error during task cleanup")
        await asyncio.to_thread(runner.stop)


_OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "strategy", "description": "区间策略配置、状态与历史。"},
    {"name": "strategy-v2-shadow", "description": "Strategy v2 前向影子决策与回放。"},
    {
        "name": "opening-momentum-shadow",
        "description": "横截面开盘动量前向影子观测。",
    },
    {"name": "universe", "description": "版本化动态候选池与只读观察标的。"},
    {
        "name": "watchlist-quant-v6",
        "description": "Quant-v6 已持久化不可变研究证据（只读）。",
    },
    {"name": "trade", "description": "订单、账户、事件与交易控制。"},
    {"name": "credentials", "description": "长桥凭据与多渠道通知。"},
    {"name": "llm", "description": "DeepSeek LLM 顾问区间建议。"},
    {"name": "backtest", "description": "离线 CSV 回测。"},
    {"name": "indicators", "description": "实时技术指标快照（只读）。"},
    {"name": "lab", "description": "实验 / 性能 A/B 统计 / 指标（只读）。"},
    {"name": "websocket", "description": "实时状态推送。"},
    {"name": "system", "description": "健康 / 就绪检查与 OpenAPI 元数据。"},
]


app = FastAPI(
    title="Auto Trade",
    version=APP_VERSION,
    description=(
        "基于长桥 OpenAPI 的自动化区间交易系统。"
        "提供策略、订单、事件、LLM 顾问、回测、实验与多渠道通知等能力。"
    ),
    openapi_tags=_OPENAPI_TAGS,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type", "Accept"],
)

app.include_router(platform_router, prefix="/api/platform")
app.include_router(portfolio_router, prefix="/api/portfolio")
app.include_router(strategy_router)
app.include_router(strategy_shadow_router)
app.include_router(opening_momentum_shadow_router)
app.include_router(strategy_experiments_router)
app.include_router(credentials_router)
app.include_router(trade_router)
app.include_router(universe_router)
app.include_router(watchlist_quant_v6_router)
app.include_router(watchlist_router)
app.include_router(llm_advisor_router)
app.include_router(backtest_router)
app.include_router(audit_pack_router)
app.include_router(audit_log_router)
app.include_router(trade_notes_router)
app.include_router(trades_router)
app.include_router(equity_router)
app.include_router(pnl_router)
app.include_router(positions_router)
app.include_router(alert_rules_router)
app.include_router(alert_firings_router)
app.include_router(strategy_presets_router)
app.include_router(risk_router)
app.include_router(broker_router)
app.include_router(llm_interactions_router)
app.include_router(llm_usage_router)
app.include_router(notifications_router)
app.include_router(experiments_router)
app.include_router(performance_router)
app.include_router(reports_router)
app.include_router(indicators_router)
app.include_router(review_router)
app.include_router(calendar_router)
app.include_router(metrics_router)
app.include_router(ws_router)
app.include_router(signal_consensus_router)
app.include_router(universe_explainer_router)
app.include_router(risk_timeline_router)
app.include_router(platform_catalog_router)
app.include_router(attribution_router)
app.include_router(regime_router)
app.include_router(drawdown_analysis_router)
app.include_router(strategy_health_router)
app.include_router(execution_quality_router)
app.include_router(decision_replay_router)
app.include_router(lookahead_analysis_router)
app.include_router(monte_carlo_router)
app.include_router(correlation_router)
app.include_router(kelly_router)
app.include_router(streaks_router)
app.include_router(time_performance_router)
app.include_router(rolling_metrics_router)
app.include_router(recovery_router)
app.include_router(benchmark_router)
app.include_router(tag_analytics_router)
app.include_router(risk_score_router)
app.include_router(holding_time_router)
app.include_router(distribution_shape_router)
app.include_router(trade_frequency_router)
app.include_router(profit_factor_router)
app.include_router(concentration_router)
app.include_router(autocorrelation_router)
app.include_router(size_impact_router)
app.include_router(return_calendar_router)
app.include_router(edge_quality_router)
app.include_router(decay_detection_router)
app.include_router(rolling_var_router)
app.include_router(asymmetry_router)
app.include_router(capital_efficiency_router)
app.include_router(intraday_seasonality_router)
app.include_router(drawdown_duration_router)
app.include_router(prediction_score_router)
app.include_router(regime_sensitivity_router)
app.include_router(robustness_router)
app.include_router(milestones_router)
app.include_router(momentum_ranking_router)
app.include_router(fee_drag_router)
app.include_router(exit_efficiency_router)
app.include_router(skip_analytics_router)
app.include_router(r_multiples_router)
app.include_router(profit_concentration_router)
app.include_router(scratch_analysis_router)
app.include_router(reentry_analysis_router)
app.include_router(first_trade_router)
app.include_router(loss_containment_router)
app.include_router(daily_consistency_router)
app.include_router(database_health_router)


# Global exception handler: log unhandled exceptions and return a generic 500 JSON
# response. Avoids leaking internal tracebacks to clients while still preserving
# the full stack in the server log for debugging (Issue 4).
async def _handle_unhandled_exception(request: Any, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    body: dict[str, Any] = {"detail": "Internal server error"}
    if settings.env in ("dev", "test"):
        body["error_type"] = type(exc).__name__
    return JSONResponse(
        status_code=500,
        content=body,
    )


app.add_exception_handler(Exception, _handle_unhandled_exception)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from app.database import engine

    from sqlalchemy import text

    health_status: dict[str, Any] = {"ok": True}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception:
        logger.exception("health check database probe failed")
        health_status["database"] = "error"
        health_status["ok"] = False

    try:
        runner = get_runner()
        diag = runner.diagnostics()
        health_status["runner"] = {
            "running": diag.get("runner_running", False),
            "quotes_subscribed": diag.get("quotes_subscribed", False),
        }
    except Exception:
        logger.exception("health check runner probe failed")
        health_status["runner"] = "unavailable"

    return health_status


@app.get("/api/ready", response_model=None)
async def ready() -> JSONResponse | dict[str, Any]:
    """Readiness probe: DB queryable + runner initialized (Issue 7).

    Returns 200 with ``ready: true`` when the process is ready to serve
    traffic. Returns 503 when DB is unreachable or runner failed to start.

    Note: ``response_model=None`` is required because FastAPI cannot
    construct a Pydantic model for the union ``JSONResponse | dict``;
    we want both branches to pass through verbatim.
    """
    from app.database import engine

    from sqlalchemy import text

    ready_status: dict[str, Any] = {"ready": True, "checks": {}}
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        ready_status["checks"]["database"] = "ok"
        db_ok = True
    except Exception:
        logger.exception("readiness check database probe failed")
        ready_status["checks"]["database"] = "error"

    runner_ok = False
    try:
        runner = get_runner()
        diag = runner.diagnostics()
        runner_ok = bool(diag.get("runner_running", False))
        ready_status["checks"]["runner"] = {
            "initialized": runner_ok,
            "quotes_subscribed": diag.get("quotes_subscribed", False),
        }
    except Exception:
        logger.exception("readiness check runner probe failed")
        ready_status["checks"]["runner"] = "unavailable"

    if not (db_ok and runner_ok):
        ready_status["ready"] = False
        return JSONResponse(status_code=503, content=ready_status)
    return ready_status
