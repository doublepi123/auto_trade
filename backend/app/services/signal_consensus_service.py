"""Signal consensus matrix — cross-source aggregation of bullish/bearish votes.

The matrix is a *read-only* observation layer. It never places orders and
never mutates any persisted state; it simply joins five existing signal
sources (range engine, Strategy v2 shadow, opening momentum shadow, watchlist
quant scores, LLM advisor) and rolls them up into a per-symbol consensus.

This deliberately mirrors the project's P0 live-safety rule: shadow / research
paths are observation-only. The matrix is one more observer on top of them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import (
    LLMInteraction,
    OpeningMomentumShadowRun,
    StrategyConfig,
    StrategyV2ShadowTrade,
    TradeEvent,
    WatchlistScore,
)

logger = logging.getLogger("auto_trade.signal_consensus")

SignalLabel = Literal["BULLISH", "BEARISH", "NEUTRAL", "NO_DATA"]
ConsensusLabel = Literal["AGREE_BULLISH", "AGREE_BEARISH", "MIXED", "INSUFFICIENT_DATA"]

# A signal source is counted toward the majority only when it contributes a
# concrete bullish/bearish vote. NEUTRAL and NO_DATA are abstentions.
_VOTES = {"BULLISH", "BEARISH"}

# Minimum number of concrete votes required before we declare a majority;
# below this the consensus is INSUFFICIENT_DATA even if the few votes agree.
_MIN_VOTES_FOR_CONSENSUS = 2


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class SignalConsensusService:
    """Aggregate per-symbol signal votes across five read-only sources."""

    SOURCE_NAMES: tuple[str, ...] = (
        "range_engine",
        "strategy_v2_shadow",
        "opening_momentum",
        "quant_score",
        "llm_advisor",
    )

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_matrix(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """Return one consensus row per symbol.

        ``symbols`` is normalized (uppercased, deduplicated, empties dropped).
        When ``None`` the union of symbols observed across all five sources is
        used. An empty/blank symbol set therefore yields ``[]`` rather than a
        synthetic "no symbol" row.
        """
        resolved = self._resolve_symbols(symbols)
        if not resolved:
            return []

        rows: list[dict[str, Any]] = []
        for symbol in resolved:
            sources = self._collect_sources(symbol)
            consensus, agreement_score = self._compute_consensus(sources)
            rows.append(
                {
                    "symbol": symbol,
                    "sources": sources,
                    "consensus": consensus,
                    "agreement_score": agreement_score,
                }
            )
        # Stable ordering: most-agreed first, then symbol for determinism.
        rows.sort(
            key=lambda row: (
                -row["agreement_score"],
                _consensus_sort_rank(row["consensus"]),
                row["symbol"],
            )
        )
        return rows

    def get_summary(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Counts of each consensus bucket across the matrix."""
        rows = self.get_matrix(symbols)
        counts: dict[str, int] = {
            "agree_bullish": 0,
            "agree_bearish": 0,
            "mixed": 0,
            "insufficient": 0,
        }
        for row in rows:
            consensus = row["consensus"]
            if consensus == "AGREE_BULLISH":
                counts["agree_bullish"] += 1
            elif consensus == "AGREE_BEARISH":
                counts["agree_bearish"] += 1
            elif consensus == "MIXED":
                counts["mixed"] += 1
            else:
                counts["insufficient"] += 1
        return {
            "total_symbols": len(rows),
            "agree_bullish": counts["agree_bullish"],
            "agree_bearish": counts["agree_bearish"],
            "mixed": counts["mixed"],
            "insufficient": counts["insufficient"],
        }

    # ------------------------------------------------------------------
    # Symbol resolution
    # ------------------------------------------------------------------
    def _resolve_symbols(self, symbols: list[str] | None) -> list[str]:
        if symbols is not None:
            cleaned = [s.strip().upper() for s in symbols if s and s.strip()]
            # Preserve caller order while dropping duplicates.
            seen: set[str] = set()
            ordered: list[str] = []
            for s in cleaned:
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            return ordered

        discovered: set[str] = set()
        for symbol in self._db.query(StrategyConfig.symbol).all():
            self._add_symbol(discovered, symbol[0])
        for symbol in self._db.query(StrategyV2ShadowTrade.symbol).all():
            self._add_symbol(discovered, symbol[0])
        for symbol in self._db.query(OpeningMomentumShadowRun.candidate_symbol).all():
            self._add_symbol(discovered, symbol[0])
        for symbol in self._db.query(WatchlistScore.symbol).all():
            self._add_symbol(discovered, symbol[0])
        for symbol in self._db.query(LLMInteraction.symbol).all():
            self._add_symbol(discovered, symbol[0])
        return sorted(discovered)

    @staticmethod
    def _add_symbol(bucket: set[str], raw: str | None) -> None:
        if not raw:
            return
        normalized = raw.strip().upper()
        if normalized:
            bucket.add(normalized)

    # ------------------------------------------------------------------
    # Per-source collectors
    # ------------------------------------------------------------------
    def _collect_sources(self, symbol: str) -> dict[str, dict[str, Any]]:
        return {
            "range_engine": self._range_engine_signal(symbol),
            "strategy_v2_shadow": self._strategy_v2_shadow_signal(symbol),
            "opening_momentum": self._opening_momentum_signal(symbol),
            "quant_score": self._quant_score_signal(symbol),
            "llm_advisor": self._llm_advisor_signal(symbol),
        }

    def _range_engine_signal(self, symbol: str) -> dict[str, Any]:
        config = (
            self._db.query(StrategyConfig)
            .filter(StrategyConfig.symbol == symbol)
            .order_by(StrategyConfig.id.desc())
            .first()
        )
        if config is None:
            return self._no_data("range engine not configured")

        buy_low = float(config.buy_low or 0.0)
        sell_high = float(config.sell_high or 0.0)
        if buy_low <= 0 or sell_high <= 0 or sell_high <= buy_low:
            return self._no_data("range engine levels not set")

        price = self._latest_price(symbol)
        if price is None or price <= 0:
            return self._no_data("no recent price available")

        # How far through the [buy_low, sell_high] band the price sits.
        band = sell_high - buy_low
        position = (price - buy_low) / band if band > 0 else 0.0
        if position <= 0.0:
            signal: SignalLabel = "BULLISH"
            confidence = self._clamp(1.0 - position)
            detail = (
                f"price {price:.4f} at/below buy_low {buy_low:.4f} "
                f"(sell_high={sell_high:.4f})"
            )
        elif position >= 1.0:
            signal = "BEARISH"
            overshoot = position - 1.0
            confidence = self._clamp(1.0 + overshoot, low=0.0, high=1.0)
            detail = (
                f"price {price:.4f} at/above sell_high {sell_high:.4f} "
                f"(buy_low={buy_low:.4f})"
            )
        else:
            # Distance from the band midpoint, normalized to [0, 0.5].
            signal = "NEUTRAL"
            confidence = self._clamp(abs(position - 0.5) * 2.0)
            detail = (
                f"price {price:.4f} inside band "
                f"[{buy_low:.4f}, {sell_high:.4f}] (mid={buy_low + band / 2:.4f})"
            )
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "detail": detail,
            "updated_at": _to_iso(config.updated_at),
        }

    def _strategy_v2_shadow_signal(self, symbol: str) -> dict[str, Any]:
        trade = (
            self._db.query(StrategyV2ShadowTrade)
            .filter(StrategyV2ShadowTrade.symbol == symbol)
            .order_by(
                StrategyV2ShadowTrade.entry_at.desc(),
                StrategyV2ShadowTrade.id.desc(),
            )
            .first()
        )
        if trade is None:
            return self._no_data("no Strategy v2 shadow trade")
        status = (trade.status or "").upper()
        # An OPEN long is a bullish stance; a recently CLOSED trade's net PnL
        # is only a weak signal, so we encode it as NEUTRAL unless the exit
        # reason explicitly indicates a stop-out (bearish) or target hit.
        if status == "OPEN":
            return {
                "signal": "BULLISH",
                "confidence": 0.7,
                "detail": f"shadow trade OPEN since {trade.entry_at}",
                "updated_at": _to_iso(trade.updated_at),
            }
        if status in {"CLOSED", "EXITED"}:
            exit_reason = (trade.exit_reason or "").upper()
            if "STOP" in exit_reason:
                signal: SignalLabel = "BEARISH"
                confidence = 0.6
            elif "TARGET" in exit_reason or "PROFIT" in exit_reason:
                signal = "BULLISH"
                confidence = 0.55
            else:
                signal = "NEUTRAL"
                confidence = 0.3
            return {
                "signal": signal,
                "confidence": round(confidence, 4),
                "detail": (
                    f"shadow trade CLOSED ({trade.exit_reason or 'n/a'}, "
                    f"net_pnl={trade.net_pnl})"
                ),
                "updated_at": _to_iso(trade.updated_at or trade.exit_at),
            }
        return self._no_data(f"unknown shadow trade status '{trade.status}'")

    def _opening_momentum_signal(self, symbol: str) -> dict[str, Any]:
        run = (
            self._db.query(OpeningMomentumShadowRun)
            .filter(OpeningMomentumShadowRun.candidate_symbol == symbol)
            .order_by(
                OpeningMomentumShadowRun.signal_at.desc(),
                OpeningMomentumShadowRun.id.desc(),
            )
            .first()
        )
        if run is None:
            return self._no_data("no opening momentum observation")
        status = (run.status or "").upper()
        # ENTRY/ARMED/OPEN -> bullish (the candidate was selected). SKIP/REJECT
        # or a realized negative net return is bearish. Otherwise neutral.
        if status in {"ENTRY", "ARMED", "OPEN", "ENTERED"}:
            signal: SignalLabel = "BULLISH"
            confidence = 0.6
        elif status in {"SKIP", "REJECT", "REJECTED", "EXCLUDED"}:
            signal = "BEARISH"
            confidence = 0.5
        elif run.net_return_bps is not None and run.net_return_bps > 0:
            signal = "BULLISH"
            confidence = 0.5
        elif run.net_return_bps is not None and run.net_return_bps < 0:
            signal = "BEARISH"
            confidence = 0.5
        else:
            signal = "NEUTRAL"
            confidence = 0.3
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "detail": (
                f"opening momentum run status={run.status} "
                f"excess_return_bps={run.excess_return_bps}"
            ),
            "updated_at": _to_iso(run.updated_at or run.signal_at),
        }

    def _quant_score_signal(self, symbol: str) -> dict[str, Any]:
        score = (
            self._db.query(WatchlistScore)
            .filter(WatchlistScore.symbol == symbol)
            .order_by(
                WatchlistScore.created_at.desc(),
                WatchlistScore.id.desc(),
            )
            .first()
        )
        if score is None:
            return self._no_data("no quant score")
        action = (score.recommended_action or "").upper()
        if action in {"BUY", "STRONG_BUY"}:
            signal: SignalLabel = "BULLISH"
        elif action in {"SELL", "STRONG_SELL", "AVOID"}:
            signal = "BEARISH"
        elif action in {"HOLD", "NEUTRAL"}:
            signal = "NEUTRAL"
        else:
            signal = "NEUTRAL"
        # confidence column is stored 0..1; score is 0..100.
        confidence = self._clamp(float(score.confidence or 0.0))
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "detail": (
                f"quant action={score.recommended_action} score={score.score:.1f}"
            ),
            "updated_at": _to_iso(score.created_at),
        }

    def _llm_advisor_signal(self, symbol: str) -> dict[str, Any]:
        interaction = (
            self._db.query(LLMInteraction)
            .filter(LLMInteraction.symbol == symbol)
            .order_by(
                LLMInteraction.created_at.desc(),
                LLMInteraction.id.desc(),
            )
            .first()
        )
        if interaction is None:
            return self._no_data("no LLM interaction")
        action = (interaction.order_action or "").upper()
        if action in {"BUY", "RAISE_BUY_LOW", "SUGGEST_BUY", "ENTER_LONG"}:
            signal: SignalLabel = "BULLISH"
            confidence = 0.6
        elif action in {"SELL", "LOWER_SELL_HIGH", "SUGGEST_SELL", "EXIT_LONG"}:
            signal = "BEARISH"
            confidence = 0.6
        elif action in {"HOLD", "NONE", "WAIT", ""}:
            signal = "NEUTRAL"
            confidence = 0.3
        else:
            signal = "NEUTRAL"
            confidence = 0.3
        return {
            "signal": signal,
            "confidence": round(confidence, 4),
            "detail": (
                f"LLM action={interaction.order_action} "
                f"success={interaction.success}"
            ),
            "updated_at": _to_iso(interaction.created_at),
        }

    # ------------------------------------------------------------------
    # Price lookup + helpers
    # ------------------------------------------------------------------
    def _latest_price(self, symbol: str) -> float | None:
        """Best-effort latest price for ``symbol`` from TradeEvent rows.

        There is no dedicated PRICE_UPDATE event type in the live path; prices
        surface through ORDER_SKIPPED / ORDER_SUBMITTED payloads and the
        ``last_price`` field recorded on RuntimeState. We therefore pull the
        newest trade event for the symbol and extract any price-like field
        from its payload (``price``, ``last_price``, ``decision_price``).
        """
        event = (
            self._db.query(TradeEvent)
            .filter(TradeEvent.symbol == symbol)
            .order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc())
            .first()
        )
        if event is None:
            return None
        payload = _parse_json(event.payload_json)
        for key in ("last_price", "price", "decision_price"):
            candidate = payload.get(key)
            try:
                if candidate is not None and float(candidate) > 0:
                    return float(candidate)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _no_data(reason: str) -> dict[str, Any]:
        return {
            "signal": "NO_DATA",
            "confidence": 0.0,
            "detail": reason,
            "updated_at": None,
        }

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))

    # ------------------------------------------------------------------
    # Consensus math
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_consensus(
        sources: dict[str, dict[str, Any]],
    ) -> tuple[ConsensusLabel, float]:
        votes = [
            src["signal"]
            for src in sources.values()
            if src.get("signal") in _VOTES
        ]
        if len(votes) < _MIN_VOTES_FOR_CONSENSUS:
            return "INSUFFICIENT_DATA", 0.0

        bullish = sum(1 for v in votes if v == "BULLISH")
        bearish = sum(1 for v in votes if v == "BEARISH")

        if bullish > 0 and bearish == 0:
            majority: SignalLabel = "BULLISH"
            agreement = bullish
        elif bearish > 0 and bullish == 0:
            majority = "BEARISH"
            agreement = bearish
        else:
            # Both sides present — MIXED. Agreement is the larger faction.
            return "MIXED", round(max(bullish, bearish) / len(votes), 4)

        return (
            "AGREE_BULLISH" if majority == "BULLISH" else "AGREE_BEARISH",
            round(agreement / len(votes), 4),
        )


def _consensus_sort_rank(label: str) -> int:
    """Lower sorts first; used only for deterministic matrix ordering."""
    order = {
        "AGREE_BULLISH": 0,
        "AGREE_BEARISH": 1,
        "MIXED": 2,
        "INSUFFICIENT_DATA": 3,
    }
    return order.get(label, 9)
