from __future__ import annotations

import json
import logging
import math
import re
import threading
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
_throttle_lock = threading.Lock()
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


# Match an already-escaped placeholder {{identifier}}, {{identifier!s}},
# {{identifier:format}}, etc. so we can restore known ones back to
# single-brace for str.format substitution.
_ESCAPED_PLACEHOLDER = re.compile(
    r"\{\{([A-Za-z_][A-Za-z0-9_]*)(?:![ars])?(?::[^}]*)?\}\}"
)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _escape_orphan_braces(template: str, allowed_keys: set[str]) -> str:
    """Escape braces in a ``str.format`` template that are NOT valid placeholders.

    The danger is when a custom prompt template (admin-curated, A/B-tested
    variant) includes literal braces that ``str.format`` would either
    interpret as missing-key placeholders (``KeyError``) or as
    non-placeholder syntax errors (``ValueError``). Example: a markdown
    table inside a comment, or a JSON example illustrating the expected
    output shape.

    Strategy: first *fully* escape ALL braces (``{`` → ``{{``, ``}`` →
    ``}}``), then restore known placeholders from ``{{key}}`` back to
    ``{key}`` so they are substituted by ``str.format``.  This handles
    identifiers, format specs (``{key:.2f}``), conversions (``{key!s}``),
    auto-numbered ``{}``, positional ``{0}``, and JSON examples like
    ``{"buy_low": 100}`` — nested JSON is handled correctly because all
    braces are escaped first.
    """

    # Step 1: fully escape ALL braces
    escaped = template.replace("{", "{{").replace("}", "}}")

    # Step 2: restore known placeholders from {{key}} back to {key}
    def _restore(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key in allowed_keys:
            full = match.group(0)
            # Remove one level of escaping: {{key...}} -> {key...}
            return "{" + full[2:-2] + "}"
        return match.group(0)

    return _ESCAPED_PLACEHOLDER.sub(_restore, escaped)


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
            context_for_template = self._build_template_context(
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
                market_data=market_data,
                account_context=account_context,
                recent_prices=recent_prices,
                recent_analysis=recent_analysis,
            )
            rendered_template = _escape_orphan_braces(
                prompt_template, set(context_for_template.keys())
            ).format(**context_for_template)
            return f"{system_instructions}\n\n{rendered_template}"
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

    def _build_template_context(
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
        market_data: dict[str, Any],
        account_context: dict[str, Any] | None,
        recent_prices: list[dict[str, Any]] | None,
        recent_analysis: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the context dict used to render custom prompt templates."""
        return {
            "symbol": symbol,
            "market": market,
            "current_price": current_price,
            "current_buy_low": current_buy_low,
            "current_sell_high": current_sell_high,
            "short_selling": short_selling,
            "current_position": current_position,
            "recent_trades": recent_trades,
            "position_quantity": position_quantity,
            "position_avg_price": position_avg_price,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "min_profit_amount": min_profit_amount,
            "daily_candles": market_data.get("daily_candles", []),
            "minute_candles": market_data.get("minute_candles", []),
            "atr": market_data.get("atr", 0.0),
            "bb_upper": market_data.get("bb_upper", 0.0),
            "bb_middle": market_data.get("bb_middle", 0.0),
            "bb_lower": market_data.get("bb_lower", 0.0),
            "rsi": market_data.get("rsi", 0.0),
            "macd": market_data.get("macd") or {"macd": 0.0, "signal": 0.0, "histogram": 0.0},
            "volume_analysis": market_data.get("volume_analysis")
            or {"avg_volume": 0.0, "volume_ratio": 0.0, "trend": "unknown"},
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

    def _call_llm(self, prompt: str) -> str:
        """Call the configured LLM provider."""
        if settings.llm_provider == "minimax":
            return self._call_minimax(prompt)
        if settings.llm_provider == "deepseek":
            return self._call_deepseek(prompt)
        raise RuntimeError(f"Unsupported LLM provider: {settings.llm_provider}")

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
        with _throttle_lock:
            if not force and self._is_throttled(interval_minutes * 60):
                return {
                    "success": False,
                    "error": f"Analysis throttled: please wait {interval_minutes} minutes between analyses",
                }
            # Reserve slot: set to inf so no concurrent request can pass
            # the throttle check while the LLM call is in flight.
            _prev_analysis_ts = _LAST_ANALYSIS_TIMESTAMP
            _LAST_ANALYSIS_TIMESTAMP = float("inf")

        try:
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

            try:
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
            except Exception:
                # _select_variant or _build_prompt failed — release the reserved
                # slot so the throttle is not permanently locked at inf.
                with _throttle_lock:
                    _LAST_ANALYSIS_TIMESTAMP = _prev_analysis_ts
                raise
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
                # LLM succeeded — consume the throttle budget.
                with _throttle_lock:
                    _LAST_ANALYSIS_TIMESTAMP = time.monotonic()

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
                # LLM call or parse failed — release the reserved slot so the
                # next caller is not needlessly blocked.
                with _throttle_lock:
                    _LAST_ANALYSIS_TIMESTAMP = _prev_analysis_ts
                logger.warning("LLM analysis failed: %s", exc)
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
            if persist:
                db = SessionLocal()
                try:
                    next_analysis_at = self._record_analysis(
                        db, result, interval_minutes, current_price=current_price,
                    )
                except Exception:
                    logger.exception("failed to record LLM analysis")
                finally:
                    db.close()

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
        finally:
            # Ensure the throttle slot is always released after an
            # unhandled exception, preventing permanent throttle blockage.
            with _throttle_lock:
                if _LAST_ANALYSIS_TIMESTAMP == float("inf"):
                    _LAST_ANALYSIS_TIMESTAMP = _prev_analysis_ts

    def _preview_market_data(self, symbol: str, market: str, current_price: float) -> dict[str, Any]:
        try:
            return self._data_aggregator.fetch_market_data(symbol, market)
        except Exception:
            logger.exception("failed to fetch market data for LLM preview")
            return {
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

    @staticmethod
    def _preview_prompt_price(market_data: dict[str, Any], current_price: float) -> float:
        current_price_raw = market_data.get("current_price")
        if isinstance(current_price_raw, (int, float)):
            return float(current_price_raw)
        return float(current_price)

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

        market_data: dict[str, Any] | None = None
        prompt_price = float(current_price)
        if prompt_price <= 0:
            market_data = self._preview_market_data(symbol, market, current_price)
            prompt_price = self._preview_prompt_price(market_data, current_price)
            if prompt_price <= 0:
                return {"success": False, "applied": False, "error": "Market data unavailable for preview"}

        with _throttle_lock:
            now_monotonic = time.monotonic()
            if _LAST_PREVIEW_TIMESTAMP <= 0:
                pass  # cold-start bypass
            elif _LAST_PREVIEW_TIMESTAMP == float("inf") or (
                now_monotonic >= _LAST_PREVIEW_TIMESTAMP
                and now_monotonic - _LAST_PREVIEW_TIMESTAMP < _PREVIEW_THROTTLE_SECONDS
            ):
                return {
                    "success": False,
                    "applied": False,
                    "error": "Preview throttled: please wait before requesting another preview",
                }
            # Reserve slot: set to inf so no concurrent request can pass
            # the throttle check while the LLM call is in flight.
            _prev_preview_ts = _LAST_PREVIEW_TIMESTAMP
            _LAST_PREVIEW_TIMESTAMP = float("inf")

        try:
            if market_data is None:
                market_data = self._preview_market_data(symbol, market, current_price)
                prompt_price = self._preview_prompt_price(market_data, current_price)
                if prompt_price <= 0:
                    # Market data unavailable — release the reserved slot.
                    with _throttle_lock:
                        _LAST_PREVIEW_TIMESTAMP = _prev_preview_ts
                    return {"success": False, "applied": False, "error": "Market data unavailable for preview"}

            try:
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
            except Exception:
                # _select_variant or _build_prompt failed — release the reserved
                # slot so the throttle is not permanently locked at inf.
                with _throttle_lock:
                    _LAST_PREVIEW_TIMESTAMP = _prev_preview_ts
                raise
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
                # LLM succeeded — consume the throttle budget.
                with _throttle_lock:
                    _LAST_PREVIEW_TIMESTAMP = time.monotonic()
            except Exception as exc:
                # LLM call or parse failed — release the reserved slot.
                with _throttle_lock:
                    _LAST_PREVIEW_TIMESTAMP = _prev_preview_ts
                logger.warning("LLM preview failed: %s", exc)
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
        finally:
            # Ensure the throttle slot is always released after an
            # unhandled exception, preventing permanent throttle blockage.
            with _throttle_lock:
                if _LAST_PREVIEW_TIMESTAMP == float("inf"):
                    _LAST_PREVIEW_TIMESTAMP = _prev_preview_ts

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

    @staticmethod
    def _minimax_chat_payload(prompt: str) -> dict[str, object]:
        return {
            "model": settings.minimax_model,
            "messages": [
                {"role": "system", "content": "You are a professional quantitative trading advisor."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_completion_tokens": settings.minimax_max_completion_tokens,
            "thinking": {"type": settings.minimax_thinking_type},
        }

    @staticmethod
    def _minimax_chat_url() -> str:
        api_url = settings.minimax_api_url.strip()
        if api_url:
            normalized = api_url.rstrip("/")
            if normalized.endswith("/v1"):
                return f"{normalized}/chat/completions"
            return api_url

        base_url = settings.minimax_base_url.strip().rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

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
            if not isinstance(message, dict):
                raise RuntimeError(f"invalid message payload type: {type(message).__name__}")
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

    def _call_minimax(self, prompt: str) -> str:
        """Call MiniMax OpenAI-compatible Chat Completions API with the prompt."""
        if not settings.minimax_api_key:
            raise RuntimeError("MINIMAX_API_KEY is not configured")

        try:
            response = httpx.post(
                self._minimax_chat_url(),
                headers={
                    "Authorization": f"Bearer {settings.minimax_api_key}",
                    "Content-Type": "application/json",
                },
                json=self._minimax_chat_payload(prompt),
                timeout=120.0,
            )
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"MiniMax request timeout: {exc}") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"MiniMax request failed: {exc}") from exc

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"MiniMax HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"MiniMax returned non-JSON response: {response.text[:200]}"
            ) from exc

        try:
            choice = data["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise RuntimeError(f"invalid message payload type: {type(message).__name__}")
            content = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"MiniMax response missing message content: {exc}") from exc
        if not content.strip():
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
            raise RuntimeError(
                "MiniMax returned empty content"
                f" (finish_reason={finish_reason}, usage={usage},"
                f" max_completion_tokens={settings.minimax_max_completion_tokens})."
            )
        return content

    @staticmethod
    def _parse_response(raw: str) -> dict[str, Any]:
        """Parse JSON from LLM response."""
        raw = LLMAdvisorService._strip_response_wrappers(raw)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            json_object = LLMAdvisorService._extract_first_json_object(raw)
            if json_object is None:
                raise
            parsed = json.loads(json_object)
        if not isinstance(parsed, dict):
            raise TypeError(
                f"LLM response must be a JSON object, got {type(parsed).__name__}"
            )
        result = LLMAdvisorService._normalize_response(parsed)
        LLMAdvisorService._validate_interval_response(result)
        return result

    @staticmethod
    def _strip_response_wrappers(raw: str) -> str:
        """Remove common non-JSON wrappers while preserving the model payload."""
        raw = _THINK_BLOCK.sub("", raw).strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        if raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        return raw.strip()

    @staticmethod
    def _extract_first_json_object(raw: str) -> str | None:
        """Extract the first balanced JSON object from prose-wrapped LLM output."""
        start: int | None = None
        depth = 0
        in_string = False
        escaped = False
        for index, char in enumerate(raw):
            if start is None:
                if char == "{":
                    start = index
                    depth = 1
                continue
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return raw[start : index + 1]
        return None

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
        # Reject non-positive or non-finite order/replacement prices. The
        # execution layer cannot accept ``<= 0`` or NaN/Inf; treat them as
        # "no price provided" so downstream guards fall back to a market
        # order or skip the action altogether.
        if not LLMAdvisorService._is_finite_positive(result.get("order_price")):
            result["order_price"] = None
        if not LLMAdvisorService._is_finite_positive(result.get("replacement_price")):
            result["replacement_price"] = None
        return result

    @staticmethod
    def _validate_interval_response(result: dict[str, Any]) -> None:
        buy_low = LLMAdvisorService._coerce_required_float(result, "suggested_buy_low")
        sell_high = LLMAdvisorService._coerce_required_float(result, "suggested_sell_high")
        confidence = LLMAdvisorService._coerce_required_float(result, "confidence_score")
        if buy_low <= 0:
            raise ValueError("LLM response suggested_buy_low must be positive")
        if sell_high <= 0:
            raise ValueError("LLM response suggested_sell_high must be positive")
        if sell_high <= buy_low:
            raise ValueError("LLM response suggested_sell_high must be greater than suggested_buy_low")
        if confidence < 0 or confidence > 1:
            raise ValueError("LLM response confidence_score must be between 0 and 1")
        result["suggested_buy_low"] = buy_low
        result["suggested_sell_high"] = sell_high
        result["confidence_score"] = confidence

    @staticmethod
    def _coerce_required_float(result: dict[str, Any], field_name: str) -> float:
        value = result.get(field_name)
        if isinstance(value, bool) or value is None:
            raise ValueError(f"LLM response missing valid {field_name}")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"LLM response missing valid {field_name}") from exc
        if not math.isfinite(number):
            raise ValueError(f"LLM response missing valid {field_name}")
        return number

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

    def _record_analysis(
        self,
        db: Any,
        result: dict[str, Any],
        interval_minutes: int,
        current_price: float | None = None,
    ) -> datetime:
        """Record LLM analysis result to database.

        ``suggested_buy_low`` / ``suggested_sell_high`` are clipped to
        ``[current_price * 0.5, current_price * 2]`` to reject pathological
        values (a hallucinated negative price, or a price an order of
        magnitude away from the market). Out-of-range or non-finite values
        are dropped (stored as ``None``) so downstream consumers can detect
        the missing signal.
        """
        svc = StrategyService(db)
        config = svc.get_config()
        now = datetime.now(timezone.utc)
        next_analysis_at = now + timedelta(minutes=interval_minutes)
        buy_low_raw = result.get("suggested_buy_low")
        sell_high_raw = result.get("suggested_sell_high")
        if current_price is not None and current_price > 0:
            low_floor = current_price * 0.5
            high_ceiling = current_price * 2.0
            buy_low = self._clip_to_range(buy_low_raw, low_floor, high_ceiling)
            sell_high = self._clip_to_range(sell_high_raw, low_floor, high_ceiling)
        else:
            buy_low = buy_low_raw if self._is_finite_positive(buy_low_raw) else None
            sell_high = sell_high_raw if self._is_finite_positive(sell_high_raw) else None
        config.llm_suggested_buy_low = buy_low
        config.llm_suggested_sell_high = sell_high
        config.llm_confidence_score = result.get("confidence_score")
        config.llm_analysis = result.get("analysis")
        config.llm_last_analysis_at = now
        config.llm_next_analysis_at = next_analysis_at
        db.commit()
        return next_analysis_at

    @staticmethod
    def _is_finite_positive(value: Any) -> bool:
        return isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0

    @staticmethod
    def _clip_to_range(value: Any, low: float, high: float) -> float | None:
        if not isinstance(value, (int, float)):
            return None
        v = float(value)
        if not math.isfinite(v) or v <= 0:
            return None
        if v < low:
            return low
        if v > high:
            return high
        return v

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
