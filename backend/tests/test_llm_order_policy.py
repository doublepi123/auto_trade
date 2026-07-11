from __future__ import annotations

import math

from app.services.llm_order_policy import LLMOrderDisposition, evaluate_llm_order_policy


def _evaluate(decision: dict[str, object], *, shadow: bool = False, enabled: bool = True):
    return evaluate_llm_order_policy(
        decision,
        min_confidence=0.7,
        max_price_deviation_pct=1.0,
        execution_enabled=enabled,
        shadow_mode=shadow,
        reference_bid=99.9,
        reference_ask=100.1,
    )


def test_low_confidence_rejected_before_shadow() -> None:
    result = _evaluate(
        {"order_action": "BUY_NOW", "order_price": 100, "confidence_score": 0.69},
        shadow=True,
    )
    assert result.disposition == LLMOrderDisposition.REJECT
    assert result.code == "LOW_CONFIDENCE"


def test_non_finite_confidence_rejected() -> None:
    result = _evaluate(
        {"order_action": "BUY_NOW", "order_price": 100, "confidence_score": math.inf}
    )
    assert result.code == "INVALID_CONFIDENCE"


def test_out_of_range_and_boolean_confidence_rejected() -> None:
    for confidence in (1.01, -0.01, True):
        result = _evaluate(
            {
                "order_action": "BUY_NOW",
                "order_price": 100,
                "confidence_score": confidence,
            }
        )
        assert result.code == "INVALID_CONFIDENCE"


def test_missing_price_rejected() -> None:
    result = _evaluate({"order_action": "SELL_NOW", "confidence_score": 0.9})
    assert result.code == "INVALID_ORDER_PRICE"


def test_missing_bbo_rejected() -> None:
    result = evaluate_llm_order_policy(
        {"order_action": "BUY_NOW", "order_price": 100, "confidence_score": 0.9},
        min_confidence=0.7,
        max_price_deviation_pct=1,
        execution_enabled=True,
        shadow_mode=False,
    )
    assert result.code == "NO_TRUSTED_BBO"


def test_price_deviation_rejected() -> None:
    result = _evaluate(
        {"order_action": "BUY_NOW", "order_price": 102, "confidence_score": 0.9}
    )
    assert result.code == "PRICE_DEVIATION"


def test_valid_action_is_shadowed_without_side_effect_permission() -> None:
    result = _evaluate(
        {"order_action": "BUY_NOW", "order_price": 100, "confidence_score": 0.9},
        shadow=True,
    )
    assert result.disposition == LLMOrderDisposition.SHADOW
    assert result.code == "SHADOW_MODE"
    assert result.to_result("BUY_NOW")["policy_disposition"] == "SHADOW"


def test_valid_live_action_allowed_at_deviation_boundary() -> None:
    result = _evaluate(
        {"order_action": "BUY_NOW", "order_price": 101, "confidence_score": 0.7}
    )
    assert result.disposition == LLMOrderDisposition.ALLOW


def test_cancel_replace_uses_replacement_price() -> None:
    result = _evaluate(
        {
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "BUY_NOW",
            "replacement_price": 102,
            "confidence_score": 0.9,
        }
    )
    assert result.code == "PRICE_DEVIATION"


def test_unknown_actions_and_disabled_short_are_rejected_before_shadow() -> None:
    unknown = _evaluate(
        {"order_action": "MAGIC", "order_price": 100, "confidence_score": 0.9},
        shadow=True,
    )
    assert unknown.code == "UNKNOWN_ACTION"

    replacement = _evaluate(
        {
            "order_action": "CANCEL_REPLACE",
            "replacement_action": "MAGIC",
            "replacement_price": 100,
            "confidence_score": 0.9,
        },
        shadow=True,
    )
    assert replacement.code == "UNKNOWN_REPLACEMENT_ACTION"

    short = _evaluate(
        {
            "order_action": "SELL_SHORT_NOW",
            "order_price": 100,
            "confidence_score": 0.9,
        },
        shadow=True,
    )
    assert short.code == "SHORT_ENTRY_DISABLED"


def test_cancel_pending_does_not_require_bbo_or_price() -> None:
    result = evaluate_llm_order_policy(
        {"order_action": "CANCEL_PENDING", "confidence_score": 0.9},
        min_confidence=0.7,
        max_price_deviation_pct=1,
        execution_enabled=True,
        shadow_mode=False,
    )
    assert result.disposition == LLMOrderDisposition.ALLOW
