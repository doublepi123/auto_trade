from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings
from app.database import SessionLocal
from app.services.data_aggregator import DataAggregator
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.llm_advisor")

_LAST_ANALYSIS_TIMESTAMP: float = 0.0


class LLMAdvisorService:
    """Calls DeepSeek API to get price interval recommendations."""

    def __init__(self) -> None:
        self._data_aggregator = DataAggregator()

    def analyze(
        self,
        symbol: str,
        market: str,
        current_price: float,
        current_buy_low: float,
        current_sell_high: float,
        short_selling: bool,
        current_position: str,
        recent_trades: list[dict[str, Any]],
        force: bool = False,
    ) -> dict[str, Any]:
        """Run LLM analysis and return recommendation."""
        global _LAST_ANALYSIS_TIMESTAMP

        interval_minutes = self._get_interval_minutes()
        if not force and self._is_throttled(interval_minutes * 60):
            return {
                "success": False,
                "error": f"Analysis throttled: please wait {interval_minutes} minutes between analyses",
            }

        try:
            market_data = self._data_aggregator.fetch_market_data(symbol, market)
        except Exception:
            logger.exception("failed to fetch market data for LLM analysis")
            market_data = {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": current_price,
                "atr": 0.0,
                "bb_upper": 0.0,
                "bb_middle": 0.0,
                "bb_lower": 0.0,
            }

        prompt = self._data_aggregator.build_prompt(
            symbol=symbol,
            market=market,
            current_price=current_price,
            current_buy_low=current_buy_low,
            current_sell_high=current_sell_high,
            short_selling=short_selling,
            daily_candles=market_data.get("daily_candles", []),
            minute_candles=market_data.get("minute_candles", []),
            atr=market_data.get("atr", 0.0),
            bb_upper=market_data.get("bb_upper", 0.0),
            bb_middle=market_data.get("bb_middle", 0.0),
            bb_lower=market_data.get("bb_lower", 0.0),
            current_position=current_position,
            recent_trades=recent_trades,
        )

        try:
            raw_response = self._call_deepseek(prompt)
            result = self._parse_response(raw_response)
        except Exception as exc:
            logger.exception("LLM analysis failed")
            return {
                "success": False,
                "error": f"LLM analysis failed: {exc}",
            }

        _LAST_ANALYSIS_TIMESTAMP = time.monotonic()

        db = SessionLocal()
        try:
            next_analysis_at = self._record_analysis(db, result, interval_minutes)
        except Exception:
            logger.exception("failed to record LLM analysis")
            next_analysis_at = datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)
        finally:
            db.close()

        return {
            "success": True,
            "applied": False,
            "reason": "Analysis completed. Use IntervalApplicationService to apply.",
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
            "analysis": result.get("analysis"),
            "next_analysis_at": next_analysis_at.isoformat(),
            "applied_at": None,
        }

    def _call_deepseek(self, prompt: str) -> str:
        """Call DeepSeek API with the prompt."""
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")

        response = httpx.post(
            settings.deepseek_api_url,
            headers={
                "Authorization": f"Bearer {settings.deepseek_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "You are a professional quantitative trading advisor."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 500,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        """Parse JSON from LLM response."""
        raw = raw.strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        return json.loads(raw)

    @staticmethod
    def _is_throttled(interval_seconds: float = 1800.0) -> bool:
        """Check if analysis is throttled."""
        return time.monotonic() - _LAST_ANALYSIS_TIMESTAMP < interval_seconds

    @staticmethod
    def _get_interval_minutes() -> int:
        db = SessionLocal()
        try:
            config = StrategyService(db).get_config()
            return config.llm_interval_minutes or settings.llm_interval_cron_minutes
        except Exception:
            logger.exception("failed to load LLM interval minutes; using settings default")
            return settings.llm_interval_cron_minutes
        finally:
            db.close()

    def _record_analysis(self, db: Any, result: dict[str, Any], interval_minutes: int) -> datetime:
        """Record LLM analysis result to database."""
        svc = StrategyService(db)
        config = svc.get_config()
        now = datetime.now(timezone.utc)
        next_analysis_at = now + timedelta(minutes=interval_minutes)
        config.llm_suggested_buy_low = result.get("suggested_buy_low")
        config.llm_suggested_sell_high = result.get("suggested_sell_high")
        config.llm_confidence_score = result.get("confidence_score")
        config.llm_analysis = result.get("analysis")
        config.llm_last_analysis_at = now
        config.llm_next_analysis_at = next_analysis_at
        db.commit()
        return next_analysis_at
