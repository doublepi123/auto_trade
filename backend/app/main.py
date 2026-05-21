from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.credentials import router as credentials_router
from app.api.llm_advisor import router as llm_advisor_router
from app.api.strategy import router as strategy_router
from app.api.trade import router as trade_router
from app.api.ws import router as ws_router
from app.api.ws import manager as ws_manager
from app.config import settings
from app.database import init_db, SessionLocal
from app.runner import get_runner
from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("auto_trade.main")


async def _ws_cleanup_task() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await ws_manager.cleanup_stale()
        except Exception:
            pass


async def _llm_analysis_cron() -> None:
    from app.database import SessionLocal
    from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
    from app.services.strategy_service import StrategyService
    from app.runner import get_runner

    while True:
        await asyncio.sleep(60)
        try:
            db = SessionLocal()
            try:
                svc = StrategyService(db)
                config = svc.get_config()
                if not config.auto_interval_enabled or not config.symbol:
                    continue

                now = datetime.now(timezone.utc)
                interval_minutes = config.llm_interval_minutes or settings.llm_interval_cron_minutes
                last_analysis_at = config.llm_last_analysis_at
                if last_analysis_at is not None:
                    if last_analysis_at.tzinfo is None:
                        last_analysis_at = last_analysis_at.replace(tzinfo=timezone.utc)
                    if now - last_analysis_at < timedelta(minutes=interval_minutes):
                        continue

                runner = get_runner()
                current_price = runner.engine.last_price if runner.engine else 0.0

                advisor = LLMAdvisorService()
                result = advisor.analyze(
                    symbol=config.symbol,
                    market=config.market,
                    current_price=current_price,
                    current_buy_low=config.buy_low,
                    current_sell_high=config.sell_high,
                    short_selling=config.short_selling,
                    current_position=runner.engine.state.value if runner.engine else "flat",
                    recent_trades=[],
                    recent_prices=runner.recent_price_context(),
                    recent_analysis=build_recent_analysis_context(config),
                    force=True,
                )
                if result.get("success"):
                    IntervalApplicationService().apply_direct_suggestion(
                        db=db,
                        current_price=current_price or config.buy_low,
                        suggestion={
                            "suggested_buy_low": result.get("suggested_buy_low"),
                            "suggested_sell_high": result.get("suggested_sell_high"),
                            "confidence_score": result.get("confidence_score"),
                        },
                    )
            finally:
                db.close()
        except Exception:
            logger.exception("LLM analysis cron failed")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    init_db()
    if not get_runner().start():
        logger.warning("runner failed to start during app lifespan — trading engine is not running")
    cleanup_task = asyncio.create_task(_ws_cleanup_task())
    llm_task = asyncio.create_task(_llm_analysis_cron())
    yield
    cleanup_task.cancel()
    llm_task.cancel()
    get_runner().stop()


app = FastAPI(title="Auto Trade", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:8080"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(strategy_router)
app.include_router(credentials_router)
app.include_router(trade_router)
app.include_router(llm_advisor_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "env": settings.env}
