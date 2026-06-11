from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings
from app.core.broker import BrokerGateway
from app.database import SessionLocal
from app.domain.prompt.context_module import ContextModule
from app.domain.prompt.feature_selector import FeatureSelector
from app.domain.prompt.output_module import OutputModule
from app.domain.prompt.prompt_builder import PromptBuilder
from app.domain.prompt.selection_module import SelectionModule
from app.domain.prompt.sentiment_module import SentimentModule
from app.domain.prompt.strategy_module import StrategyModule
from app.domain.prompt.system_module import SystemModule
from app.services.data_aggregator import DataAggregator
from app.services.llm_interaction_service import LLMInteractionService
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.llm_advisor")

_LAST_ANALYSIS_TIMESTAMP: float = 0.0
_LAST_PREVIEW_TIMESTAMP: float = 0.0
_PREVIEW_THROTTLE_SECONDS = 60.0
_ORDER_ACTIONS = {
    "NONE",
    "BUY_NOW",
    "SELL_NOW",
    "SELL_SHORT_NOW",
    "BUY_TO_COVER_NOW",
    "STOP_LOSS_SELL_NOW",
    "STOP_LOSS_COVER_NOW",
    "CANCEL_PENDING",
    "CANCEL_REPLACE",
}
_REPLACEMENT_ACTIONS = {
    "NONE",
    "BUY_NOW",
    "SELL_NOW",
    "SELL_SHORT_NOW",
    "BUY_TO_COVER_NOW",
    "STOP_LOSS_SELL_NOW",
    "STOP_LOSS_COVER_NOW",
}


def build_recent_analysis_context(config: Any) -> dict[str, Any] | None:
    """Build compact previous LLM analysis context for the next prompt."""
    has_suggestion = (
        getattr(config, "llm_suggested_buy_low", None) is not None
        and getattr(config, "llm_suggested_sell_high", None) is not None
    )
    has_analysis = bool(getattr(config, "llm_analysis", None))
    has_reject = bool(getattr(config, "llm_reject_reason", None))
    if not has_suggestion and not has_analysis and not has_reject:
        return None

    last_analysis_at = getattr(config, "llm_last_analysis_at", None)
    return {
        "last_analysis_at": last_analysis_at.isoformat() if last_analysis_at else None,
        "buy_low": getattr(config, "llm_suggested_buy_low", None),
        "sell_high": getattr(config, "llm_suggested_sell_high", None),
        "confidence_score": getattr(config, "llm_confidence_score", None),
        "analysis": getattr(config, "llm_analysis", None),
        "applied_buy_low": getattr(config, "llm_applied_buy_low", None),
        "applied_sell_high": getattr(config, "llm_applied_sell_high", None),
        "reject_reason": getattr(config, "llm_reject_reason", None),
    }


class LLMAdvisorService:
    """Calls DeepSeek API to get price interval recommendations."""

    def __init__(self, broker: BrokerGateway | None = None) -> None:
        self._data_aggregator = DataAggregator(broker=broker)

    def _build_prompt(
        self,
        *,
        symbol: str,
        market: str,
        current_price: float,
        current_buy_low: float,
        current_sell_high: float,
        short_selling: bool,
        current_position: str,
        recent_trades: list[dict[str, Any]],
        position_quantity: float,
        position_avg_price: float,
        unrealized_pnl_pct: float,
        min_profit_amount: float,
        recent_prices: list[dict[str, Any]] | None,
        recent_analysis: dict[str, Any] | None,
        account_context: dict[str, Any] | None,
        market_data: dict[str, Any],
        prompt_template: str | None = None,
    ) -> str:
        """Build LLM prompt using modular PromptBuilder or a custom template.

        When a custom ``prompt_template`` is supplied (e.g. from an A/B test
        variant), the system role instructions from ``SystemModule`` are always
        prepended so that the LLM's baseline safety directives cannot be fully
        overridden by an untrusted template.
        """
        if prompt_template:
            system_instructions = SystemModule().render({})
            return f"{system_instructions}\n\n{prompt_template}"
        context: dict[str, Any] = {
            "symbol": symbol,
            "market": market,
            "current_price": current_price,
            "current_buy_low": current_buy_low,
            "current_sell_high": current_sell_high,
            "short_selling": short_selling,
            "daily_candles": market_data.get("daily_candles", []),
            "minute_candles": market_data.get("minute_candles", []),
            "atr": market_data.get("atr", 0.0),
            "bb_upper": market_data.get("bb_upper", 0.0),
            "bb_middle": market_data.get("bb_middle", 0.0),
            "bb_lower": market_data.get("bb_lower", 0.0),
            "current_position": current_position,
            "recent_trades": recent_trades,
            "position_quantity": position_quantity,
            "position_avg_price": position_avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "min_profit_amount": min_profit_amount,
            "rsi": market_data.get("rsi", 0.0),
            "macd": market_data.get("macd") or {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "volume_analysis": market_data.get("volume_analysis") or {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
            "sentiment": market_data.get("sentiment") or {"sentiment": "neutral", "score": 0.0, "description": "无"},
            "market_state": market_data.get("market_state"),
            "obv": market_data.get("obv"),
            "adx": market_data.get("adx"),
            "stochastic": market_data.get("stochastic"),
            "cci": market_data.get("cci"),
            "williams_r": market_data.get("williams_r"),
            "vwap": market_data.get("vwap"),
            "aggregate_signals": market_data.get("aggregate_signals"),
            "account_context_text": DataAggregator._format_account_context(account_context),
            "recent_price_context": DataAggregator._format_recent_prices(recent_prices),
            "recent_analysis_context": DataAggregator._format_recent_analysis(recent_analysis),
        }
        builder = PromptBuilder()
        builder.add_module(SystemModule())
        builder.add_module(SelectionModule())
        builder.add_module(ContextModule())
        builder.add_module(SentimentModule())
        builder.add_module(StrategyModule())
        builder.add_module(OutputModule())
        return builder.build(context)

    def _call_llm(self, prompt: str) -> str:
        """Call LLM API (delegates to _call_deepseek)."""
        return self._call_deepseek(prompt)

    def _select_variant(self, symbol: str) -> tuple[str | None, str | None]:
        """Select A/B prompt variant for the given symbol.

        Returns (template, variant_name). If no experiment is configured or
        no variants are available, returns (None, None).
        """
        experiment_name = settings.llm_experiment_name
        if not experiment_name:
            return None, None
        try:
            from app.domain.experiment.ab_test_manager import ABTestManager

            db = None
            try:
                db = SessionLocal()
                manager = ABTestManager(db)
                variant = manager.select_variant(symbol, experiment_name)
                if variant:
                    return variant.template, f"{variant.name}:{variant.version}"
            finally:
                if db is not None:
                    db.close()
        except Exception:
            logger.debug("failed to select A/B variant for %s", symbol, exc_info=True)
        return None, None

    def _get_active_prompt_template(self) -> str | None:
        """Load active prompt template from experiment if available.

        Fallback when A/B experiment name is not configured.
        """
        try:
            from app.domain.experiment.ab_test_manager import ABTestManager

            db = None
            try:
                db = SessionLocal()
                manager = ABTestManager(db)
                experiment_name = settings.llm_experiment_name or None
                active = manager.get_active_version(experiment_name)
                return active.template if active else None
            finally:
                if db is not None:
                    db.close()
        except Exception:
            logger.debug("no active experiment variant available")
            return None

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
        position_quantity: float = 0.0,
        position_avg_price: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        min_profit_amount: float = 0.0,
        recent_prices: list[dict[str, Any]] | None = None,
        recent_analysis: dict[str, Any] | None = None,
        account_context: dict[str, Any] | None = None,
        force: bool = False,
        persist: bool = True,
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
                "rsi": 0.0,
                "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
                "volume_analysis": {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
                "sentiment": {"sentiment": "neutral", "score": 0.0, "description": "无"},
            }

        prompt_template, prompt_variant = self._select_variant(symbol)
        prompt = self._build_prompt(
            symbol=symbol,
            market=market,
            current_price=current_price,
            current_buy_low=current_buy_low,
            current_sell_high=current_sell_high,
            short_selling=short_selling,
            current_position=current_position,
            recent_trades=recent_trades,
            position_quantity=position_quantity,
            position_avg_price=position_avg_price,
            unrealized_pnl_pct=unrealized_pnl_pct,
            min_profit_amount=min_profit_amount,
            recent_prices=recent_prices,
            recent_analysis=recent_analysis,
            account_context=account_context,
            market_data=market_data,
            prompt_template=prompt_template,
        )
        context_snapshot = {
            "symbol": symbol,
            "market": market,
            "current_price": current_price,
            "current_buy_low": current_buy_low,
            "current_sell_high": current_sell_high,
            "short_selling": short_selling,
            "current_position": current_position,
            "position_quantity": position_quantity,
            "position_avg_price": position_avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "min_profit_amount": min_profit_amount,
            "recent_prices": recent_prices or [],
            "recent_analysis": recent_analysis or {},
            "account_context": account_context or {},
        }

        raw_response = ""
        try:
            raw_response = self._call_llm(prompt)
            result = self._parse_response(raw_response)

            # NOTE: indicator selection is validated against AVAILABLE_INDICATORS
            # whitelist inside FeatureSelector.parse_selection, so adversarial
            # keys injected via prompt or market data are discarded.
            market_state_raw = market_data.get("market_state")
            market_state = market_state_raw if isinstance(market_state_raw, dict) else {}
            suggested_raw = market_state.get("suggested_indicators", [])
            suggested = suggested_raw if isinstance(suggested_raw, list) else []
            selected = FeatureSelector.parse_selection(raw_response, suggested)
            logger.info("LLM selected indicators: %s", selected)
        except Exception as exc:
            logger.exception("LLM analysis failed")
            interaction_id = self._record_interaction(
                interaction_type="analyze",
                symbol=symbol,
                market=market,
                prompt=prompt,
                raw_response=raw_response,
                result=None,
                context_snapshot=context_snapshot,
                success=False,
                error=f"LLM analysis failed: {exc}",
                prompt_variant=prompt_variant,
            )
            return {
                "success": False,
                "error": "LLM analysis failed",
                "interaction_id": interaction_id,
            }

        next_analysis_at = datetime.now(timezone.utc) + timedelta(minutes=interval_minutes)
        _persist_succeeded = True
        if persist:
            db = SessionLocal()
            try:
                next_analysis_at = self._record_analysis(db, result, interval_minutes)
            except Exception:
                _persist_succeeded = False
                logger.exception("failed to record LLM analysis")
            finally:
                db.close()

        if _persist_succeeded:
            _LAST_ANALYSIS_TIMESTAMP = time.monotonic()  # pyright: ignore[reportConstantRedefinition]

        interaction_id = self._record_interaction(
            interaction_type="analyze",
            symbol=symbol,
            market=market,
            prompt=prompt,
            raw_response=raw_response,
            result=result,
            context_snapshot=context_snapshot,
            success=True,
            error="",
            prompt_variant=prompt_variant,
        )

        return {
            "success": True,
            "applied": False,
            "reason": "Analysis completed. Use IntervalApplicationService to apply.",
            "interaction_id": interaction_id,
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
            "analysis": result.get("analysis"),
            "next_analysis_at": next_analysis_at.isoformat(),
            "applied_at": None,
            "order_action": result.get("order_action"),
            "order_price": result.get("order_price"),
            "replacement_action": result.get("replacement_action"),
            "replacement_price": result.get("replacement_price"),
            "order_reason": result.get("order_reason"),
        }

    def preview(
        self,
        symbol: str,
        market: str,
        current_price: float,
        current_buy_low: float,
        current_sell_high: float,
        short_selling: bool,
        min_profit_amount: float = 0.0,
        account_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run LLM preview analysis.

        Records interaction for both success and failure paths (does not apply
        suggestions, does not update analysis throttle, and never triggers
        orders).
        """
        global _LAST_PREVIEW_TIMESTAMP

        if _LAST_PREVIEW_TIMESTAMP > 0 and time.monotonic() - _LAST_PREVIEW_TIMESTAMP < _PREVIEW_THROTTLE_SECONDS:
            return {
                "success": False,
                "applied": False,
                "error": "Preview throttled: please wait before requesting another preview",
            }

        try:
            market_data = self._data_aggregator.fetch_market_data(symbol, market)
        except Exception:
            logger.exception("failed to fetch market data for LLM preview")
            market_data = {
                "daily_candles": [],
                "minute_candles": [],
                "current_price": current_price,
                "atr": 0.0,
                "bb_upper": 0.0,
                "bb_middle": 0.0,
                "bb_lower": 0.0,
                "rsi": 0.0,
                "macd": {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
                "volume_analysis": {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
                "sentiment": {"sentiment": "neutral", "score": 0.0, "description": "无"},
            }

        current_price_raw = market_data.get("current_price")
        if isinstance(current_price_raw, (int, float)):
            prompt_price = float(current_price_raw)
        else:
            prompt_price = float(current_price)
        if prompt_price <= 0:
            return {"success": False, "applied": False, "error": "Market data unavailable for preview"}

        prompt_template, prompt_variant = self._select_variant(symbol)
        prompt = self._build_prompt(
            symbol=symbol,
            market=market,
            current_price=prompt_price,
            current_buy_low=current_buy_low,
            current_sell_high=current_sell_high,
            short_selling=short_selling,
            current_position="FLAT",
            recent_trades=[],
            position_quantity=0.0,
            position_avg_price=0.0,
            unrealized_pnl_pct=0.0,
            min_profit_amount=min_profit_amount,
            recent_prices=None,
            recent_analysis=None,
            account_context=account_context,
            market_data=market_data,
            prompt_template=prompt_template,
        )
        context_snapshot = {
            "symbol": symbol,
            "market": market,
            "current_price": prompt_price,
            "current_buy_low": current_buy_low,
            "current_sell_high": current_sell_high,
            "short_selling": short_selling,
            "min_profit_amount": min_profit_amount,
            "account_context": account_context or {},
        }

        raw_response = ""
        try:
            raw_response = self._call_llm(prompt)
            result = self._parse_response(raw_response)
        except Exception as exc:
            logger.exception("LLM preview failed")
            self._record_interaction(
                interaction_type="preview",
                symbol=symbol,
                market=market,
                prompt=prompt,
                raw_response=raw_response,
                result=None,
                context_snapshot=context_snapshot,
                success=False,
                error=f"LLM preview failed: {exc}",
                prompt_variant=prompt_variant,
            )
            return {"success": False, "applied": False, "error": "LLM preview failed"}

        _LAST_PREVIEW_TIMESTAMP = time.monotonic()  # pyright: ignore[reportConstantRedefinition]

        interaction_id = self._record_interaction(
            interaction_type="preview",
            symbol=symbol,
            market=market,
            prompt=prompt,
            raw_response=raw_response,
            result=result,
            context_snapshot=context_snapshot,
            success=True,
            error="",
            prompt_variant=prompt_variant,
        )

        return {
            "success": True,
            "applied": False,
            "reason": "Preview completed. Confirm to save and apply.",
            "interaction_id": interaction_id,
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
            "analysis": result.get("analysis"),
            "next_analysis_at": None,
            "applied_at": None,
            "order_action": result.get("order_action"),
        }

    @staticmethod
    def _deepseek_chat_payload(prompt: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": settings.deepseek_model,
            "messages": [
                {"role": "system", "content": "You are a professional quantitative trading advisor."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": settings.deepseek_max_tokens,
            "thinking": {"type": settings.deepseek_thinking_type},
        }
        if settings.deepseek_thinking_type == "enabled":
            payload["reasoning_effort"] = settings.deepseek_reasoning_effort
        return payload

    def _call_deepseek(self, prompt: str) -> str:
        """Call DeepSeek API with the prompt."""
        if not settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")

        try:
            response = httpx.post(
                settings.deepseek_api_url,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=self._deepseek_chat_payload(prompt),
                timeout=120.0,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"DeepSeek request timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"DeepSeek request failed: {exc}") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"DeepSeek HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"DeepSeek returned non-JSON response: {response.text[:200]}"
            ) from exc

        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"DeepSeek response missing message content: {exc}") from exc
        if not content.strip():
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
            completion_details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
            reasoning_tokens = (
                completion_details.get("reasoning_tokens")
                if isinstance(completion_details, dict)
                else None
            )
            reasoning_len = len(str(message.get("reasoning_content") or ""))
            raise RuntimeError(
                "DeepSeek returned empty content"
                f" (finish_reason={finish_reason}, reasoning_tokens={reasoning_tokens},"
                f" reasoning_chars={reasoning_len}, max_tokens={settings.deepseek_max_tokens})."
                " Increase DEEPSEEK_MAX_TOKENS or lower/disable DeepSeek thinking."
            )
        return content

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
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(
                f"LLM response must be a JSON object, got {type(parsed).__name__}"
            )
        return LLMAdvisorService._normalize_response(parsed)

    @staticmethod
    def _normalize_response(parsed: dict[str, Any]) -> dict[str, Any]:
        result = dict(parsed)
        action = str(result.get("order_action") or "NONE").strip().upper()
        if action not in _ORDER_ACTIONS:
            action = "NONE"
        replacement_action = str(result.get("replacement_action") or "NONE").strip().upper()
        if replacement_action not in _REPLACEMENT_ACTIONS:
            replacement_action = "NONE"
        result["order_action"] = action
        result["replacement_action"] = replacement_action
        result.setdefault("order_price", None)
        result.setdefault("replacement_price", None)
        result.setdefault("order_reason", "")
        return result

    @staticmethod
    def _is_throttled(interval_seconds: float = 1800.0) -> bool:
        """Check if analysis is throttled."""
        if _LAST_ANALYSIS_TIMESTAMP <= 0:
            return False
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

    @staticmethod
    def _record_interaction(
        *,
        interaction_type: str,
        symbol: str,
        market: str,
        prompt: str,
        raw_response: str,
        result: dict[str, Any] | None,
        context_snapshot: dict[str, Any],
        success: bool,
        error: str,
        prompt_variant: str | None = None,
    ) -> int | None:
        db = SessionLocal()
        try:
            record = LLMInteractionService(db).create(
                interaction_type=interaction_type,
                symbol=symbol,
                market=market,
                prompt=prompt,
                raw_response=raw_response,
                parsed_response=result or {},
                context_snapshot=context_snapshot,
                success=success,
                error=error,
                order_action=(result or {}).get("order_action", "NONE"),
                prompt_variant=prompt_variant,
            )
            return record.id
        except Exception:
            logger.exception("failed to record LLM interaction")
            return None
        finally:
            db.close()
