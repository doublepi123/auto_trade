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
from app.api.ws import router as ws_router
from app.api.ws import manager as ws_manager
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
_universe_selection_lock = asyncio.Lock()
_llm_globals_lock = threading.Lock()


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

        targets = _llm_runtime_targets(runner, config.symbol, config.market)
        if not targets:
            return
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
                symbol_last_analysis_at = symbol_state.last_analysis_at
                symbol_next_analysis_at = symbol_state.next_analysis_at
                symbol_last_status = getattr(symbol_state, "last_status", "")
                # A newly created schedule row is flushed by get_state(). End
                # that transaction before broker context and the long LLM call.
                db.commit()
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
    """Prune and compact LLM audit rows without running on the event loop."""
    from app.services.llm_interaction_service import LLMInteractionService

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
    finally:
        db.close()


async def _llm_storage_maintenance_cron() -> None:
    """Run bounded SQLite maintenance; full VACUUM is intentionally offline-only."""
    await asyncio.sleep(60)
    while True:
        try:
            await _run_llm_storage_maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("LLM storage maintenance failed")
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
            logger.exception("LLM storage maintenance failed during shutdown")
        raise


def _strategy_v2_shadow_tick_sync() -> None:
    """Advance every active Strategy v2 simulator without touching orders."""
    from app.core.market_calendar import market_for_symbol
    from app.models import (
        StrategyV2ForwardRegistration,
        StrategyV2ShadowConfig,
        StrategyV2ShadowTrade,
    )
    from app.services.strategy_v2_shadow_service import StrategyV2ShadowService

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
        registered_symbols = db.query(StrategyV2ForwardRegistration.symbol).all()
        for (symbol,) in (*enabled_symbols, *open_symbols, *registered_symbols):
            targets.setdefault(symbol, market_for_symbol(symbol))
        if not targets:
            return

        shadow = StrategyV2ShadowService(db, get_runner().broker)
        for symbol, market in sorted(targets.items()):
            try:
                shadow.tick(symbol=symbol, market=market)
            except Exception:
                db.rollback()
                logger.exception("Strategy v2 shadow tick failed for symbol=%s", symbol)
            try:
                shadow.collect_forward_validation(symbol=symbol, market=market)
            except Exception:
                db.rollback()
                logger.exception(
                    "Strategy v2 forward validation failed for symbol=%s",
                    symbol,
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


def _universe_selection_tick_sync() -> None:
    """Refresh the candidate pool and its short-horizon suitability scores."""
    if not settings.universe_selection_enabled:
        return
    from app.api.universe import build_universe_selection_service
    from app.models import WatchlistItem
    from app.services.watchlist_quant_service import (
        QuantScoringOutsideRTHError,
        WatchlistQuantService,
    )

    db = SessionLocal()
    try:
        response = build_universe_selection_service(db).refresh()
        logger.info(
            "universe selection run=%d as_of=%s status=%s coverage=%.3f "
            "selected=%d applied=%s",
            response.run.id,
            response.run.as_of_date,
            response.run.status,
            response.run.coverage_ratio,
            response.run.selected_count,
            response.applied,
        )
        if response.applied:
            # Reconciliation commits before the in-memory runtime reload. Keep
            # this idempotent so a transient reload failure is retried even
            # when the next refresh has no watchlist delta.
            get_runner().reload_strategy()
        if response.run.status == "COMPLETE":
            watchlist_items = db.query(WatchlistItem).all()
            if watchlist_items:
                try:
                    WatchlistQuantService(
                        db,
                        get_runner().broker,
                    ).score_items(
                        watchlist_items,
                        ttl_minutes=max(
                            60,
                            settings.universe_selection_interval_minutes * 2,
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
        asyncio.create_task(_universe_selection_cron()),
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
    {"name": "universe", "description": "版本化动态候选池与只读观察标的。"},
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
app.include_router(strategy_experiments_router)
app.include_router(credentials_router)
app.include_router(trade_router)
app.include_router(universe_router)
app.include_router(watchlist_router)
app.include_router(llm_advisor_router)
app.include_router(backtest_router)
app.include_router(audit_pack_router)
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
