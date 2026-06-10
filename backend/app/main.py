from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.backtest import router as backtest_router
from app.api.credentials import router as credentials_router
from app.api.experiments import router as experiments_router
from app.api.indicators import router as indicators_router
from app.api.performance import router as performance_router
from app.api.llm_advisor import router as llm_advisor_router
from app.api.reports import router as reports_router
from app.api.review import router as review_router
from app.api.strategy import router as strategy_router
from app.api.strategy_experiments import router as strategy_experiments_router
from app.api.trade import router as trade_router
from app.api.watchlist import router as watchlist_router
from app.api.ws import router as ws_router
from app.api.ws import manager as ws_manager
from app.config import settings
from app.database import init_db
from app.runner import get_runner
from app.services.interval_application_service import IntervalApplicationService
from app.services.llm_symbol_state_service import LLMSymbolStateService
from app.services.trade_event_service import record_trade_event

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("auto_trade.main")
_last_llm_trigger_price: float = 0.0
_last_llm_trigger_price_by_symbol: dict[str, float] = {}
_llm_last_analysis_at_by_symbol: dict[str, datetime] = {}
_llm_analysis_timestamps: list[float] = []
_llm_analysis_lock = asyncio.Lock()
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
            pass


async def _llm_analysis_tick() -> None:
    from app.database import SessionLocal
    from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
    from app.services.strategy_service import StrategyService
    from app.runner import get_runner
    global _last_llm_trigger_price

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
        analyzed_count = 0

        from app.api.llm_advisor import _interval_reference_quantity
        from app.services.llm_interaction_service import LLMInteractionService

        advisor = LLMAdvisorService(broker=runner.broker)
        for symbol, market, engine, is_primary in targets:
            try:
                if analyzed_count >= cycle_budget:
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
                current_price = float(getattr(engine, "last_price", 0.0) or 0.0)
                if current_price <= 0:
                    current_price = config.buy_low if is_primary else float(getattr(params, "buy_low", 0.0) or 0.0)
                if current_price <= 0:
                    continue

                symbol_state = state_svc.get_state(symbol, market)
                with _llm_globals_lock:
                    if is_primary:
                        last_analysis_at = config.llm_last_analysis_at
                        last_trigger_price = _last_llm_trigger_price
                    else:
                        last_analysis_at = symbol_state.last_analysis_at
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

                result = await asyncio.to_thread(
                    advisor.analyze,
                    symbol=symbol,
                    market=market,
                    current_price=current_price,
                    current_buy_low=config.buy_low if is_primary else float(getattr(params, "buy_low", 0.0) or 0.0),
                    current_sell_high=config.sell_high if is_primary else float(getattr(params, "sell_high", 0.0) or 0.0),
                    short_selling=getattr(params, "short_selling", config.short_selling),
                    current_position=str(position_context["side"]),
                    recent_trades=[],
                    position_quantity=float(position_context["quantity"]),
                    position_avg_price=float(position_context["avg_price"]),
                    unrealized_pnl_pct=float(position_context["unrealized_pnl_pct"]),
                    min_profit_amount=float(getattr(params, "min_profit_amount", config.min_profit_amount) or 0.0),
                    recent_prices=_recent_price_context_for_target(engine, runtime, symbol),
                    recent_analysis=build_recent_analysis_context(config) if is_primary else [],
                    account_context=account_context,
                    force=True,
                    persist=is_primary,
                )
                analyzed_count += 1
                now_mono = time.monotonic()
                with _llm_globals_lock:
                    _prune_llm_analysis_timestamps(now_mono)
                    _llm_analysis_timestamps.append(now_mono)
                    _llm_last_analysis_at_by_symbol[symbol] = now

                if result.get("success"):
                    # Only update trigger reference price on successful analysis
                    with _llm_globals_lock:
                        if is_primary:
                            _last_llm_trigger_price = current_price
                        else:
                            _last_llm_trigger_price_by_symbol[symbol] = current_price

                    app_result = {"applied": False, "reason": "secondary symbol analysis does not update primary interval config"}
                    if is_primary:
                        app_result = IntervalApplicationService().apply_direct_suggestion(
                            db=db,
                            current_price=current_price if current_price > 0 else config.buy_low,
                            suggestion={
                                "suggested_buy_low": result.get("suggested_buy_low"),
                                "suggested_sell_high": result.get("suggested_sell_high"),
                                "confidence_score": result.get("confidence_score"),
                            },
                            reference_quantity=_interval_reference_quantity(position_context, account_context),
                        )
                    order_result = {"status": "NO_ACTION", "order_id": None}
                    if result.get("order_action") and result.get("order_action") != "NONE":
                        order_result = await asyncio.to_thread(runner.execute_llm_order_decision, {**result, "symbol": symbol})
                    interaction_id = result.get("interaction_id")
                    if interaction_id is not None:
                        LLMInteractionService(db).update_outcome(
                            interaction_id,
                            applied=bool(app_result["applied"]),
                            order_status=order_result.get("status"),
                            order_id=order_result.get("order_id"),
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
                            "symbol_budget_index": analyzed_count,
                            "persisted_interval": is_primary,
                        },
                    )
                    state_svc.record_analysis(
                        symbol,
                        market,
                        analyzed_at=now,
                        next_analysis_at=now + timedelta(minutes=interval_minutes),
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
                            "symbol_budget_index": analyzed_count,
                            "persisted_interval": is_primary,
                        },
                    )
                    state_svc.record_failure(
                        symbol,
                        market,
                        result.get("error", "Unknown error"),
                        next_analysis_at=now + timedelta(minutes=interval_minutes),
                    )
                    db.commit()
            except Exception:
                db.rollback()
                logger.exception("LLM analysis failed for symbol %s; skipping", symbol)
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    from app.api.deps import init_audit_logger

    init_db()
    init_audit_logger()
    runner = get_runner()
    started = await asyncio.to_thread(runner.start, loop=asyncio.get_running_loop())
    if not started:
        logger.warning("runner failed to start during app lifespan — trading engine is not running")
    cleanup_task = asyncio.create_task(_ws_cleanup_task())
    llm_task = asyncio.create_task(_llm_analysis_cron())
    yield
    cleanup_task.cancel()
    llm_task.cancel()
    for task in (cleanup_task, llm_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("error during task cleanup")
    await asyncio.to_thread(get_runner().stop)


app = FastAPI(title="Auto Trade", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)
app.include_router(strategy_experiments_router)
app.include_router(credentials_router)
app.include_router(trade_router)
app.include_router(watchlist_router)
app.include_router(llm_advisor_router)
app.include_router(backtest_router)
app.include_router(experiments_router)
app.include_router(performance_router)
app.include_router(reports_router)
app.include_router(indicators_router)
app.include_router(review_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    from app.database import engine

    from sqlalchemy import text

    health_status: dict[str, Any] = {"ok": True, "env": settings.env}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        health_status["database"] = "connected"
    except Exception as exc:
        health_status["database"] = f"error: {exc}"
        health_status["ok"] = False

    try:
        runner = get_runner()
        diag = runner.diagnostics()
        health_status["runner"] = {
            "running": diag.get("runner_running", False),
            "quotes_subscribed": diag.get("quotes_subscribed", False),
            "risk_paused": diag.get("risk", {}).get("paused", False),
            "risk_kill_switch": diag.get("risk", {}).get("kill_switch", False),
        }
    except Exception as exc:
        health_status["runner"] = f"error: {exc}"

    return health_status
