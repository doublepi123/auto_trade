"""Pure-computation domain package for PREREGISTRATION §10.

``earnings-revenue-guidance-continuation-v1``: point-in-time membership,
guidance-raise eligibility, universe liquidity filters, precise entry
conditions, sizing, quote-supported simulated fills, fixed-barrier exits
and the frozen cost model.  See ``strategy_v2/PREREGISTRATION.md`` §10.
"""

from __future__ import annotations

from app.domain.guidance_continuation.config import (
    ALGORITHM_VERSION,
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
    config_digest,
    config_payload,
    first_passage_driftless_baseline,
)
from app.domain.guidance_continuation.costs import (
    TradeCostBreakdown,
    breakdown,
    commission,
    cost_columns,
)
from app.domain.guidance_continuation.eligibility import (
    GuidanceRaiseVerdict,
    GuidanceStatement,
    announcement_window,
    evaluate_guidance_raise,
    natural_days_between,
    previous_trading_day_close,
    registration_deadline,
)
from app.domain.guidance_continuation.entry import (
    EntryBar,
    EntryConditions,
    EntryEvaluation,
    evaluate_entry,
    expected_bar_starts,
)
from app.domain.guidance_continuation.exit import (
    ExitBarriers,
    ExitEvaluation,
    evaluate_exit,
)
from app.domain.guidance_continuation.membership import (
    CHANGE_ADD,
    CHANGE_REMOVE,
    MEMBER,
    NOT_MEMBER,
    UNKNOWN,
    CoverageProof,
    MembershipChange,
    MembershipSnapshot,
    MembershipVerdict,
    evaluate_membership,
)
from app.domain.guidance_continuation.quotes import (
    EntrySimulationResult,
    QuoteObservation,
    attempt_window,
    entry_cutoff_instant,
    evaluate_entry_time_gate,
    inputs_deadline,
    qualify_quote,
    simulate_entry,
)
from app.domain.guidance_continuation.sizing import (
    ceil_to_tick,
    position_quantity,
)
from app.domain.guidance_continuation.universe import (
    PriorDayBar,
    UniverseVerdict,
    evaluate_universe,
    expected_history_days,
)

__all__ = [
    "ALGORITHM_VERSION",
    "CHANGE_ADD",
    "CHANGE_REMOVE",
    "DEFAULT_GUIDANCE_CONFIG",
    "CoverageProof",
    "ExitBarriers",
    "ExitEvaluation",
    "GuidanceContinuationConfig",
    "GuidanceRaiseVerdict",
    "GuidanceStatement",
    "MEMBER",
    "NOT_MEMBER",
    "UNKNOWN",
    "EntryBar",
    "EntryConditions",
    "EntryEvaluation",
    "MembershipChange",
    "MembershipSnapshot",
    "MembershipVerdict",
    "PriorDayBar",
    "QuoteObservation",
    "EntrySimulationResult",
    "TradeCostBreakdown",
    "announcement_window",
    "attempt_window",
    "breakdown",
    "ceil_to_tick",
    "commission",
    "config_digest",
    "config_payload",
    "cost_columns",
    "entry_cutoff_instant",
    "evaluate_entry",
    "evaluate_entry_time_gate",
    "evaluate_exit",
    "evaluate_guidance_raise",
    "evaluate_membership",
    "evaluate_universe",
    "expected_bar_starts",
    "expected_history_days",
    "first_passage_driftless_baseline",
    "inputs_deadline",
    "natural_days_between",
    "position_quantity",
    "previous_trading_day_close",
    "qualify_quote",
    "registration_deadline",
    "simulate_entry",
]
