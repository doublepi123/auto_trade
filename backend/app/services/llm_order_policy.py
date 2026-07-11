from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


class LLMOrderDisposition(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    SHADOW = "SHADOW"


_TRADE_ACTIONS = {
    "BUY_NOW",
    "SELL_NOW",
    "SELL_SHORT_NOW",
    "BUY_TO_COVER_NOW",
    "STOP_LOSS_SELL_NOW",
    "STOP_LOSS_COVER_NOW",
}
_SUPPORTED_ACTIONS = _TRADE_ACTIONS | {"CANCEL_PENDING", "CANCEL_REPLACE"}


@dataclass(frozen=True)
class LLMOrderPolicyDecision:
    disposition: LLMOrderDisposition
    code: str
    reason: str
    confidence: float | None = None
    reference_price: float | None = None
    candidate_price: float | None = None
    deviation_pct: float | None = None

    @property
    def allowed(self) -> bool:
        return self.disposition == LLMOrderDisposition.ALLOW

    def to_result(self, action: str) -> dict[str, Any]:
        status = "SHADOW_ONLY" if self.disposition == LLMOrderDisposition.SHADOW else "POLICY_REJECTED"
        return {
            "executed": False,
            "status": status,
            "order_id": None,
            "action": action,
            "policy_code": self.code,
            "policy_disposition": self.disposition.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "reference_price": self.reference_price,
            "candidate_price": self.candidate_price,
            "deviation_pct": self.deviation_pct,
        }


def evaluate_llm_order_policy(
    decision: dict[str, Any],
    *,
    min_confidence: float,
    max_price_deviation_pct: float,
    execution_enabled: bool,
    shadow_mode: bool,
    reference_bid: float | None = None,
    reference_ask: float | None = None,
    short_entries_enabled: bool = False,
) -> LLMOrderPolicyDecision:
    action = str(decision.get("order_action") or "NONE").upper()
    if action == "NONE":
        return LLMOrderPolicyDecision(LLMOrderDisposition.REJECT, "NO_ACTION", "no LLM order action")
    if action not in _SUPPORTED_ACTIONS:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "UNKNOWN_ACTION",
            f"unsupported LLM order action: {action}",
        )
    replacement_action = str(decision.get("replacement_action") or "NONE").upper()
    if action == "CANCEL_REPLACE" and replacement_action not in _TRADE_ACTIONS:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "UNKNOWN_REPLACEMENT_ACTION",
            f"unsupported replacement action: {replacement_action}",
        )
    if (
        action == "SELL_SHORT_NOW"
        or action == "CANCEL_REPLACE" and replacement_action == "SELL_SHORT_NOW"
    ) and not short_entries_enabled:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "SHORT_ENTRY_DISABLED",
            "short entry actions are disabled by the live safety policy",
        )

    raw_confidence = decision.get("confidence_score")
    if raw_confidence is None or isinstance(raw_confidence, bool):
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "INVALID_CONFIDENCE",
            "confidence_score must be a finite number",
        )
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "INVALID_CONFIDENCE",
            "confidence_score must be a finite number",
        )
    if not math.isfinite(confidence):
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "INVALID_CONFIDENCE",
            "confidence_score must be a finite number",
        )
    if not 0 <= confidence <= 1:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "INVALID_CONFIDENCE",
            "confidence_score must be between 0 and 1",
        )
    if confidence < min_confidence:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.REJECT,
            "LOW_CONFIDENCE",
            f"confidence_score {confidence:.2f} is below {min_confidence:.2f}",
            confidence=confidence,
        )

    candidate = _candidate_price(action, decision)
    if action != "CANCEL_PENDING":
        if candidate is None:
            return LLMOrderPolicyDecision(
                LLMOrderDisposition.REJECT,
                "INVALID_ORDER_PRICE",
                "LLM order action requires a positive finite order price",
                confidence=confidence,
            )
        reference = _bbo_mid(reference_bid, reference_ask)
        if reference is None:
            return LLMOrderPolicyDecision(
                LLMOrderDisposition.REJECT,
                "NO_TRUSTED_BBO",
                "LLM order action requires a positive bid/ask reference",
                confidence=confidence,
                candidate_price=candidate,
            )
        deviation_pct = abs(candidate - reference) / reference * 100
        if deviation_pct > max_price_deviation_pct:
            return LLMOrderPolicyDecision(
                LLMOrderDisposition.REJECT,
                "PRICE_DEVIATION",
                (
                    f"order price deviation {deviation_pct:.3f}% exceeds "
                    f"{max_price_deviation_pct:.3f}%"
                ),
                confidence=confidence,
                reference_price=reference,
                candidate_price=candidate,
                deviation_pct=deviation_pct,
            )
    else:
        reference = None
        deviation_pct = None

    if shadow_mode or not execution_enabled:
        return LLMOrderPolicyDecision(
            LLMOrderDisposition.SHADOW,
            "SHADOW_MODE",
            "LLM order action recorded in shadow mode",
            confidence=confidence,
            reference_price=reference,
            candidate_price=candidate,
            deviation_pct=deviation_pct,
        )
    return LLMOrderPolicyDecision(
        LLMOrderDisposition.ALLOW,
        "ALLOW",
        "LLM order action passed live policy",
        confidence=confidence,
        reference_price=reference,
        candidate_price=candidate,
        deviation_pct=deviation_pct,
    )


def _candidate_price(action: str, decision: dict[str, Any]) -> float | None:
    if action == "CANCEL_REPLACE":
        raw = decision.get("replacement_price") or decision.get("order_price")
    else:
        raw = decision.get("order_price")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _bbo_mid(bid: float | None, ask: float | None) -> float | None:
    try:
        bid_value = float(bid) if bid is not None else 0.0
        ask_value = float(ask) if ask is not None else 0.0
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(bid_value)
        or not math.isfinite(ask_value)
        or bid_value <= 0
        or ask_value <= 0
        or ask_value < bid_value
    ):
        return None
    return (bid_value + ask_value) / 2
