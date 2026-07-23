from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import WatchlistScore

logger = logging.getLogger("auto_trade.watchlist_score_service")

# Hard-bounded fallbacks: when LLM is unavailable or returns malformed
# output, the service still returns a deterministic neutral score.
DEFAULT_SCORE = 50.0
DEFAULT_CONFIDENCE = 0.0
DEFAULT_ACTION = "HOLD"
DEFAULT_RETENTION_DAYS = 30

# Loose JSON object match — the LLM is asked to return {"score": N, ...}
# but we never trust the raw text; we extract defensively.
_JSON_FIELD_PATTERNS = {
    "score": re.compile(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)', re.IGNORECASE),
    "confidence": re.compile(r'"confidence"\s*:\s*(-?\d+(?:\.\d+)?)', re.IGNORECASE),
    "action": re.compile(r'"recommended_action"\s*:\s*"([A-Z_]+)"', re.IGNORECASE),
    "rationale": re.compile(r'"rationale"\s*:\s*"([^"]*)"', re.IGNORECASE),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_stale(score: WatchlistScore, now: datetime) -> bool:
    if score.expires_at is None:
        return True
    expires = score.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


def _parse_llm_payload(text: str) -> dict[str, object]:
    """Best-effort extraction of the LLM scoring payload.

    Returns a dict with keys ``score``, ``confidence``, ``recommended_action``,
    and ``rationale``. Missing fields fall back to defaults. The LLM is asked
    to return JSON, but we tolerate prose, markdown code fences, and minor
    formatting drift so the feature degrades gracefully.
    """
    if not text:
        return {}
    blob = text.strip()
    # Strip ```json ... ``` fences if present.
    if blob.startswith("```"):
        first_newline = blob.find("\n")
        if first_newline != -1:
            blob = blob[first_newline + 1 :]
        if blob.endswith("```"):
            blob = blob[:-3]
        blob = blob.strip()

    out: dict[str, object] = {}
    for key, pattern in _JSON_FIELD_PATTERNS.items():
        match = pattern.search(blob)
        if not match:
            continue
        value = match.group(1)
        # Map the regex key ``action`` to the public field name
        # ``recommended_action`` so callers don't have to know about the
        # internal naming.
        out_key = "recommended_action" if key == "action" else key
        if key in ("score", "confidence"):
            try:
                out[out_key] = float(value)
            except ValueError:
                continue
        else:
            out[out_key] = value
    return out


class WatchlistScoreService:
    """Read-through cache for watchlist LLM scores.

    Scoring requires a live LLM advisor. If the advisor is not configured
    or fails, this service returns a deterministic fallback rather than
    raising — the watchlist UI must stay usable even when the LLM is down.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_latest(self, symbol: str) -> Optional[WatchlistScore]:
        return (
            self.db.query(WatchlistScore)
            .filter(WatchlistScore.symbol == symbol)
            .order_by(WatchlistScore.created_at.desc())
            .first()
        )

    @staticmethod
    def source_family(source: str) -> str:
        return "quant" if source.startswith("quant_") else "review"

    def list_latest_per_symbol_and_family(
        self,
    ) -> list[WatchlistScore]:
        """Return the newest quant and review row independently per symbol."""
        family = case(
            (
                WatchlistScore.source.like("quant_%"),
                "quant",
            ),
            else_="review",
        )
        ranked = select(
            WatchlistScore.id.label("score_id"),
            func.row_number().over(
                partition_by=(WatchlistScore.symbol, family),
                order_by=(
                    WatchlistScore.created_at.desc(),
                    WatchlistScore.id.desc(),
                ),
            ).label("family_rank"),
        ).subquery()
        return (
            self.db.query(WatchlistScore)
            .join(ranked, ranked.c.score_id == WatchlistScore.id)
            .filter(ranked.c.family_rank == 1)
            .order_by(
                WatchlistScore.symbol.asc(),
                WatchlistScore.created_at.desc(),
            )
            .all()
        )

    def list_latest_per_symbol(self, now: Optional[datetime] = None) -> list[WatchlistScore]:
        """Return one primary row per symbol, preferring quant selection.

        AI review is supplementary evidence and must not overwrite the
        deterministic quant candidate tier. Symbols without a quant score
        retain the newest review for backwards compatibility.
        """
        del now
        by_symbol: dict[str, WatchlistScore] = {}
        for row in self.list_latest_per_symbol_and_family():
            existing = by_symbol.get(row.symbol)
            if existing is None or self.source_family(row.source) == "quant":
                by_symbol[row.symbol] = row
        return list(by_symbol.values())

    def record_score(
        self,
        *,
        symbol: str,
        market: str,
        score: float,
        rationale: str = "",
        confidence: float = DEFAULT_CONFIDENCE,
        recommended_action: str = DEFAULT_ACTION,
        source: str = "llm",
        ttl_minutes: int = 60,
        commit: bool = True,
    ) -> WatchlistScore:
        now = _utcnow()
        expires = now + timedelta(minutes=max(1, int(ttl_minutes)))
        row = WatchlistScore(
            symbol=symbol,
            market=market,
            score=_clamp(float(score), 0.0, 100.0),
            rationale=(rationale or "")[:4000],
            confidence=_clamp(float(confidence), 0.0, 1.0),
            recommended_action=(recommended_action or DEFAULT_ACTION).upper()[:16],
            source=(source or "llm")[:32],
            created_at=now,
            expires_at=expires,
        )
        self.db.add(row)
        if commit:
            self.db.commit()
            self.db.refresh(row)
        return row

    def prune_history(
        self,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        now: datetime | None = None,
        commit: bool = True,
    ) -> int:
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        cutoff = (now or _utcnow()) - timedelta(days=retention_days)
        deleted = (
            self.db.query(WatchlistScore)
            .filter(WatchlistScore.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        if commit:
            self.db.commit()
        return int(deleted)

    def is_fresh(self, row: WatchlistScore, now: Optional[datetime] = None) -> bool:
        return not _is_stale(row, now or _utcnow())

    def score_from_llm_or_fallback(
        self,
        *,
        symbol: str,
        market: str,
        ttl_minutes: int = 60,
    ) -> WatchlistScore:
        """Try to score ``symbol`` via the LLM advisor.

        On any failure (no key, timeout, malformed response, exception) we
        return a fresh row with the neutral defaults so the UI shows
        *something*. The ``source`` field marks fallback rows.
        """
        try:
            from app.config import settings

            from app.services.llm_advisor_service import LLMAdvisorService

            provider = str(
                getattr(settings, "llm_provider", "deepseek")
            ).lower()
            configured_key = (
                getattr(settings, "minimax_api_key", "")
                if provider == "minimax"
                else getattr(settings, "deepseek_api_key", "")
            )
            if not configured_key:
                return self.record_score(
                    symbol=symbol,
                    market=market,
                    score=DEFAULT_SCORE,
                    rationale="LLM advisor not configured; using neutral fallback.",
                    confidence=DEFAULT_CONFIDENCE,
                    recommended_action=DEFAULT_ACTION,
                    source="fallback_unconfigured",
                    ttl_minutes=ttl_minutes,
                )

            advisor = LLMAdvisorService(broker=None)

            prompt = (
                f"Score the watchlist symbol {symbol} ({market}) on a 0..100 "
                "scale for short-term trade attractiveness. Return ONLY a JSON "
                "object with keys score (number 0..100), confidence (number 0..1), "
                "recommended_action (one of BUY/SELL/HOLD/AVOID), rationale "
                "(one short sentence)."
            )
            raw = advisor._call_llm(prompt).content
            if not raw:
                return self.record_score(
                    symbol=symbol,
                    market=market,
                    score=DEFAULT_SCORE,
                    rationale="LLM did not return a response; using neutral fallback.",
                    confidence=DEFAULT_CONFIDENCE,
                    recommended_action=DEFAULT_ACTION,
                    source="fallback_empty",
                    ttl_minutes=ttl_minutes,
                )

            parsed = _parse_llm_payload(str(raw))
            score_value = parsed.get("score", DEFAULT_SCORE)
            confidence_value = parsed.get("confidence", DEFAULT_CONFIDENCE)
            if not isinstance(score_value, (int, float, str)):
                score_value = DEFAULT_SCORE
            if not isinstance(confidence_value, (int, float, str)):
                confidence_value = DEFAULT_CONFIDENCE
            action_value = str(parsed.get("recommended_action", DEFAULT_ACTION)).upper()
            if action_value not in ("BUY", "SELL", "HOLD", "AVOID"):
                action_value = DEFAULT_ACTION
            rationale_value = str(parsed.get("rationale", ""))[:4000]
            return self.record_score(
                symbol=symbol,
                market=market,
                score=float(score_value),
                rationale=rationale_value,
                confidence=float(confidence_value),
                recommended_action=action_value,
                source="llm",
                ttl_minutes=ttl_minutes,
            )
        except Exception:
            logger.exception("Watchlist LLM scoring failed for %s; falling back", symbol)
            return self.record_score(
                symbol=symbol,
                market=market,
                score=DEFAULT_SCORE,
                rationale="LLM scoring error; using neutral fallback.",
                confidence=DEFAULT_CONFIDENCE,
                recommended_action=DEFAULT_ACTION,
                source="fallback_error",
                ttl_minutes=ttl_minutes,
            )
